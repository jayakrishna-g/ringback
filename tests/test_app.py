import json

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from ringback import twilio
from ringback.app import create_app
from ringback.config import Settings
from ringback.store import Store

from .fakes import FakeChannel

PUBLIC = "https://rb.example.runta.app"
OWNER = "+15552223333"
AGENT = {"Authorization": "Bearer agent-secret"}


@pytest.fixture
def env(tmp_path):
    settings = Settings(public_url=PUBLIC, db_path=str(tmp_path / "rb.db"), twilio_auth_token="tw-token",
                        owner_numbers=[OWNER], agent_token="agent-secret")
    store = Store(settings.db_path)
    realtime = FakeChannel()
    calls = []

    async def connector():
        return realtime

    async def placer(to, url):
        calls.append((to, url))
        return "CAout"

    app = create_app(settings, store=store, realtime_connector=connector, call_placer=placer)
    return TestClient(app), store, realtime, calls


def signed_post(client, path, form):
    sig = twilio.compute_signature("tw-token", PUBLIC + path, form)
    return client.post(path, data=form, headers={"X-Twilio-Signature": sig})


def test_signature_roundtrip_and_tamper():
    params = {"CallSid": "CA1", "From": OWNER}
    sig = twilio.compute_signature("t", "https://x/y", params)
    assert twilio.valid_signature("t", "https://x/y", params, sig)
    assert not twilio.valid_signature("t", "https://x/y", {**params, "From": "+1999"}, sig)
    assert not twilio.valid_signature("", "https://x/y", params, sig)


def test_twiml_escapes_attributes():
    xml = twilio.stream_twiml("wss://h/m", {"token": 'a"<b'})
    assert 'value="a&quot;&lt;b"' in xml and '<Stream url="wss://h/m">' in xml


def test_inbound_owner_gets_stream_with_one_time_token(env):
    client, store, *_ = env
    r = signed_post(client, "/twilio/voice", {"CallSid": "CA1", "From": OWNER, "To": "+1000", "Direction": "inbound"})
    assert r.status_code == 200
    assert '<Stream url="wss://rb.example.runta.app/twilio/media">' in r.text
    grant = list(client.app.state.stream_tokens.values())[0]
    assert grant["party"] == OWNER and grant["reason"] == "inbound"


def test_bad_signature_and_strangers_rejected(env):
    client, store, *_ = env
    r = client.post("/twilio/voice", data={"From": OWNER}, headers={"X-Twilio-Signature": "forged"})
    assert r.status_code == 403
    r = signed_post(client, "/twilio/voice", {"CallSid": "CA2", "From": "+19998887777", "Direction": "inbound"})
    assert "<Hangup/>" in r.text and not client.app.state.stream_tokens
    assert {"call_rejected"} <= {e["kind"] for e in store.recent_events(10)}


def test_outbound_call_checks_callee(env):
    client, *_ = env
    r = signed_post(client, "/twilio/voice?reason=approval",
                    {"CallSid": "CA3", "From": "+1000", "To": OWNER, "Direction": "outbound-api"})
    assert "<Stream" in r.text
    assert list(client.app.state.stream_tokens.values())[0]["reason"] == "approval"


def test_media_stream_requires_token(env):
    client, store, realtime, _ = env
    with client.websocket_connect("/twilio/media") as ws:
        ws.send_text(json.dumps({"event": "connected"}))
        ws.send_text(json.dumps({"event": "start", "start": {"streamSid": "MZ", "callSid": "CA", "customParameters": {"token": "guess"}}}))
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_text()
    assert closed.value.code == 1008  # policy violation: no valid one-time token
    assert realtime.sent == []  # never reached OpenAI


def test_media_stream_bridges_to_realtime(env):
    client, store, realtime, _ = env
    signed_post(client, "/twilio/voice", {"CallSid": "CA1", "From": OWNER, "Direction": "inbound"})
    token = next(iter(client.app.state.stream_tokens))
    realtime.push({"type": "response.output_audio.delta", "delta": "QUJD", "item_id": "i1"})
    with client.websocket_connect("/twilio/media") as ws:
        ws.send_text(json.dumps({"event": "start", "start": {"streamSid": "MZ", "callSid": "CA1", "customParameters": {"token": token}}}))
        out = json.loads(ws.receive_text())
        assert out == {"event": "media", "streamSid": "MZ", "media": {"payload": "QUJD"}}
        ws.send_text(json.dumps({"event": "media", "media": {"payload": "AAAA", "timestamp": "20"}}))
        ws.send_text(json.dumps({"event": "stop"}))
    assert realtime.sent[0]["type"] == "session.update"
    assert {"type": "input_audio_buffer.append", "audio": "AAAA"} in realtime.sent
    assert not client.app.state.stream_tokens  # token was single-use


