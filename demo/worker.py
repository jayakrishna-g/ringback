"""Demo agent: a scripted, long-running "database migration" job.

MOCKED: the work itself (it sleeps and logs; no database is touched).
REAL:   everything it does through `ringback` - progress events, reading the
        owner's phone instructions between steps, and blocking on a phone
        approval before the destructive step.

Run inside the runtime:  python demo/worker.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import time

STEPS = [
    ("Snapshot users table", 4),
    ("Create users_v2 with new schema", 3),
    ("Backfill users_v2 (1.2M rows)", 12),
    ("Verify row counts and checksums", 4),
]
DANGEROUS = "Drop column legacy_email from users on production"


def rb(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "ringback.cli", *args], capture_output=True, text=True)


def check_inbox() -> list[str]:
    out = rb("inbox").stdout.strip()
    texts = [m["text"] for m in json.loads(out or "[]")]
    for t in texts:
        rb("log", f"Got instruction from owner: {t}", "--kind", "instruction_received")
    return texts


def main() -> int:
    rb("task", "Migrate users table to v2 schema and remove legacy_email")
    paused_for_instruction = False
    for name, secs in STEPS:
        for instruction in check_inbox():
            if "stop" in instruction.lower() or "pause" in instruction.lower():
                rb("log", "Stopping at owner's request before: " + name, "--kind", "stopped")
                return 0
            paused_for_instruction = True
        rb("log", f"Started: {name}")
        time.sleep(secs)
        rb("log", f"Done: {name}")

    if paused_for_instruction:
        rb("log", "Applied owner instructions; continuing to the destructive step")

    ask = rb(
        "ask",
        DANGEROUS,
        "--context",
        "Backfill verified: 1,204,311 rows, checksums match. Dropping is irreversible; "
        "a snapshot from step 1 exists.",
        "--timeout",
        "900",
    )
    decision = json.loads(ask.stdout.strip().splitlines()[-1]) if ask.stdout.strip() else {}
    if ask.returncode == 0:
        rb("log", f"Approved by {decision.get('decided_by')}. Dropping legacy_email (mock).")
        time.sleep(2)
        rb("log", "Migration complete", "--kind", "done")
    elif ask.returncode == 1:
        rb("log", f"Denied ({decision.get('note') or 'no reason'}). Leaving legacy_email in place.", "--kind", "done")
    else:
        rb("log", "No decision in time; leaving legacy_email in place and stopping.", "--kind", "done")
    for instruction in check_inbox():
        rb("log", f"Owner instruction noted for follow-up: {instruction}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
