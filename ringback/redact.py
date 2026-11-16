"""Strip credentials from text before it's stored or shown to the voice model.

Agent commands get logged and read back on calls, so `curl -H 'Authorization: Bearer ...'`
must not land in the database, the Realtime prompt, or the owner's ear.

Two rules, both from review:
  * Only mask things that are *shaped* like credentials: `key=value` / `key: value` where the
    key names a secret, `--password x`-style flags, and well-known token formats. Never mask a
    bare word just because it follows "token": `rm -rf token /production` must be read out
    in full, or the owner approves a command whose target was hidden.
  * Never edit serialized JSON. Structured data is redacted value by value (`redact_data`).
Best effort: it covers the usual shapes of keys, not every possible secret.
"""

from __future__ import annotations

import re
from typing import Any

MASK = "[REDACTED]"
_SECRET_WORD = r"(?:token|secret|password|passwd|pwd|api[-_]?key|access[-_]?key|private[-_]?key|authorization|credential)"
_SECRET_KEY = re.compile(rf"(?i){_SECRET_WORD}")

_RULES: list[tuple[re.Pattern[str], str]] = [
    # Quoted values first, whole: `password='hello world'`, `--password "a b"` (else only the first word goes)
    (re.compile(rf"(?i)(\b[A-Za-z0-9_.-]*{_SECRET_WORD}[A-Za-z0-9_-]*[\"']?\s*[:=]\s*|(?<!\S)--?[A-Za-z0-9-]*{_SECRET_WORD}"
                rf"[A-Za-z0-9-]*(?:\s+|=))(['\"])(?:(?!\2).)+\2"), rf"\1\2{MASK}\2"),
    # KEY=value / "key": value / Authorization: Bearer x   (key may be TWILIO_AUTH_TOKEN, x-api-key, ...)
    (re.compile(rf"(?i)\b([A-Za-z0-9_.-]*{_SECRET_WORD}[A-Za-z0-9_-]*)([\"']?\s*[:=]\s*)(bearer\s+|basic\s+)?([\"']?)[^\s\"'&,;}}]+"),
     rf"\1\2\3\4{MASK}"),
    # --password x / --token=x / -H style flags carrying a secret name
    (re.compile(rf"(?i)(--?[A-Za-z0-9-]*{_SECRET_WORD}[A-Za-z0-9-]*)(\s+|=)([\"']?)[^\s\"']+"), rf"\1\2\3{MASK}"),
    (re.compile(r"(?i)(\s(?:-u|--user)\s+[\"']?[^\s:\"']+:)[^\s\"']+"), rf"\1{MASK}"),   # curl -u user:pass
    (re.compile(r"(\bmysql(?:dump|admin)?\b[^;&|\n]*\s-p)(?!\s)[^\s\"']+"), rf"\1{MASK}"),     # mysql -phunter2
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"), f"Bearer {MASK}"),
    (re.compile(r"\b(?:sk|rk|pk)-[A-Za-z0-9_-]{16,}"), MASK),                           # OpenAI/Anthropic/Stripe-style
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), MASK),                                         # AWS access key id
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"), MASK),                               # GitHub tokens
    (re.compile(r"(?i)(://[^/\s:@]+:)[^@\s/]+@"), rf"\1{MASK}@"),                        # user:password@ in URLs
]


def redact(text: str) -> str:
    if not text:
        return text
    for pattern, repl in _RULES:
        text = pattern.sub(repl, text)
    return text


def redact_data(value: Any) -> Any:
    """Redact structured data before it's serialized: secret-named keys lose their value."""
    if isinstance(value, dict):
        return {k: (MASK if isinstance(k, str) and _SECRET_KEY.search(k) and v not in (None, "")
                    else redact_data(v)) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [redact_data(v) for v in value]
    if isinstance(value, str):
        return redact(value)
    return value
