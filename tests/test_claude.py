"""Phone <-> Claude Code: start a task by phone, ask on a live call, runner, progress hook."""

import asyncio
import importlib.util
import json
import stat
from pathlib import Path

from fastapi.testclient import TestClient

from ringback import runner
from ringback.app import create_app
from ringback.config import Settings
from ringback.store import Store
from ringback.voice import CallSession

from .fakes import FakeChannel

AGENT = {"Authorization": "Bearer agent-secret"}


class FakeLauncher:
    def __init__(self, busy: bool = False):
        self.busy = busy
        self.started: list[str] = []

    def running(self) -> bool:
        return self.busy

    def start(self, task: str) -> None:
        self.started.append(task)
        self.busy = True


def session(tmp_path, launcher=None):
    store = Store(tmp_path / "rb.db")
    rt = FakeChannel()
    s = CallSession(store=store, twilio=FakeChannel(), realtime=rt, caller="+1555", call_sid="CA1",
                    stream_sid="MZ1", launcher=launcher)
    return store, rt, s


def outputs(rt):
    return [json.loads(m["item"]["output"]) for m in rt.sent if m["type"] == "conversation.item.create"]


def test_phone_starts_claude_code_with_proof(tmp_path):
    launcher = FakeLauncher()
    store, rt, s = session(tmp_path, launcher)
    asyncio.run(s.handle_realtime({"type": "input_audio_buffer.committed", "item_id": "u2"}))  # the request
    asyncio.run(s.run_tool("c1", "start_task", json.dumps(
        {"task": "Fix the failing test in billing", "caller_words": "fix billing", "confirmed": False})))
    assert launcher.started == [] and outputs(rt)[0]["started"] is False  # registered, read back next
    asyncio.run(s.handle_realtime({"type": "input_audio_buffer.committed", "item_id": "u3"}))  # "yes, go"
    asyncio.run(s.handle_realtime({"type": "conversation.item.input_audio_transcription.completed",
                                   "item_id": "u3", "transcript": "yes, go"}))

    asyncio.run(s.run_tool("c2", "start_task", json.dumps(
        {"task": "something else entirely", "caller_words": "yes, go", "confirmed": True})))

    assert launcher.started == ["Fix the failing test in billing"]  # what was read back, not a new wording
    assert outputs(rt)[1]["ok"] is True
    ev = [e for e in store.recent_events(5) if e["kind"] == "task_requested"][0]
    assert ev["data"]["requested_by"] == "+1555" and ev["data"]["call_sid"] == "CA1"
    assert ev["data"]["caller_item_id"] == "u3"
    assert ev["data"]["caller_turn_transcript"] == "yes, go"
    assert s.snapshot()["claude_code"] == "running"


def test_start_task_refused_when_busy_or_disabled(tmp_path):
    launcher = FakeLauncher(busy=True)
    _, rt, s = session(tmp_path, launcher)
    _, rt2, s2 = session(tmp_path / "b", None)
    asyncio.run(s.handle_realtime({"type": "input_audio_buffer.committed"}))

    asyncio.run(s.run_tool("c1", "start_task", json.dumps({"task": "x", "caller_words": "yes"})))
    asyncio.run(s2.run_tool("c1", "start_task", json.dumps({"task": "x", "caller_words": "yes"})))

    assert launcher.started == []
    assert "already running" in outputs(rt)[0]["error"]
    assert "not enabled" in outputs(rt2)[0]["error"]


def test_start_task_needs_a_yes_after_the_read_back(tmp_path):
    """Third review: the turn that *described* the task used to be enough to start it."""
    launcher = FakeLauncher()
    _, rt, s = session(tmp_path, launcher)
    asyncio.run(s.handle_realtime({"type": "input_audio_buffer.committed", "item_id": "u1"}))  # the description
    asyncio.run(s.run_tool("c1", "start_task", json.dumps({"task": "rm everything", "caller_words": "x",
                                                            "confirmed": True})))  # skipping step 1
    assert launcher.started == [] and outputs(rt)[0]["started"] is False
    asyncio.run(s.run_tool("c2", "start_task", json.dumps({"task": "rm everything", "caller_words": "yes",
                                                            "confirmed": True})))  # no reply to the read-back
    assert launcher.started == [] and "hasn't answered the read-back" in outputs(rt)[1]["error"]


def test_approval_while_owner_on_the_line_is_asked_there_not_dialled(tmp_path):
    settings = Settings(public_url="https://rb", db_path=str(tmp_path / "rb.db"), owner_numbers=["+1555"],
                        agent_token="agent-secret")
    store = Store(settings.db_path)
    dialled = []

    async def placer(to, url):
        dialled.append(to)
        return "CAout"

    app = create_app(settings, store=store, call_placer=placer)
    client = TestClient(app)
    _, rt, live = session(tmp_path / "live")
    app.state.live_calls.append(live)

    r = client.post("/agent/approvals", headers=AGENT, json={"question": "Claude Code wants to run: DROP COLUMN"})

    assert r.status_code == 200 and dialled == []
    system_msg = rt.sent[0]["item"]
    assert system_msg["role"] == "system" and "DROP COLUMN" in system_msg["content"][0]["text"]
    assert rt.sent[1] == {"type": "response.create"}

    app.state.live_calls.clear()  # owner hung up: next approval phones them
    client.post("/agent/approvals", headers=AGENT, json={"question": "q2"})
    assert dialled == ["+1555"]


