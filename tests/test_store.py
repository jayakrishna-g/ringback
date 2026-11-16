import json

import pytest

from ringback.store import ApprovalError, Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "rb.db")


def test_approval_decided_once_with_proof(store):
    a = store.request_approval("Drop column?", "verified")
    assert [p.id for p in store.pending_approvals()] == [a.id]

    done = store.decide(a.id, approve=True, decided_by="+1555", call_sid="CA1", caller_words="yes, drop it")
    assert done.status == "approved" and done.decided_by == "+1555"
    assert store.pending_approvals() == []

    with pytest.raises(ApprovalError, match="already approved"):
        store.decide(a.id, approve=False, decided_by="+1555")

    proof = [e for e in store.recent_events(20) if e["kind"] == "approval_approved"][0]
    assert proof["data"]["call_sid"] == "CA1"
    assert proof["data"]["caller_words"] == "yes, drop it"


def test_unknown_and_expired(store):
    with pytest.raises(ApprovalError, match="no approval"):
        store.decide("nope", approve=True, decided_by="x")
    a = store.request_approval("q")
    store.expire(a.id)
    assert store.get_approval(a.id).status == "expired"
    with pytest.raises(ApprovalError, match="already expired"):
        store.decide(a.id, approve=True, decided_by="x")


def test_inbox_is_consumed_once(store):
    store.send_instruction("skip the staging step", sender="+1555")
    assert [m["text"] for m in store.take_instructions()] == ["skip the staging step"]
    assert store.take_instructions() == []


def test_state_survives_reopen(tmp_path):
    """The runtime may shut down and boot again; state is just a file on disk."""
    path = tmp_path / "rb.db"
    s1 = Store(path)
    s1.set_task("migrate")
    a = s1.request_approval("q")
    s2 = Store(path)
    assert s2.get_task() == "migrate"
    assert s2.pending_approvals()[0].id == a.id


def test_snapshot_shape(store):
    store.set_task("t")
    store.request_approval("q", "ctx")
    snap = store.status_snapshot()
    assert snap["task"] == "t"
    assert snap["pending_approvals"][0]["question"] == "q"
    assert snap["recent_events"][-1]["kind"] == "approval_requested"


def test_secrets_in_commands_are_redacted_before_storage(tmp_path):
    """Review finding: raw commands were logged and fed to the voice model verbatim."""
    store = Store(tmp_path / "rb.db")
    store.log("progress", "Ran: curl -H 'Authorization: Bearer abcdefgh12345678' https://api", token="t0ps3cret")
    a = store.request_approval("Claude Code wants to run: mysql --password hunter2 -e 'DROP TABLE x'")
    stored = json.dumps(store.recent_events(5))
    assert "abcdefgh12345678" not in stored and "t0ps3cret" not in stored and "hunter2" not in stored
    assert "hunter2" not in a.question and "DROP TABLE x" in a.question  # the risky part is still read out


def test_concurrent_inbox_claims_never_double_deliver(tmp_path):
    """Review finding: SELECT-then-UPDATE let two hooks claim the same instruction."""
    from concurrent.futures import ThreadPoolExecutor

    store = Store(tmp_path / "rb.db")
    for i in range(50):
        store.send_instruction(f"step {i}", sender="+1")
    with ThreadPoolExecutor(8) as pool:
        batches = list(pool.map(lambda _: store.take_instructions(), range(16)))
    ids = [m["id"] for b in batches for m in b]
    assert sorted(ids) == list(range(1, 51))  # every instruction claimed exactly once (delivery isn't acked)


def test_redaction_never_hides_what_a_command_does(tmp_path):
    """Second review: masking the word after "token" read `rm -rf token /production` as `... [REDACTED]`."""
    store = Store(tmp_path / "rb.db")
    for cmd in ["rm -rf token /production", "DROP TABLE token CASCADE", "echo password reset done"]:
        assert store.request_approval(f"Claude Code wants to run: {cmd}").question.endswith(cmd)
    store.log("x", "x", token='a"b', nested={"secret": {"k": "v"}}, note="back\\slash")
    assert store.status_snapshot()["recent_events"]  # stored JSON still parses (it didn't before)


def test_every_stored_field_is_redacted_not_just_events(tmp_path):
    """Third review: task, approval notes and inbox rows were stored raw; quoted values leaked a suffix."""
    store = Store(tmp_path / "rb.db")
    store.set_task("migrate with password=hunter2")
    a = store.request_approval("Claude Code wants to run: mysql --password 'hello world' -e 'DROP TABLE x'")
    store.decide(a.id, approve=True, decided_by="+1", note="use token: s3cr3tvalue")
    store.send_instruction("the api_key=abc123xyz is in vault", sender="+1")
    everything = json.dumps([store.status_snapshot(), store.get_approval(a.id).to_dict(), store.take_instructions(),
                             store.recent_events(20)])
    for leaked in ["hunter2", "hello", "world", "s3cr3tvalue", "abc123xyz"]:
        assert leaked not in everything
    assert "DROP TABLE x" in a.question
