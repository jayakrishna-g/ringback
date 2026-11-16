import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("hook", Path(__file__).parent.parent / "hooks" / "claude_pretooluse.py")
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)

RISKY = [
    "rm -rf build/", "rm -rf __pycache__ src/", "rm -rf __pycache__ && rm -rf data",
    "git push --force origin feat", "git push origin main",
    "psql -c 'DROP TABLE users'", "terraform apply -auto-approve", "kubectl delete ns prod",
    'ringback ask "ok?"; psql -c "DROP TABLE users"',
    # Bypasses found in review (Codex), now caught:
    "rm --recursive data", "rm -f -r data", "rm -R data",
    "git -C /workspace/project push --force origin main", "git push -f origin feat",
    "git push origin +feat", "git push origin HEAD",
    "terraform -chdir=infra destroy", "kubectl -n prod delete pod x",
    "ringback status\nrm -rf data", 'ringback log "$(rm -rf data)"', "ringback log `rm -rf data`",
    # Second review (Codex), now caught:
    "rm 'a;b' -rf data", 'r"m" -rf data', "git -C 'repo;name' push --force origin feature",
    "/tmp/evil/ringback status; rm -rf data", "/tmp/evil/ringback log x && rm -rf y",
    # Third review (Codex): two quoting tricks combined, and glued cache names:
    """r"m" 'a;b' -rf data""", """git -C 'repo;name' p"ush" --force origin feature""",
    "rm -rf __pycache__.pytest_cache", "rm -rf __pycache__data",
]
SAFE = [
    "ls -la", "pytest -q", "git push origin feat/x", "git push -u origin feat/x", "git push --follow-tags origin feat/x",
    "rm notes.txt", "rm -i notes.txt", "rm notes-recent.txt", "git status", "cp -r a b",
    "rm -rf __pycache__ && python3 app.py; rm -rf ./__pycache__/ .pytest_cache",
    '/workspace/venv/bin/ringback ask "Should I DROP COLUMN legacy_email now?" --timeout 540',
]


@pytest.mark.parametrize("cmd", RISKY)
def test_risky_commands_need_a_call(cmd):
    assert hook.is_risky(cmd)


@pytest.mark.parametrize("cmd", SAFE)
def test_safe_commands_pass_without_a_call(cmd):
    assert not hook.is_risky(cmd)


@pytest.mark.parametrize("cmd", ['ringback log "${P@P}"', "ringback log $X", "/tmp/evil/ringback status",
                                 "ringback log a\\\nb", "ringback status > /etc/x"])
def test_ringback_exemption_only_for_a_plain_call(cmd):
    """Second review: `${P@P}` (bash prompt expansion) can execute code; other binaries named ringback aren't ours."""
    assert not hook.is_exempt(cmd)
    assert hook.is_exempt('ringback ask "Should I DROP COLUMN x?" --timeout 540')


def test_non_bash_and_safe_tools_are_not_touched():
    assert hook.decide({"tool_name": "Bash", "tool_input": {"command": "ls"}}) is None
    assert hook.decide({"tool_name": "Edit", "tool_input": {}}) is None


DROP = json.dumps({"tool_name": "Bash", "tool_input": {"command": "rm -rf data"}})


def fake_ask(monkeypatch, returncode, stdout):
    monkeypatch.setattr(hook.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, returncode, stdout=stdout, stderr=""))


def decision(out):
    return out["hookSpecificOutput"]["permissionDecision"]


def test_any_crash_denies_instead_of_failing_open(monkeypatch):
    """A crashing hook exits non-zero, which Claude Code treats as non-blocking: the command would run."""
    fake_ask(monkeypatch, 0, json.dumps({"id": "a1", "status": "approved"}))

    def boom(*a):
        raise ConnectionError("checkpoint connection dropped")

    monkeypatch.setattr(hook, "checkpoint", boom)
    out = hook.main(DROP)
    assert decision(out) == "deny" and "ConnectionError" in out["hookSpecificOutput"]["permissionDecisionReason"]
    assert decision(hook.main("not json")) == "deny"


@pytest.mark.parametrize("rc,stdout", [
    (0, "garbage, not json"),                               # malformed CLI output
    (0, ""),                                                # no output at all
    (0, json.dumps({"id": "a1", "status": "denied"})),      # exit code and decision disagree
    (2, json.dumps({"id": "a1", "status": "expired"})),     # nobody answered
])
def test_only_a_recorded_approval_allows(monkeypatch, rc, stdout):
    fake_ask(monkeypatch, rc, stdout)
    monkeypatch.setattr(hook, "checkpoint", lambda *a: {"ok": False, "enabled": False})
    assert decision(hook.main(DROP)) == "deny"


@pytest.mark.parametrize("cp", [{}, {"ok": True}, {"enabled": True}, {"ok": False},
                                {"ok": True, "name": {"bad": "shape"}}, {"ok": True, "name": " "}, ["ok"]])
def test_unconfirmed_checkpoint_denies(monkeypatch, cp):
    """Second review: a checkpoint reply of `{}` used to count as 'checkpoints disabled' and allow."""
    fake_ask(monkeypatch, 0, json.dumps({"id": "a1", "status": "approved"}))
    monkeypatch.setattr(hook, "checkpoint", lambda *a: cp)
    assert decision(hook.main(DROP)) == "deny"