def test_runner_runs_claude_and_reports_result(tmp_path, monkeypatch):
    settings = Settings(public_url="https://rb", db_path=str(tmp_path / "rb.db"), agent_token="agent-secret")
    store = Store(settings.db_path)
    client = TestClient(create_app(settings, store=store), headers=AGENT)
    monkeypatch.setattr(runner, "_client", lambda: client)

    fake = tmp_path / "claude"
    fake.write_text("#!/bin/sh\n"
                    "echo '{\"result\": \"Backfilled 40 rows. Drop was denied by phone.\", "
                    "\"is_error\": false, \"total_cost_usd\": 0.21, \"num_turns\": 9}'\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("RINGBACK_CLAUDE_BIN", str(fake))

    assert runner.run("Finish the users migration") == 0
    assert store.get_task() == "Finish the users migration"
    done = [e for e in store.recent_events(10) if e["kind"] == "task_done"][0]
    assert "Drop was denied by phone" in done["message"] and "$0.21" in done["message"]


def load_hook(name):
    path = Path(__file__).parent.parent / "hooks" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_posttooluse_logs_work_and_delivers_phone_instructions(tmp_path, monkeypatch):
    settings = Settings(public_url="https://rb", db_path=str(tmp_path / "rb.db"), agent_token="agent-secret")
    store = Store(settings.db_path)
    client = TestClient(create_app(settings, store=store), headers=AGENT)
    import ringback.cli

    monkeypatch.setattr(ringback.cli, "_client", lambda: client)
    hook = load_hook("claude_posttooluse")

    assert hook.main({"tool_name": "Read", "tool_input": {"file_path": "x"}}) is None
    store.send_instruction("skip the changelog", sender="+1555")
    out = hook.main({"tool_name": "Bash", "tool_input": {"command": "python seed.py"}})

    assert "Ran: python seed.py" in [e["message"] for e in store.recent_events(5)]
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUse" and "skip the changelog" in ctx
    assert hook.main({"tool_name": "Bash", "tool_input": {"command": "ls"}}) is None  # consumed once


def make_app(tmp_path, owners=("+1555",)):
    settings = Settings(public_url="https://rb", db_path=str(tmp_path / "rb.db"), owner_numbers=list(owners),
                        agent_token="agent-secret")
    store = Store(settings.db_path)
    dialled = []

    async def placer(to, url):
        dialled.append((to, url))
        return "CAupd"

    app = create_app(settings, store=store, call_placer=placer)
    return app, TestClient(app, headers=AGENT), store, dialled


def test_agent_update_phones_owner_or_speaks_on_live_call(tmp_path):
    app, client, store, dialled = make_app(tmp_path)

    r = client.post("/agent/call", json={"message": "Migration finished, 40 rows backfilled"}).json()
    assert r["delivered"] == "call" and dialled == [("+1555", "https://rb/twilio/voice?reason=update")]
    assert "Migration finished" in [e["message"] for e in store.recent_events(5) if e["kind"] == "call_reason"][0]

    _, rt, live = session(tmp_path / "live")
    app.state.live_calls.append(live)
    r = client.post("/agent/call", json={"message": "Tests are red on main"}).json()
    assert r["delivered"] == "live_call" and len(dialled) == 1
    assert "Tests are red on main" in rt.sent[0]["item"]["content"][0]["text"]


def test_update_opener_tells_line_to_lead_with_the_reason(tmp_path):
    from ringback.voice import build_instructions

    assert "call_reason" in build_instructions({}, "update")


def test_runner_phones_owner_when_done_and_gives_bash_time_to_wait(tmp_path, monkeypatch):
    app, client, store, dialled = make_app(tmp_path)
    monkeypatch.setattr(runner, "_client", lambda: client)
    fake = tmp_path / "claude"
    fake.write_text("#!/bin/sh\n"
                    "echo \"{\\\"result\\\": \\\"done, bash timeout $BASH_DEFAULT_TIMEOUT_MS\\\", \\\"is_error\\\": false}\"\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("RINGBACK_CLAUDE_BIN", str(fake))

    assert runner.run("Do the thing") == 0
    assert dialled and dialled[0][1].endswith("reason=update")
    reason = [e for e in store.recent_events(10) if e["kind"] == "call_reason"][0]["message"]
    assert reason == "Task finished: done, bash timeout 600000"

    monkeypatch.setenv("RINGBACK_CALL_WHEN_DONE", "0")
    runner.run("Quiet task")
    assert len(dialled) == 1  # success stays quiet when opted out
