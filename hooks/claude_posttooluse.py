#!/usr/bin/env python3
"""Claude Code PostToolUse hook: progress to the phone line, phone instructions back to Claude.

After each tool call:
  * Bash commands and file edits are logged, so "what's it doing?" on a call has a real answer.
  * Instructions the owner gave by phone (`send_instruction`) are taken from the inbox and
    returned as additionalContext, so Claude sees them before its next step.

Never blocks Claude: any failure here exits 0 with no output.
"""

from __future__ import annotations

import json
import sys


def summarize(payload: dict) -> str | None:
    tool = payload.get("tool_name", "")
    args = payload.get("tool_input") or {}
    if tool == "Bash":
        return f"Ran: {args.get('command', '')[:160]}"
    if tool in ("Edit", "Write", "MultiEdit"):
        return f"{'Wrote' if tool == 'Write' else 'Edited'} {args.get('file_path', '')}"
    return None  # reads and searches are noise on a phone call


def phone_context(instructions: list[dict]) -> str | None:
    if not instructions:
        return None
    lines = "\n".join(f"- {i['text']}" for i in instructions)
    return f"Owner (by phone) says, follow this before continuing:\n{lines}"


def main(payload: dict) -> dict | None:
    from ringback.cli import _client

    with _client() as c:
        if (line := summarize(payload)) is not None:
            c.post("/agent/log", json={"message": line, "kind": "progress"})
        r = c.get("/agent/inbox")
        context = phone_context(r.json().get("instructions", []) if r.status_code == 200 else [])
    if context is None:
        return None
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": context}}


if __name__ == "__main__":
    try:
        out = main(json.load(sys.stdin))
        if out:
            print(json.dumps(out))
    except Exception:  # noqa: BLE001 - a broken phone line must not break the agent
        pass
    sys.exit(0)
