"""Start Claude Code inside the runtime when the owner asks for it by phone.

One task at a time. Each task is a transient systemd unit (`claude-task`) running
`python -m ringback.runner "<task>"`, so it outlives the call that started it and
the HTTP request (Runta kills processes an exec session started; systemd doesn't).
"""

from __future__ import annotations

import subprocess
import sys
from typing import Protocol

UNIT = "claude-task"


class TaskLauncher(Protocol):
    def running(self) -> bool: ...
    def start(self, task: str) -> None: ...


class SystemdLauncher:
    def __init__(self, project_dir: str, env_file: str):
        self.project_dir = project_dir
        self.env_file = env_file

    def running(self) -> bool:
        try:  # called from the voice server's event loop: never let a wedged systemd hang the call
            state = subprocess.run(["systemctl", "is-active", UNIT], capture_output=True, text=True,
                                   timeout=5).stdout.strip()
        except subprocess.TimeoutExpired:
            return True  # unknown: report busy rather than start a second task
        return state in ("active", "activating", "reloading")

    def start(self, task: str) -> None:
        if self.running():
            raise RuntimeError("a Claude Code task is already running")
        subprocess.run(["systemctl", "reset-failed", UNIT], capture_output=True, timeout=5)
        subprocess.run(
            [
                "systemd-run", "--unit", UNIT, "--collect",
                "--working-directory", self.project_dir,
                "--property", f"EnvironmentFile={self.env_file}",
                "--setenv", "HOME=/root",
                sys.executable, "-m", "ringback.runner", task,
            ],
            check=True, capture_output=True, text=True, timeout=15,
        )
