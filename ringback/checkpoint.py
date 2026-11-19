"""Checkpoint this runtime through the Runta API before an approved irreversible step.

Runs inside the runtime. The request carries no token: a Runta secret stub on
api.runta.com injects it, so the token never exists inside the runtime.
(Trade-off: anything in the runtime can use that stub, including the agent; see docs/SECURITY.md.)
"""

from __future__ import annotations

import time
from typing import Any

import httpx


class CheckpointError(Exception):
    pass


def create(
    client: httpx.Client,
    runtime_id: str,
    name: str,
    *,
    timeout: float = 240,
    poll: float = 2,
    sleep=time.sleep,
    on_created=None,
) -> dict[str, Any]:
    """Create a full (disk + memory) checkpoint and wait until Runta says it's ready."""
    r = client.post(f"/v2/runtimes/{runtime_id}/checkpoints", json={"name": name, "kind": "full"})
    if r.status_code >= 400:
        raise CheckpointError(f"create failed: HTTP {r.status_code} {r.text[:200]}")
    cp = r.json()["data"]
    if on_created:
        on_created(cp["id"])
    deadline = time.monotonic() + timeout
    while cp["state"] == "creating":
        if time.monotonic() > deadline:
            raise CheckpointError(f"checkpoint {cp['id']} not ready after {timeout:.0f}s")
        sleep(poll)
        r = client.get(f"/v2/checkpoints/{cp['id']}")
        if r.status_code >= 400:
            raise CheckpointError(f"status check failed: HTTP {r.status_code}")
        cp = r.json()["data"]
    if cp["state"] != "ready":
        raise CheckpointError(f"checkpoint {cp['id']} {cp['state']}: {cp.get('safe_error_code') or 'no reason given'}")
    return cp
