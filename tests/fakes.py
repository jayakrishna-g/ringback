from __future__ import annotations

import asyncio
from typing import Any


class FakeChannel:
    """In-memory Channel: tests push inbound messages, inspect `sent`."""

    def __init__(self) -> None:
        self.inbound: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    async def send(self, msg: dict[str, Any]) -> None:
        self.sent.append(msg)

    async def recv(self) -> dict[str, Any] | None:
        return await self.inbound.get()

    async def close(self) -> None:
        self.closed = True

    def push(self, msg: dict[str, Any] | None) -> None:
        self.inbound.put_nowait(msg)

    def of_type(self, key: str, value: str) -> list[dict[str, Any]]:
        return [m for m in self.sent if m.get(key) == value]
