"""`ringback` - the agent-facing CLI. Any agent that can run a shell command can use it.

  ringback serve                       run the voice server (inside the runtime)
  ringback task "Migrate users table"  set what the agent is working on
  ringback log "Backfilled 40k rows"   append a progress event
  ringback ask "Drop column legacy_email on prod?" [--context ...] [--timeout 900] [--no-call]
                                       block until a human decides by phone
                                       exit 0 = approved, 1 = denied, 2 = no decision
  ringback call "Done: migration finished, 40 rows backfilled"
                                       phone the owner with an update (no answer needed)
  ringback inbox                       print (and consume) instructions from the owner
  ringback status                      task, pending approvals, recent events

Output is JSON on stdout so agents can parse it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import httpx

EXIT_APPROVED, EXIT_DENIED, EXIT_NO_DECISION = 0, 1, 2


def _client() -> httpx.Client:
    base = os.environ.get("RINGBACK_URL", "http://127.0.0.1:8080").rstrip("/")
    token = os.environ.get("RINGBACK_AGENT_TOKEN", "")
    return httpx.Client(base_url=base, headers={"Authorization": f"Bearer {token}"}, timeout=70)


def _out(data: object) -> None:
    print(json.dumps(data))


def ask(client: httpx.Client, question: str, context: str, timeout: float, call: bool) -> int:
    resp = client.post("/agent/approvals", json={"question": question, "context": context, "call": call})
    resp.raise_for_status()
    approval_id = resp.json()["approval"]["id"]
    print(f"waiting for a phone decision on {approval_id}...", file=sys.stderr)
    deadline = time.time() + timeout
    while time.time() < deadline:
        wait = max(1, min(50, deadline - time.time()))
        r = client.get(f"/agent/approvals/{approval_id}", params={"wait": wait})
        r.raise_for_status()
        approval = r.json()["approval"]
        if approval["status"] != "pending":
            _out(approval)
            return EXIT_APPROVED if approval["status"] == "approved" else EXIT_DENIED
    client.post(f"/agent/approvals/{approval_id}/expire").raise_for_status()
    _out({"id": approval_id, "status": "expired"})
    return EXIT_NO_DECISION


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="ringback")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve")
    sub.add_parser("task").add_argument("text")
    lg = sub.add_parser("log")
    lg.add_argument("text")
    lg.add_argument("--kind", default="progress")
    a = sub.add_parser("ask")
    a.add_argument("question")
    a.add_argument("--context", default="")
    a.add_argument("--timeout", type=float, default=900)
    a.add_argument("--no-call", action="store_true", help="don't phone; wait for the owner to call in")
    sub.add_parser("call").add_argument("message")
    sub.add_parser("inbox")
    sub.add_parser("status")
    args = p.parse_args(argv)

    if args.cmd == "serve":
        from .app import main as serve

        serve()
        return 0

    with _client() as c:
        if args.cmd == "task":
            c.post("/agent/task", json={"task": args.text}).raise_for_status()
            _out({"ok": True})
        elif args.cmd == "log":
            r = c.post("/agent/log", json={"message": args.text, "kind": args.kind})
            r.raise_for_status()
            _out(r.json())
        elif args.cmd == "ask":
            return ask(c, args.question, args.context, args.timeout, call=not args.no_call)
        elif args.cmd == "call":
            r = c.post("/agent/call", json={"message": args.message})
            r.raise_for_status()
            _out(r.json())
        elif args.cmd == "inbox":
            r = c.get("/agent/inbox")
            r.raise_for_status()
            _out(r.json()["instructions"])
        elif args.cmd == "status":
            r = c.get("/agent/status")
            r.raise_for_status()
            _out(r.json())
    return 0


if __name__ == "__main__":
    sys.exit(main())
