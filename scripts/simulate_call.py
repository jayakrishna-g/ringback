"""Simulate a phone call end to end, without the phone network.

Plays Twilio's part against the *deployed* server on Runta:
  1. POSTs a correctly signed /twilio/voice webhook (as Twilio would on an inbound call)
  2. opens the returned wss:// Media Stream with the one-time token
  3. speaks caller lines (OpenAI TTS -> 8 kHz mu-law, streamed in real time, 20 ms frames)
  4. records what the voice line says back to call.wav and prints the agent's event log

Everything after Twilio is real: Runta ingress, the server, OpenAI Realtime (key injected
by the Runta secret stub), tools, SQLite. What's NOT exercised: the PSTN leg and Twilio itself.

  python scripts/simulate_call.py                 # default script: status, then approve
  python scripts/simulate_call.py "Hi, what's it doing?" "Tell it to skip staging."
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import sys
import time
import wave
from pathlib import Path

import httpx
import websockets

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))
from deploy import NAME, REMOTE, find_url, load_dotenv, runta_cli, sdk  # noqa: E402

from ringback.twilio import compute_signature  # noqa: E402

FRAME = 160  # 20 ms of 8 kHz mu-law
DEFAULT_LINES = [
    "Hey, what is the agent doing right now?",
    "Yes. I approve dropping the legacy email column.",
    "Yes, confirmed.",  # the line reads irreversible actions back and asks again
]


# ---- G.711 mu-law (audioop is gone in Python 3.13) -------------------------
def _ulaw_encode(sample: int) -> int:
    BIAS, CLIP = 0x84, 32635
    sign = 0x80 if sample < 0 else 0
    sample = min(abs(sample), CLIP) + BIAS
    exponent = 7
    for exp_mask in (0x4000, 0x2000, 0x1000, 0x800, 0x400, 0x200, 0x100):
        if sample & exp_mask:
            break
        exponent -= 1
    mantissa = (sample >> (exponent + 3)) & 0x0F
    return ~(sign | (exponent << 4) | mantissa) & 0xFF


def _ulaw_decode(byte: int) -> int:
    byte = ~byte & 0xFF
    sign, exponent, mantissa = byte & 0x80, (byte >> 4) & 0x07, byte & 0x0F
    sample = (((mantissa << 3) + 0x84) << exponent) - 0x84
    return -sample if sign else sample


ULAW_DECODE = [_ulaw_decode(b) for b in range(256)]


def pcm24k_to_ulaw8k(pcm: bytes) -> bytes:
    samples = memoryview(pcm).cast("h")
    out = bytearray()
    for i in range(0, len(samples) - 2, 3):  # 24k -> 8k: average each group of 3
        out.append(_ulaw_encode((samples[i] + samples[i + 1] + samples[i + 2]) // 3))
    return bytes(out)


def ulaw_to_pcm16(data: bytes) -> bytes:
    import array

    return array.array("h", (ULAW_DECODE[b] for b in data)).tobytes()


def tts(text: str, key: str) -> bytes:
    r = httpx.post(
        "https://api.openai.com/v1/audio/speech",
        headers={"Authorization": f"Bearer {key}"},
        json={"model": "gpt-4o-mini-tts", "voice": "alloy", "input": text, "response_format": "pcm"},
        timeout=60,
    )
    r.raise_for_status()
    return pcm24k_to_ulaw8k(r.content)


# ---- the call ---------------------------------------------------------------
class SimCall:
    def __init__(self, ws):
        self.ws = ws
        self.received = bytearray()  # everything the line said, mu-law
        self.timeline = bytearray()  # both sides interleaved, for call.wav
        self.last_audio = 0.0
        self.media_ts = 0

    async def reader(self) -> None:
        async for raw in self.ws:
            msg = json.loads(raw)
            if msg["event"] == "media":
                chunk = base64.b64decode(msg["media"]["payload"])
                self.received += chunk
                self.timeline += chunk
                self.last_audio = time.monotonic()
            elif msg["event"] == "mark":  # Twilio echoes marks once audio has played
                await self.ws.send(json.dumps({"event": "mark", "streamSid": "MZsim", "mark": msg["mark"]}))
            elif msg["event"] == "clear":
                print("   [line cleared its playback: barge-in]")

    async def send_audio(self, ulaw: bytes) -> None:
        for i in range(0, len(ulaw), FRAME):
            frame = ulaw[i:i + FRAME].ljust(FRAME, b"\xff")
            self.media_ts += 20
            await self.ws.send(json.dumps({"event": "media", "streamSid": "MZsim",
                                           "media": {"payload": base64.b64encode(frame).decode(),
                                                     "timestamp": str(self.media_ts)}}))
            self.timeline += frame
            await asyncio.sleep(0.02)

    async def wait_for_line(self, quiet: float = 1.5, timeout: float = 30) -> float:
        """Send silence until the line has spoken and then gone quiet. Returns seconds to first audio."""
        start, before = time.monotonic(), len(self.received)
        first = None
        while time.monotonic() - start < timeout:
            await self.send_audio(b"\xff" * FRAME * 5)  # 100 ms silence keeps the stream realistic
            if len(self.received) > before and first is None:
                first = time.monotonic() - start
            if first is not None and time.monotonic() - self.last_audio > quiet:
                break
        return first if first is not None else -1


async def run(lines: list[str]) -> int:
    load_dotenv()
    key = os.environ["OPENAI_API_KEY"]
    auth_token = os.environ["TWILIO_AUTH_TOKEN"]
    url = find_url(runta_cli("ports", "ls", NAME))
    rt = sdk().runtimes.get(NAME)
    renv = dict(l.split("=", 1) for l in rt.files.read(f"{REMOTE}/runtime.env").decode().splitlines() if "=" in l)
    caller = renv["RINGBACK_OWNER_NUMBERS"].split(",")[0]
    agent = httpx.Client(base_url=url, headers={"Authorization": f"Bearer {renv['RINGBACK_AGENT_TOKEN']}"}, timeout=30)

    print("1. seeding agent state (task + pending approval, no outbound call)")
    agent.post("/agent/task", json={"task": "Migrate users table to v2 and remove legacy_email"}).raise_for_status()
    agent.post("/agent/log", json={"message": "Backfill verified: 1,204,311 rows, checksums match"}).raise_for_status()
    ap = agent.post("/agent/approvals", json={"question": "Drop column legacy_email from users on production",
                                              "context": "Irreversible; snapshot exists", "call": False}).json()["approval"]

    print("2. synthesising caller lines with OpenAI TTS")
    audio = [tts(line, key) for line in lines]

    print("3. signed webhook -> TwiML")
    form = {"CallSid": f"CAsim{int(time.time())}", "From": caller, "To": os.environ.get("TWILIO_NUMBER", ""),
            "Direction": "inbound", "AccountSid": os.environ.get("TWILIO_ACCOUNT_SID", "")}
    t0 = time.monotonic()
    r = httpx.post(f"{url}/twilio/voice", data=form,
                   headers={"X-Twilio-Signature": compute_signature(auth_token, f"{url}/twilio/voice", form)})
    print(f"   HTTP {r.status_code} in {time.monotonic() - t0:.2f}s")
    r.raise_for_status()
    ws_url = re.search(r'Stream url="([^"]+)"', r.text).group(1)
    token = re.search(r'name="token" value="([^"]+)"', r.text).group(1)

    print("4. media stream")
    async with websockets.connect(ws_url) as ws:
        call = SimCall(ws)
        reader = asyncio.create_task(call.reader())
        await ws.send(json.dumps({"event": "connected", "protocol": "Call", "version": "1.0.0"}))
        await ws.send(json.dumps({"event": "start", "sequenceNumber": "1", "streamSid": "MZsim",
                                  "start": {"streamSid": "MZsim", "callSid": form["CallSid"],
                                            "customParameters": {"token": token},
                                            "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1}}}))
        first = await call.wait_for_line()
        print(f"   greeting: first audio after {first:.2f}s, {len(call.received) / 8000:.1f}s of speech")
        for line, ulaw in zip(lines, audio, strict=False):
            print(f"   caller: {line}")
            before = len(call.received)
            await call.send_audio(ulaw)
            first = await call.wait_for_line()
            print(f"   line answered: first audio {first:.2f}s after caller stopped, "
                  f"{(len(call.received) - before) / 8000:.1f}s of speech")
        await ws.send(json.dumps({"event": "stop", "streamSid": "MZsim"}))
        reader.cancel()

    out = Path("call.wav")
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000)
        w.writeframes(ulaw_to_pcm16(bytes(call.timeline)))
    print(f"5. recording: {out.resolve()}")

    await asyncio.sleep(1.5)  # let call_ended land
    final = agent.get(f"/agent/approvals/{ap['id']}").json()["approval"]
    print(f"\napproval {ap['id']}: {final['status']} (decided_by={final['decided_by']})")
    print("\nagent event log:")
    for e in agent.get("/agent/status", params={"events": 30}).json()["recent_events"]:
        print(f"  {e['kind']:20} {e['message'][:110]}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run(sys.argv[1:] or DEFAULT_LINES)))
