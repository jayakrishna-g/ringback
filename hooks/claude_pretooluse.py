#!/usr/bin/env python3
"""Claude Code PreToolUse hook: risky shell commands wait for a phone approval.

Wire it up in the runtime's .claude/settings.json (see hooks/settings.example.json).
Claude Code pipes the pending tool call as JSON on stdin; we reply with a
permissionDecision. Safe commands pass straight through without a call.

The risk filter is a heuristic guardrail for a cooperative agent, not a sandbox: it
catches the common destructive commands and their usual spellings, but code run
through an interpreter (python -c, a script file) can do the same things unseen.
What it does guarantee: once a command *is* gated, it runs only on an explicit
phone approval (and a checkpoint, when enabled). Any error here denies.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys

# One simple command's worth of text: stop at shell separators and newlines.
_ARGS = r"[^;&|\n]*"
RISKY = [
    rf"\brm\b{_ARGS}\s(-[a-zA-Z]*[rR][a-zA-Z]*|--recursive)\b",  # rm -r, -rf, -f -r, -R, --recursive
    rf"\bgit\b{_ARGS}\bpush\b{_ARGS}(--force\b|--force-with-lease\b|\s-[a-zA-Z]*f\b|\s\+\S)",  # incl. git -C x push
    rf"\bgit\b{_ARGS}\bpush\b{_ARGS}\b(main|master|prod\w*|HEAD)\b",
    r"\b(DROP|TRUNCATE)\s+(TABLE|COLUMN|DATABASE|SCHEMA)\b",
    rf"\bterraform\b{_ARGS}\b(apply|destroy)\b",
    rf"\bkubectl\b{_ARGS}\bdelete\b",
]


# Recursive deletes of throwaway build/cache dirs aren't worth a phone call.
# (Seen live: Claude Code tidying `rm -rf __pycache__` rang the owner.)
CACHE_RM = re.compile(
    r"\brm\s+-[a-z]*r[a-z]*(?:\s+(?:\./)?(?:__pycache__|\.pytest_cache|\.mypy_cache|\.ruff_cache)/?)+\s*(?=$|[;&|])",
    re.IGNORECASE,
)


# The agent phoning the owner is never itself risky, even if the question mentions DROP,
# but only as one plain command of the real ringback binary: no second line, separator,
# redirect, substitution or parameter expansion (`${P@P}` can execute code).
RINGBACK_CMD = re.compile(r"^\s*(/workspace/venv/bin/)?ringback\s+(ask|call|log|status|inbox|task)\b")
SHELL_META = re.compile(r"[;&|<>\n`$\\]")
_QUOTED = re.compile(r"'[^']*'|\"(?:[^\"\\]|\\.)*\"")


def _shell_words(command: str) -> str | None:
    """How bash would split it: quotes removed, separators kept as their own words, and
    separators *inside* a quoted word neutralised (`r"m" 'a;b' -rf x` -> `rm a_b -rf x`)."""
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=";&|<>")
        lex.whitespace_split = True
        lex.commenters = ""
        words = list(lex)
    except ValueError:  # unbalanced quotes: bash would refuse it too
        return None
    return " ".join(w if set(w) <= set(";&|<>") else re.sub(r"[;&|\n]", "_", w) for w in words)


def _views(command: str) -> list[str]:
    """The same command seen several ways, so quoting can't hide it from the patterns.

    * as written                        (`psql -c 'DROP TABLE x'`)
    * quoted text blanked out           (`rm 'a;b' -rf data` -> `rm Q -rf data`)
    * quote characters dropped          (`r"m" -rf data`     -> `rm -rf data`)
    * split into words like bash does   (`r"m" 'a;b' -rf data` -> `rm a_b -rf data`)
    """
    views = [command, _QUOTED.sub("Q", command), re.sub(r"[\"'\\]", "", command)]
    words = _shell_words(command)
    return views + [words] if words is not None else views


def is_exempt(command: str) -> bool:
    """A plain call to the real ringback CLI: the agent phoning the owner."""
    return bool(RINGBACK_CMD.match(command)) and not SHELL_META.search(command)


def is_risky(command: str) -> bool:
    if is_exempt(command):
        return False
    return any(re.search(p, CACHE_RM.sub("", view), re.IGNORECASE) for view in _views(command) for p in RISKY)


def checkpoint(approval_id: str, command: str) -> dict:
    """Ask the server to checkpoint the runtime before the approved command runs."""
    from ringback.cli import _client

    with _client() as c:
        c.timeout = 300  # a full checkpoint took 39-67 s in testing
        r = c.post("/agent/checkpoint", json={"approval_id": approval_id, "label": command})
        return r.json() if r.status_code == 200 else {"ok": False, "enabled": True, "error": f"HTTP {r.status_code}"}


def deny(reason: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def decide(payload: dict) -> dict | None:
    if payload.get("tool_name") != "Bash":
        return None
    command = (payload.get("tool_input") or {}).get("command", "")
    if not is_risky(command):
        return None
    proc = subprocess.run(
        [sys.executable, "-m", "ringback.cli", "ask", f"Claude Code wants to run: {command}",
         "--context", payload.get("cwd", ""), "--timeout", "300"],
        capture_output=True, text=True,
    )
    lines = proc.stdout.strip().splitlines()
    result = json.loads(lines[-1]) if lines else {}
    # Both must agree: the CLI's exit code and the recorded decision.
    allow = proc.returncode == 0 and result.get("status") == "approved"
    reason = f"Owner {'approved' if allow else 'did not approve'} by phone"
    if result.get("note"):
        reason += f": {result['note']}"
    if not allow:
        return deny(reason)
    cp = checkpoint(result.get("id", ""), command)
    if not isinstance(cp, dict):
        cp = {}
    if cp.get("ok") is True and isinstance(cp.get("name"), str) and cp["name"].strip():
        reason += (f". Runtime checkpoint {cp['name']} taken first; undo runtime-local changes with "
                   f"`runta checkpoint restore {cp['name']} <new-runtime>`")
    elif cp.get("enabled") is not False:
        # Fail closed: checkpoints on (or unknown), and we couldn't confirm a restore point.
        return deny(f"{reason}, but the pre-step checkpoint failed ({cp.get('error', 'no confirmation')}), "
                    "so it was not run")
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                                   "permissionDecisionReason": reason}}


def main(stdin_text: str) -> dict | None:
    """Never fail open: a crash would exit non-zero, which Claude Code treats as a non-blocking error."""
    try:
        return decide(json.loads(stdin_text))
    except Exception as exc:  # noqa: BLE001
        return deny(f"ringback approval hook failed ({type(exc).__name__}: {exc}); not running it")


if __name__ == "__main__":
    out = main(sys.stdin.read())
    if out:
        print(json.dumps(out))
    sys.exit(0)
