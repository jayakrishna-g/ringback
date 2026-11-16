"""Settings, all from environment variables (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _list(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


@dataclass
class Settings:
    # Public https base URL of this server, e.g. the Runta published-port URL.
    public_url: str = ""
    db_path: str = "/workspace/ringback/ringback.db"

    # Twilio
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""  # needed to verify inbound webhook signatures
    twilio_number: str = ""  # our Twilio number, E.164
    owner_numbers: list[str] = field(default_factory=list)  # who may call in / gets called
    verify_twilio_signature: bool = True

    # OpenAI Realtime
    openai_api_key: str = ""  # leave empty on Runta: a secret stub injects it
    realtime_model: str = "gpt-realtime"
    realtime_voice: str = "marin"
    realtime_url: str = "wss://api.openai.com/v1/realtime"

    # Shared secret for agent-side HTTP routes (/agent/*)
    agent_token: str = ""

    # Checkpoint the runtime (Runta API, token via secret stub) before an approved risky step.
    runtime_id: str = ""  # empty = checkpoints off
    runta_api: str = "https://api.runta.com"

    # Claude Code tasks started by phone. Empty project_dir = feature off.
    project_dir: str = ""
    env_file: str = "/workspace/ringback/runtime.env"

    @classmethod
    def from_env(cls) -> Settings:
        e = os.environ.get
        return cls(
            public_url=(e("RINGBACK_PUBLIC_URL") or "").rstrip("/"),
            db_path=e("RINGBACK_DB", cls.db_path),
            twilio_account_sid=e("TWILIO_ACCOUNT_SID", ""),
            twilio_auth_token=e("TWILIO_AUTH_TOKEN", ""),
            twilio_number=e("TWILIO_NUMBER", ""),
            owner_numbers=_list(e("RINGBACK_OWNER_NUMBERS")),
            verify_twilio_signature=e("RINGBACK_VERIFY_SIGNATURE", "1") != "0",
            openai_api_key=e("OPENAI_API_KEY", ""),
            realtime_model=e("RINGBACK_REALTIME_MODEL", cls.realtime_model),
            realtime_voice=e("RINGBACK_VOICE", cls.realtime_voice),
            realtime_url=e("RINGBACK_REALTIME_URL", cls.realtime_url),
            agent_token=e("RINGBACK_AGENT_TOKEN", ""),
            project_dir=e("RINGBACK_PROJECT_DIR", ""),
            runtime_id=e("RINGBACK_RUNTIME_ID", ""),
            runta_api=e("RINGBACK_RUNTA_API", cls.runta_api),
            env_file=e("RINGBACK_ENV_FILE", cls.env_file),
        )
