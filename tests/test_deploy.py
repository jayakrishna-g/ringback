"""The .env loader evaluators depend on: comments must not leak into keys."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from deploy import parse_dotenv  # noqa: E402


def test_dotenv_strips_comments_and_quotes():
    env = parse_dotenv(
        "# header\n"
        "OPENAI_API_KEY=sk-real   # becomes a Runta secret\n"
        "TOKEN='a#b # c'\n"
        'QUOTED="x y"\n'
        "URL=https://h/p#frag\n"
        "\n"
        "BAD LINE\n"
    )
    assert env == {"OPENAI_API_KEY": "sk-real", "TOKEN": "a#b # c", "QUOTED": "x y", "URL": "https://h/p#frag"}


def test_env_example_parses_to_placeholders_only():
    env = parse_dotenv((Path(__file__).parent.parent / ".env.example").read_text())
    assert env["OPENAI_API_KEY"] == "sk-..." and env["RINGBACK_OWNER_NUMBERS"] == "+15552223333"
    assert "RINGBACK_IDLE_SECS" not in env  # optional settings stay commented out


ORDER: list[str] = []  # shared across fakes, so the test sees the real deletion order


class FakeRuntime:
    def __init__(self):
        self.deleted = False

    def delete(self):
        self.deleted = True
        ORDER.append("runtime")


class FakeCheckpoints:
    def __init__(self, fail=()):
        self.fail, self.deleted = set(fail), []

    def delete(self, cp_id):
        if cp_id in self.fail:
            raise RuntimeError("503 try again")
        self.deleted.append(cp_id)
        ORDER.append(cp_id)


class FakeClient:
    def __init__(self, fail=()):
        self.rt, self.checkpoints = FakeRuntime(), FakeCheckpoints(fail)
        self.runtimes = type("R", (), {"get": lambda _self, name: self.rt})()


def test_down_deletes_own_checkpoints_then_the_runtime(monkeypatch):
    import deploy

    client = FakeClient()
    monkeypatch.setattr(deploy, "sdk", lambda: client)
    monkeypatch.setattr(deploy, "own_checkpoint_ids", lambda rt: ["cp-1", "cp-2"])
    ORDER.clear()
    deploy.down()
    assert ORDER == ["cp-1", "cp-2", "runtime"]


def test_down_keeps_the_runtime_if_a_checkpoint_survives(monkeypatch):
    """Second review: deleting the runtime first lost the only record of which checkpoints were ours."""
    import deploy
    import pytest

    client = FakeClient(fail={"cp-2"})
    monkeypatch.setattr(deploy, "sdk", lambda: client)
    monkeypatch.setattr(deploy, "own_checkpoint_ids", lambda rt: ["cp-1", "cp-2"])
    with pytest.raises(SystemExit):
        deploy.down()
    assert client.checkpoints.deleted == ["cp-1"] and not client.rt.deleted