def test_agent_routes_need_token(env):
    client, *_ = env
    assert client.get("/agent/status").status_code == 401
    assert client.get("/agent/status", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/agent/status", headers=AGENT).status_code == 200


def stream_token(twiml):
    import re

    m = re.search(r'name="token" value="([^"]+)"', twiml)
    return m.group(1) if m else None


def test_webhook_retry_is_idempotent_but_replay_after_use_gets_nothing(env):
    """Reviews: replays minted fresh sessions; then the fix rejected Twilio's legitimate retries."""
    client, _, realtime, _ = env
    form = {"CallSid": "CA1", "From": OWNER, "Direction": "inbound"}
    first = stream_token(signed_post(client, "/twilio/voice", form).text)
    retry = stream_token(signed_post(client, "/twilio/voice", form).text)
    assert first and retry == first  # Twilio retried before streaming: same grant, no new one

    realtime.push(None)  # OpenAI side closes straight away; we only need the stream to start
    with client.websocket_connect("/twilio/media") as ws:
        ws.send_text(json.dumps({"event": "start", "start": {"streamSid": "MZ", "callSid": "CA1",
                                                              "customParameters": {"token": first}}}))
        ws.send_text(json.dumps({"event": "stop"}))

    replay = signed_post(client, "/twilio/voice", form)
    assert stream_token(replay.text) is None and "<Hangup/>" in replay.text
    assert media_start(client, first, "CA1") == 1008  # and the used token is dead


def media_start(client, token, call_sid):
    with client.websocket_connect("/twilio/media") as ws:
        ws.send_text(json.dumps({"event": "start", "start": {"streamSid": "MZ", "callSid": call_sid,
                                                              "customParameters": {"token": token}}}))
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_text()
    return closed.value.code


def test_stream_token_is_single_use_and_bound_to_its_call(env):
    client, _, realtime, _ = env
    signed_post(client, "/twilio/voice", {"CallSid": "CA1", "From": OWNER, "Direction": "inbound"})
    token = next(iter(client.app.state.stream_tokens))
    assert media_start(client, token, "CA-other") == 1008  # token issued for CA1, presented by another call
    assert media_start(client, token, "CA1") == 1008       # and it was consumed by that attempt
    signed_post(client, "/twilio/voice", {"CallSid": "CA2", "From": OWNER, "Direction": "inbound"})
    token2 = client.app.state.stream_tokens and next(iter(client.app.state.stream_tokens))
    assert media_start(client, token2, None) == 1008       # a stream that doesn't say which call it is
    assert realtime.sent == []


def test_long_poll_wakes_when_decision_arrives_mid_wait(env):
    """Review finding: the old test decided *before* polling, so it never exercised waiting."""
    import threading
    import time

    client, store, _, _ = env
    approval = client.post("/agent/approvals", json={"question": "Drop column?"}, headers=AGENT).json()["approval"]
    threading.Timer(0.6, lambda: store.decide(approval["id"], approve=True, decided_by=OWNER)).start()
    t0 = time.monotonic()
    got = client.get(f"/agent/approvals/{approval['id']}?wait=10", headers=AGENT).json()["approval"]
    waited = time.monotonic() - t0
    assert got["status"] == "approved" and 0.5 < waited < 3  # returned soon after the decision, not at timeout


def test_announce_failure_on_live_call_falls_back_to_dialling(env):
    """Review finding: an owner hanging up mid-announce made the request fail after the approval was stored."""
    client, store, _, calls = env

    class HungUp:
        call_sid = "CAlive"

        async def announce_approval(self, *a):
            raise ConnectionError("caller hung up")

    client.app.state.live_calls.append(HungUp())
    r = client.post("/agent/approvals", json={"question": "Drop column?"}, headers=AGENT)
    assert r.status_code == 200 and r.json()["approval"]["status"] == "pending"
    assert calls == [(OWNER, f"{PUBLIC}/twilio/voice?reason=approval")]


def test_update_falls_back_to_dialling_if_the_live_call_drops(env):
    """Third review: /agent/call had no fallback; a hang-up mid-announce lost the 'done' call."""
    client, _, _, calls = env

    class HungUp:
        call_sid = "CAlive"

        async def announce(self, *a, **k):
            raise ConnectionError("caller hung up")

    client.app.state.live_calls.append(HungUp())
    r = client.post("/agent/call", json={"message": "Done: migration applied"}, headers=AGENT)
    assert r.json()["delivered"] == "call"
    assert calls == [(OWNER, f"{PUBLIC}/twilio/voice?reason=update")]


def test_agent_approval_places_call_and_long_poll_sees_decision(env):
    client, store, _, calls = env
    r = client.post("/agent/approvals", json={"question": "Drop column?"}, headers=AGENT)
    approval = r.json()["approval"]
    assert calls == [(OWNER, f"{PUBLIC}/twilio/voice?reason=approval")]
    assert r.json()["call_sid"] == "CAout"

    pending = client.get(f"/agent/approvals/{approval['id']}", headers=AGENT).json()["approval"]
    assert pending["status"] == "pending"
    store.decide(approval["id"], approve=False, decided_by=OWNER, note="not today")
    decided = client.get(f"/agent/approvals/{approval['id']}?wait=5", headers=AGENT).json()["approval"]
    assert decided["status"] == "denied" and decided["note"] == "not today"


def test_failed_outbound_call_keeps_approval(tmp_path):
    settings = Settings(public_url=PUBLIC, db_path=str(tmp_path / "rb.db"), owner_numbers=[OWNER], agent_token="agent-secret")

    async def broken(to, url):
        raise RuntimeError("twilio down")

    client = TestClient(create_app(settings, call_placer=broken))
    r = client.post("/agent/approvals", json={"question": "q"}, headers=AGENT)
    assert r.status_code == 200 and r.json()["call_sid"] is None
    status = client.get("/agent/status", headers=AGENT).json()
    assert len(status["pending_approvals"]) == 1
    assert any(e["kind"] == "call_failed" for e in status["recent_events"])
