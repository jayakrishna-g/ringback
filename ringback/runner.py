"""Run one Claude Code task headless and report it to ringback.

  python -m ringback.runner "Finish the users migration"

Sets the task, runs `claude -p`, and logs the outcome so the phone line can read
it back. Approvals and progress come from Claude Code hooks (hooks/), not from here.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from .cli import _client

ALLOWED_TOOLS = "Bash Read Edit Write Glob Grep"
PHONE_CONTEXT = """You are running unattended in a cloud runtime. The owner is away from the keyboard
and reachable only by phone. Work on your own; call only when you genuinely need them.

- Risky shell commands (DROP, rm -r, force push, terraform apply...) are held until the owner
  approves them by phone. If one is denied, do not retry it or work around it: stop that step
  and explain why in your summary.
- If you are blocked on a decision only the owner can make (ambiguous requirement, choosing
  between options with real consequences), phone them with a yes/no question:
    {ringback} ask "Should I ...?" --context "one line of why" --timeout 540
  Exit code 0 = yes, 1 = no, 2 = no answer (then take the safe option). The JSON it prints has
  a "note" with anything else they said. Don't call for things you can decide yourself.
- The owner may send instructions by phone; they appear as tool-result context starting
  with "Owner (by phone)". Follow them.
- Finish with a 2-3 sentence plain-text summary. The owner is phoned with it, so make it
  something worth hearing: what changed, and anything they need to do."""


def run(task: str) -> int:
    claude = os.environ.get("RINGBACK_CLAUDE_BIN", "/root/.local/bin/claude")
    ringback = str(Path(sys.executable).with_name("ringback"))
    env = {
        **os.environ,
        # `ringback ask` waits for a person; Claude Code's default 2-minute Bash timeout is too short.
        "BASH_DEFAULT_TIMEOUT_MS": "600000",
        "BASH_MAX_TIMEOUT_MS": "600000",
    }
    with _client() as c:
        c.post("/agent/task", json={"task": task}).raise_for_status()
        c.post("/agent/log", json={"message": f"Claude Code started: {task}", "kind": "task_started"})
        try:
            proc = subprocess.run(
                [claude, "-p", task, "--output-format", "json", "--allowedTools", ALLOWED_TOOLS,
                 "--append-system-prompt", PHONE_CONTEXT.format(ringback=ringback)],
                capture_output=True, text=True, env=env, timeout=int(os.environ.get("RINGBACK_TASK_TIMEOUT", "3600")),
            )
            out = json.loads(proc.stdout or "{}")
        except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as exc:
            c.post("/agent/log", json={"message": f"Claude Code failed: {exc}", "kind": "task_failed"})
            c.post("/agent/call", json={"message": f"The task failed before finishing: {exc}"})
            return 1
        result = str(out.get("result") or proc.stderr.strip() or "no output")[:800]
        failed = proc.returncode != 0 or out.get("is_error")
        cost = out.get("total_cost_usd")
        summary = f"Claude Code {'failed' if failed else 'finished'}: {result}"
        if cost is not None:
            summary += f" (cost ${cost:.2f}, {out.get('num_turns', '?')} turns)"
        c.post("/agent/log", json={"message": summary, "kind": "task_failed" if failed else "task_done"})
        if failed or os.environ.get("RINGBACK_CALL_WHEN_DONE", "1") != "0":
            c.post("/agent/call", json={"message": f"{'Task failed' if failed else 'Task finished'}: {result}"})
        return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(run(" ".join(sys.argv[1:])))
