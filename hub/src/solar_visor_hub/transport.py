"""Single choke point for all outbound network traffic (Codex P0 finding 2).

Every Home Assistant WebSocket message and every InfluxDB request of the hub must go
through these classes, which enforce ``ops_allowlist`` before anything leaves the process.
Concrete network I/O is injected (``sender``/``http_get``) so the guard is testable and the
clients built in P1/P3 cannot bypass it.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from .ops_allowlist import check_ha_ws, check_influx

WsSender = Callable[[str], Awaitable[None]]
HttpGet = Callable[[str, dict[str, str]], Awaitable[Any]]


class GuardedHaSocket:
    """Wraps a raw WebSocket send function; only allow-listed message types pass."""

    def __init__(self, sender: WsSender) -> None:
        self._send = sender
        self._next_id = 1

    @property
    def next_id(self) -> int:
        """Id the next non-auth message will receive (lets callers register a pending result first)."""
        return self._next_id

    async def send(self, message: dict[str, Any]) -> int | None:
        msg_type = message.get("type", "")
        check_ha_ws(msg_type)
        payload = dict(message)
        msg_id: int | None = None
        if msg_type != "auth":
            msg_id = self._next_id
            self._next_id += 1
            payload["id"] = msg_id
        await self._send(json.dumps(payload))
        return msg_id


class GuardedInflux:
    """Wraps a raw HTTP GET function; only read statements on allowed paths pass."""

    def __init__(self, http_get: HttpGet, database: str, retention_policy: str | None = None) -> None:
        self._get = http_get
        self._db = database
        self._rp = retention_policy

    async def query(self, statement: str, params: dict[str, Any] | None = None) -> Any:
        check_influx("/query", statement)
        query: dict[str, str] = {"db": self._db, "q": statement, "epoch": "ms"}
        if self._rp:
            query["rp"] = self._rp
        if params:
            query["params"] = json.dumps(params)
        return await self._get("/query", query)

    async def health(self) -> Any:
        check_influx("/health")
        return await self._get("/health", {})
