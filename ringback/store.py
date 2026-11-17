"""Durable state shared by the agent and the phone line.

One SQLite file on the runtime disk. It survives Runta suspend/resume and
shutdown/boot, and every process in the runtime (the voice server, the agent,
a Claude Code hook) can read and write it without a broker.

Three tables:
  events     - append-only log of what the agent did and what humans decided.
               This is the "proof" record: every phone decision lands here with
               the caller number and Twilio CallSid.
  approvals  - blocking questions from the agent ("may I drop this column?").
  inbox      - free-form instructions from a human for the agent's next step.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .redact import redact, redact_data

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  kind TEXT NOT NULL,
  message TEXT NOT NULL,
  data TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS approvals (
  id TEXT PRIMARY KEY,
  created_ts REAL NOT NULL,
  question TEXT NOT NULL,
  context TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending',   -- pending | approved | denied | expired
  decided_ts REAL,
  decided_by TEXT,
  note TEXT
);
CREATE TABLE IF NOT EXISTS inbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  text TEXT NOT NULL,
  sender TEXT NOT NULL,
  consumed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


@dataclass
class Approval:
    id: str
    created_ts: float
    question: str
    context: str
    status: str
    decided_ts: float | None
    decided_by: str | None
    note: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ApprovalError(Exception):
    pass


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._mem_conn = sqlite3.connect(":memory:", check_same_thread=False) if self.path == ":memory:" else None
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        if self._mem_conn is not None:
            yield self._mem_conn
            self._mem_conn.commit()
            return
        conn = sqlite3.connect(self.path, timeout=10)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ---- task / meta -----------------------------------------------------
    def set_task(self, task: str) -> None:
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('task', ?)", (redact(task),))
        self.log("task", f"Task set: {task}")

    def get_task(self) -> str | None:
        with self._conn() as c:
            row = c.execute("SELECT value FROM meta WHERE key='task'").fetchone()
        return row[0] if row else None

    # ---- events ----------------------------------------------------------
    def log(self, kind: str, message: str, **data: Any) -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO events(ts, kind, message, data) VALUES (?, ?, ?, ?)",
                (time.time(), kind, redact(message), json.dumps(redact_data(data))),
            )
            return int(cur.lastrowid)

    def recent_events(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, ts, kind, message, data FROM events ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {"id": r[0], "ts": r[1], "kind": r[2], "message": r[3], "data": json.loads(r[4])}
            for r in reversed(rows)
        ]

    # ---- approvals -------------------------------------------------------
    def request_approval(self, question: str, context: str = "") -> Approval:
        # It's read aloud and stored. Say so if anything was hidden, so the owner knows.
        clean_q, clean_c = redact(question), redact(context)
        if (clean_q, clean_c) != (question, context):
            clean_q += " (credentials hidden)"
        question, context = clean_q, clean_c
        approval_id = uuid.uuid4().hex[:8]
        with self._conn() as c:
            c.execute(
                "INSERT INTO approvals(id, created_ts, question, context) VALUES (?, ?, ?, ?)",
                (approval_id, time.time(), question, context),
            )
        self.log("approval_requested", question, approval_id=approval_id, context=context)
        return self.get_approval(approval_id)  # type: ignore[return-value]

    def get_approval(self, approval_id: str) -> Approval | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT id, created_ts, question, context, status, decided_ts, decided_by, note "
                "FROM approvals WHERE id=?",
                (approval_id,),
            ).fetchone()
        return Approval(*row) if row else None

    def pending_approvals(self) -> list[Approval]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, created_ts, question, context, status, decided_ts, decided_by, note "
                "FROM approvals WHERE status='pending' ORDER BY created_ts"
            ).fetchall()
        return [Approval(*r) for r in rows]

    def decide(self, approval_id: str, approve: bool, decided_by: str, note: str = "", **proof: Any) -> Approval:
        """Record a human decision. Only a pending approval can be decided, once."""
        status = "approved" if approve else "denied"
        note = redact(note)
        with self._conn() as c:
            cur = c.execute(
                "UPDATE approvals SET status=?, decided_ts=?, decided_by=?, note=? "
                "WHERE id=? AND status='pending'",
                (status, time.time(), decided_by, note, approval_id),
            )
            changed = cur.rowcount
        if not changed:
            existing = self.get_approval(approval_id)
            if existing is None:
                raise ApprovalError(f"no approval with id {approval_id}")
            raise ApprovalError(f"approval {approval_id} is already {existing.status}")
        self.log(f"approval_{status}", note or status, approval_id=approval_id, decided_by=decided_by, **proof)
        return self.get_approval(approval_id)  # type: ignore[return-value]

    def expire(self, approval_id: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE approvals SET status='expired' WHERE id=? AND status='pending'", (approval_id,))
        self.log("approval_expired", "No decision before timeout", approval_id=approval_id)

    # ---- inbox -----------------------------------------------------------
    def send_instruction(self, text: str, sender: str, **proof: Any) -> int:
        text = redact(text)
        with self._conn() as c:
            cur = c.execute("INSERT INTO inbox(ts, text, sender) VALUES (?, ?, ?)", (time.time(), text, sender))
            msg_id = int(cur.lastrowid)
        self.log("instruction", text, sender=sender, inbox_id=msg_id, **proof)
        return msg_id

    def take_instructions(self) -> list[dict[str, Any]]:
        """Claim unread instructions and mark them consumed, atomically (agent side).

        One UPDATE ... RETURNING, so two concurrent hooks can't both claim the same row.
        (Delivery isn't acknowledged: if the hook dies after claiming, the instruction is lost.)
        """
        with self._conn() as c:
            rows = c.execute(
                "UPDATE inbox SET consumed=1 WHERE consumed=0 RETURNING id, ts, text, sender"
            ).fetchall()
        return [{"id": r[0], "ts": r[1], "text": r[2], "sender": r[3]} for r in sorted(rows)]

    # ---- summary for the voice model ------------------------------------
    def status_snapshot(self, events: int = 8) -> dict[str, Any]:
        return {
            "task": self.get_task(),
            "pending_approvals": [
                {"id": a.id, "question": a.question, "context": a.context, "waiting_secs": int(time.time() - a.created_ts)}
                for a in self.pending_approvals()
            ],
            "recent_events": [
                {"ago_secs": int(time.time() - e["ts"]), "kind": e["kind"], "message": e["message"]}
                for e in self.recent_events(events)
            ],
        }
