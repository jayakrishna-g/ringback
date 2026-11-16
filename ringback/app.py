"""HTTP + websocket server. Runs inside the Runta runtime on one published port.

Phone side (public, Twilio-authenticated):
  POST /twilio/voice     Twilio webhook for inbound and outbound calls -> TwiML
  WS   /twilio/media     Twilio Media Stream, bridged to OpenAI Realtime

Agent side (bearer RINGBACK_AGENT_TOKEN; used by `ringback` CLI and hooks):
  POST /agent/task, /agent/log, /agent/approvals
  GET  /agent/approvals/{id}, /agent/inbox, /agent/status
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import Response
from pydantic import BaseModel

from . import checkpoint, twilio
from .config import Settings
from .redact import redact
from .store import Store
from .tasks import SystemdLauncher, TaskLauncher
from .voice import CallSession, Channel, RealtimeChannel, StarletteChannel

log = logging.getLogger("ringback.app")

TOKEN_TTL_SECS = 60
CallPlacer = Callable[[str, str], Awaitable[str]]  # (to_number, twiml_url) -> CallSid


class TaskIn(BaseModel):
    task: str


class LogIn(BaseModel):
    message: str
    kind: str = "progress"


class CheckpointIn(BaseModel):
    label: str = ""
    approval_id: str = ""


class CallIn(BaseModel):
    message: str


class ApprovalIn(BaseModel):
    question: str
    context: str = ""
    call: bool = True  # phone the owner right away


def create_app(
    settings: Settings,
    store: Store | None = None,
    realtime_connector: Callable[[], Awaitable[Channel]] | None = None,
    call_placer: CallPlacer | None = None,
    launcher: TaskLauncher | None = None,
    runta_http: Callable[[], Any] | None = None,
) -> FastAPI:
    app = FastAPI(title="ringback")
    store = store or Store(settings.db_path)
    stream_tokens: dict[str, dict[str, Any]] = {}  # one-time tokens: webhook -> media stream

    async def default_connector() -> Channel:
        return await RealtimeChannel.connect(settings.realtime_url, settings.realtime_model, settings.openai_api_key)

    async def default_placer(to_number: str, url: str) -> str:
        return await twilio.place_call(
            account_sid=settings.twilio_account_sid,
            auth_token=settings.twilio_auth_token,
            from_number=settings.twilio_number,
            to_number=to_number,
            url=url,
        )

    connect_realtime = realtime_connector or default_connector
    place_call = call_placer or default_placer
    if launcher is None and settings.project_dir:
        launcher = SystemdLauncher(settings.project_dir, settings.env_file)
    live_calls: list[CallSession] = []  # owner calls in progress
    # Each Twilio CallSid gets one stream token, ever. A retry before the stream starts gets the
    # same token back (idempotent); once used or expired, a replay gets nothing. In memory only:
    # a restart forgets, which the README notes.
    issued: dict[str, str] = {}  # CallSid -> token
    app.state.store = store
    app.state.stream_tokens = stream_tokens
    app.state.live_calls = live_calls

    # ---- auth helpers ----------------------------------------------------
    def require_agent(request: Request) -> None:
        expected = settings.agent_token
        got = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if not expected or not hmac.compare_digest(got, expected):
            raise HTTPException(401, "bad agent token")

    def public_url_for(request: Request) -> str:
        # Behind Runta ingress the Host/scheme the app sees may not be what Twilio signed.
        base = settings.public_url or str(request.base_url).rstrip("/")
        query = f"?{request.url.query}" if request.url.query else ""
        return f"{base}{request.url.path}{query}"

    # ---- phone side ------------------------------------------------------
    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "pending": len(store.pending_approvals())}

    @app.post("/twilio/voice")
    async def twilio_voice(request: Request) -> Response:
        form = {k: str(v) for k, v in (await request.form()).items()}
        if settings.verify_twilio_signature:
            sig = request.headers.get("x-twilio-signature", "")
            if not twilio.valid_signature(settings.twilio_auth_token, public_url_for(request), form, sig):
                store.log("call_rejected", "Bad Twilio signature", from_=form.get("From", ""))
                raise HTTPException(403, "bad signature")

        outbound = form.get("Direction", "inbound") != "inbound"
        party = form.get("To" if outbound else "From", "")
        if party not in settings.owner_numbers:
            store.log("call_rejected", f"Caller {party} not on the owner list", call_sid=form.get("CallSid", ""))
            return Response(twilio.reject_twiml("This line is not available."), media_type="application/xml")

        call_sid = form.get("CallSid", "")
        base = settings.public_url or str(request.base_url).rstrip("/")
        ws_url = base.replace("https://", "wss://").replace("http://", "ws://") + "/twilio/media"
        prior = issued.get(call_sid)
        if prior and prior in stream_tokens and stream_tokens[prior]["expires"] > time.time():
            return Response(twilio.stream_twiml(ws_url, {"token": prior}), media_type="application/xml")  # retry
        if not call_sid or prior:
            # A signed webhook can be replayed (Twilio signatures carry no timestamp). One call, one stream.
            store.log("call_rejected", "Webhook for a CallSid that already had a stream (replay?)", call_sid=call_sid)
            return Response(twilio.reject_twiml("This line is not available."), media_type="application/xml")
        token = secrets.token_urlsafe(16)
        issued[call_sid] = token
        stream_tokens[token] = {
            "party": party,
            "call_sid": form.get("CallSid", ""),
            "reason": request.query_params.get("reason", "outbound" if outbound else "inbound"),
            "expires": time.time() + TOKEN_TTL_SECS,
        }
        return Response(twilio.stream_twiml(ws_url, {"token": token}), media_type="application/xml")

    @app.websocket("/twilio/media")
    async def twilio_media(ws: WebSocket) -> None:
        await ws.accept()
        chan = StarletteChannel(ws)
        start: dict[str, Any] | None = None
        while start is None:  # Twilio sends "connected" then "start"
            msg = await chan.recv()
            if msg is None:
                return
            if msg.get("event") == "start":
                start = msg["start"]
        token = (start.get("customParameters") or {}).get("token", "")
        grant = stream_tokens.pop(token, None)
        if grant is None or grant["expires"] < time.time() or start.get("callSid") != grant["call_sid"]:
            # The token is bound to the CallSid it was issued for.
            store.log("call_rejected", "Media stream without a valid one-time token for this call")
            await ws.close(code=1008)
            return
        realtime = await connect_realtime()
        session = CallSession(
            store=store,
            twilio=chan,
            realtime=realtime,
            caller=grant["party"],
            call_sid=grant["call_sid"],
            stream_sid=start["streamSid"],
            reason=grant["reason"],
            voice=settings.realtime_voice,
            model=settings.realtime_model,
            launcher=launcher,
        )
        live_calls.append(session)
        try:
            await session.run()
        finally:
            live_calls.remove(session)

    # ---- agent side ------------------------------------------------------
    @app.post("/agent/task", dependencies=[Depends(require_agent)])
    async def set_task(body: TaskIn) -> dict[str, Any]:
        store.set_task(body.task)
        return {"ok": True}

    @app.post("/agent/log", dependencies=[Depends(require_agent)])
    async def agent_log(body: LogIn) -> dict[str, Any]:
        return {"ok": True, "id": store.log(body.kind, body.message)}

    @app.post("/agent/approvals", dependencies=[Depends(require_agent)])
    async def create_approval(body: ApprovalIn, request: Request) -> dict[str, Any]:
        approval = store.request_approval(body.question, body.context)
        call_sid = None
        live = live_calls[-1] if (body.call and live_calls) else None  # pick before any await
        if live is not None:
            # The owner is already on the line (e.g. just started this task): ask there, don't dial a busy phone.
            try:
                await live.announce_approval(approval.id, approval.question, approval.context)
                store.log("approval_announced", f"Asked on the live call for approval {approval.id}",
                          approval_id=approval.id, call_sid=live.call_sid)
            except Exception as exc:  # noqa: BLE001 - they hung up mid-announce: fall back to dialling
                log.warning("live-call announce failed: %s", exc)
                live = None
        if live is None and body.call and settings.owner_numbers:
            base = settings.public_url or str(request.base_url).rstrip("/")
            try:
                call_sid = await place_call(settings.owner_numbers[0], f"{base}/twilio/voice?reason=approval")
                store.log("call_placed", f"Calling {settings.owner_numbers[0]} for approval {approval.id}",
                          approval_id=approval.id, call_sid=call_sid)
            except Exception as exc:  # the approval still stands; the owner can call in
                log.exception("outbound call failed")
                store.log("call_failed", f"Could not place call: {exc}", approval_id=approval.id)
        return {"ok": True, "approval": approval.to_dict(), "call_sid": call_sid}

    def default_runta_http() -> Any:
        import httpx

        return httpx.Client(base_url=settings.runta_api, timeout=30)  # no token: the secret stub adds it

    make_runta_http = runta_http or default_runta_http

    @app.post("/agent/checkpoint", dependencies=[Depends(require_agent)])
    async def take_checkpoint(body: CheckpointIn) -> dict[str, Any]:
        """Snapshot the whole runtime (disk + memory) so an approved irreversible step can be undone."""
        if not settings.runtime_id:
            return {"ok": False, "enabled": False}
        name = f"before-{body.approval_id or 'step'}-{int(time.time())}"
        t0 = time.monotonic()

        def started(cp_id: str) -> None:  # logged before it's ready, so teardown can always find it
            store.log("checkpoint_started", name, checkpoint_id=cp_id, approval_id=body.approval_id)

        def run() -> dict[str, Any]:
            with make_runta_http() as http:
                return checkpoint.create(http, settings.runtime_id, name, on_created=started)

        try:
            cp = await asyncio.to_thread(run)
        except Exception as exc:  # noqa: BLE001 - reported to the hook, which fails closed
            store.log("checkpoint_failed", str(exc), approval_id=body.approval_id)
            return {"ok": False, "enabled": True, "error": str(exc)}
        secs = round(time.monotonic() - t0, 1)
        store.log("checkpoint", f"Checkpoint {name} ready in {secs}s before: {body.label[:120]}",
                  checkpoint_id=cp["id"], checkpoint_name=name, approval_id=body.approval_id, secs=secs)
        return {"ok": True, "enabled": True, "id": cp["id"], "name": name, "secs": secs}

    @app.post("/agent/call", dependencies=[Depends(require_agent)])
    async def call_owner(body: CallIn, request: Request) -> dict[str, Any]:
        """The agent wants to tell the owner something (finished, stuck, found a problem)."""
        store.log("call_reason", body.message)
        live = live_calls[-1] if live_calls else None  # pick before any await
        if live is not None:
            try:
                await live.announce("The agent has an update for the owner. Tell them in one or two sentences.",
                                    agent_text=redact(body.message))
                return {"ok": True, "delivered": "live_call"}
            except Exception as exc:  # noqa: BLE001 - they hung up mid-announce: fall back to dialling
                log.warning("live-call update failed: %s", exc)
        if not settings.owner_numbers:
            return {"ok": True, "delivered": "log_only"}
        base = settings.public_url or str(request.base_url).rstrip("/")
        try:
            call_sid = await place_call(settings.owner_numbers[0], f"{base}/twilio/voice?reason=update")
        except Exception as exc:
            log.exception("outbound call failed")
            store.log("call_failed", f"Could not place call: {exc}")
            return {"ok": False, "delivered": "log_only"}
        store.log("call_placed", f"Calling {settings.owner_numbers[0]} with an update", call_sid=call_sid)
        return {"ok": True, "delivered": "call", "call_sid": call_sid}

    @app.get("/agent/approvals/{approval_id}", dependencies=[Depends(require_agent)])
    async def get_approval(approval_id: str, wait: float = 0) -> dict[str, Any]:
        """Long-poll: with ?wait=N, hold up to N seconds for a decision."""
        deadline = time.time() + min(wait, 55)
        while True:
            approval = store.get_approval(approval_id)
            if approval is None:
                raise HTTPException(404, "no such approval")
            if approval.status != "pending" or time.time() >= deadline:
                return {"ok": True, "approval": approval.to_dict()}
            await asyncio.sleep(0.5)

    @app.post("/agent/approvals/{approval_id}/expire", dependencies=[Depends(require_agent)])
    async def expire_approval(approval_id: str) -> dict[str, Any]:
        store.expire(approval_id)
        return {"ok": True}

    @app.get("/agent/inbox", dependencies=[Depends(require_agent)])
    async def inbox() -> dict[str, Any]:
        return {"ok": True, "instructions": store.take_instructions()}

    @app.get("/agent/status", dependencies=[Depends(require_agent)])
    async def status(events: int = 20) -> dict[str, Any]:
        return {"ok": True, **store.status_snapshot(events=events)}

    return app


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    settings = Settings.from_env()
    uvicorn.run(create_app(settings), host="0.0.0.0", port=8080, proxy_headers=True, forwarded_allow_ips="*")


if __name__ == "__main__":
    main()
