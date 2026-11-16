"""Checkpoint before an approved irreversible step: Runta API client, server endpoint, hook."""

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ringback import checkpoint
from ringback.app import create_app
from ringback.config import Settings
from ringback.store import Store

AGENT = {"Authorization": "Bearer agent-secret"}


class Resp:
    def __init__(self, status, data=None):
        self.status_code, self._data, self.text = status, data, json.dumps(data)

    def json(self):
        return self._data


class FakeRunta:
    """Stands in for api.runta.com: creating -> ... -> final state."""

    def __init__(self, states=("creating", "creating", "ready"), create_status=200):
        self.states = list(states)
        self.create_status = create_status
        self.calls = []

    def _cp(self):
        return {"data": {"id": "cp1", "state": self.states.pop(0), "safe_error_code": "disk_busy"}}

    def post(self, url, json=None):
        self.calls.append(("POST", url, json))
        return Resp(self.create_status, self._cp() if self.create_status < 400 else {"error": "nope"})

    def get(self, url):
        self.calls.append(("GET", url, None))
        return Resp(200, self._cp())

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_create_polls_until_ready():
    api = FakeRunta()
    cp = checkpoint.create(api, "rt-1", "before-x", sleep=lambda s: None)
    assert cp["state"] == "ready"
    assert api.calls[0] == ("POST", "/v2/runtimes/rt-1/checkpoints", {"name": "before-x", "kind": "full"})
    assert [c[1] for c in api.calls[1:]] == ["/v2/checkpoints/cp1"] * 2


def test_create_raises_on_failed_or_rejected():
    with pytest.raises(checkpoint.CheckpointError, match="failed: disk_busy"):
        checkpoint.create(FakeRunta(("creating", "failed")), "rt", "n", sleep=lambda s: None)
    with pytest.raises(checkpoint.CheckpointError, match="HTTP 403"):
        checkpoint.create(FakeRunta(create_status=403), "rt", "n", sleep=lambda s: None)


def make(tmp_path, runtime_id="rt-1", api=None):
    settings = Settings(public_url="https://rb", db_path=str(tmp_path / "rb.db"), agent_token="agent-secret",
                        runtime_id=runtime_id)
    store = Store(settings.db_path)
    api = api or FakeRunta(("ready",))
    client = TestClient(create_app(settings, store=store, runta_http=lambda: api), headers=AGENT)
    return client, store


def test_endpoint_checkpoints_and_logs_proof(tmp_path):
    client, store = make(tmp_path)
    r = client.post("/agent/checkpoint", json={"approval_id": "ab12", "label": "DROP COLUMN legacy_email"}).json()
    assert r["ok"] and r["id"] == "cp1" and r["name"].startswith("before-ab12-")
    ev = [e for e in store.recent_events(5) if e["kind"] == "checkpoint"][0]
    assert ev["data"]["approval_id"] == "ab12" and ev["data"]["checkpoint_id"] == "cp1"
    # logged as soon as Runta returned an id, before it was ready, so teardown can find it even if polling fails
    started = [e for e in store.recent_events(5) if e["kind"] == "checkpoint_started"][0]
    assert started["data"]["checkpoint_id"] == "cp1"


def test_endpoint_reports_disabled_and_failures(tmp_path):
    client, _ = make(tmp_path / "off", runtime_id="")
    assert client.post("/agent/checkpoint", json={}).json() == {"ok": False, "enabled": False}

    client, store = make(tmp_path / "bad", api=FakeRunta(("failed",)))
    r = client.post("/agent/checkpoint", json={"approval_id": "x"}).json()
    assert r["ok"] is False and r["enabled"] is True and "disk_busy" in r["error"]
    assert [e["kind"] for e in store.recent_events(3)][-1] == "checkpoint_failed"


@pytest.fixture
def hook(monkeypatch):
    spec = importlib.util.spec_from_file_location("pre", Path(__file__).parent.parent / "hooks" / "claude_pretooluse.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    def approved(*a, **k):
        return subprocess.CompletedProcess(a, 0, stdout=json.dumps({"id": "ab12", "status": "approved"}), stderr="")

    monkeypatch.setattr(mod.subprocess, "run", approved)
    return mod


DROP = {"tool_name": "Bash", "tool_input": {"command": "sqlite3 app.db 'ALTER TABLE users DROP COLUMN legacy_email'"}}


def decision(out):
    return out["hookSpecificOutput"]["permissionDecision"], out["hookSpecificOutput"]["permissionDecisionReason"]


def test_hook_takes_checkpoint_before_allowing(hook, monkeypatch):
    seen = []
    monkeypatch.setattr(hook, "checkpoint", lambda aid, cmd: seen.append(aid) or {"ok": True, "name": "before-ab12-1"})
    allow, reason = decision(hook.decide(DROP))
    assert allow == "allow" and seen == ["ab12"] and "runta checkpoint restore before-ab12-1" in reason


def test_hook_fails_closed_when_checkpoint_fails(hook, monkeypatch):
    monkeypatch.setattr(hook, "checkpoint", lambda aid, cmd: {"ok": False, "enabled": True, "error": "timeout"})
    allow, reason = decision(hook.decide(DROP))
    assert allow == "deny" and "checkpoint failed (timeout)" in reason


def test_hook_allows_when_checkpoints_are_off(hook, monkeypatch):
    monkeypatch.setattr(hook, "checkpoint", lambda aid, cmd: {"ok": False, "enabled": False})
    assert decision(hook.decide(DROP))[0] == "allow"
