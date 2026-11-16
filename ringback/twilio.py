"""Small, dependency-free Twilio helpers: signature check, TwiML, outbound call."""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Mapping
from xml.sax.saxutils import escape

import httpx


def quoteattr(value: str) -> str:
    return '"' + escape(value, {'"': "&quot;"}) + '"'


def compute_signature(auth_token: str, url: str, params: Mapping[str, str]) -> str:
    """Twilio's X-Twilio-Signature: base64(HMAC-SHA1(token, url + sorted k+v))."""
    payload = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    digest = hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def valid_signature(auth_token: str, url: str, params: Mapping[str, str], signature: str) -> bool:
    if not auth_token or not signature:
        return False
    return hmac.compare_digest(compute_signature(auth_token, url, params), signature)


def stream_twiml(ws_url: str, parameters: Mapping[str, str]) -> str:
    """Connect the call's audio to our websocket, passing custom parameters."""
    params = "".join(
        f"<Parameter name={quoteattr(k)} value={quoteattr(v)} />" for k, v in parameters.items()
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<Response><Connect><Stream url={quoteattr(ws_url)}>{params}</Stream></Connect></Response>"
    )


def reject_twiml(message: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<Response><Say>{escape(message)}</Say><Hangup/></Response>"
    )


async def place_call(
    *,
    account_sid: str,
    auth_token: str,
    from_number: str,
    to_number: str,
    url: str,
    client: httpx.AsyncClient | None = None,
) -> str:
    """Start an outbound call; Twilio fetches TwiML from `url` once answered.

    Returns the CallSid.
    """
    endpoint = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Calls.json"
    data = {"To": to_number, "From": from_number, "Url": url, "Method": "POST"}
    own = client is None
    client = client or httpx.AsyncClient(timeout=15)
    try:
        resp = await client.post(endpoint, data=data, auth=(account_sid, auth_token))
        resp.raise_for_status()
        return resp.json()["sid"]
    finally:
        if own:
            await client.aclose()
