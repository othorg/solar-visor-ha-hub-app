"""Read-only access to the Home Assistant recorder for the history of any plant (plan ha-addon P3 step 1).

Two kinds of data, kept apart on purpose (P3 detail plan §1):
- raw states (``history/history_during_period``): every reported change, only inside the recorder's keep window;
- statistics (``recorder/statistics_during_period``): 5-minute rows for about the same window, hourly rows for good.

Requests are split by time so that no answer comes near the WebSocket frame limit of the shared live session
(16 MiB; measured: a day of ten busy sensors is about 1 MB, see P0 finding §6). ``read`` is any coroutine that sends
one message and returns HA's ``result`` – ``HaClient.read`` in the hub, ``HaConnection.read`` for the command line.
"""

from __future__ import annotations

import json
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import websockets

from .transport import GuardedHaSocket

Read = Callable[[dict[str, Any]], Awaitable[Any]]
RAW_CHUNK = timedelta(hours=6)
STAT_CHUNK = {"5minute": timedelta(days=7), "hour": timedelta(days=31), "day": timedelta(days=366)}
STAT_UNITS = {"power": "W", "energy": "Wh"}     # always: HA keeps some power statistics in kW (P0)


@dataclass(frozen=True)
class RawState:
    at: datetime
    state: str | None
    initial: bool = False       # HA's state at the start of the request: its time is the request start, not the change


@dataclass(frozen=True)
class StatRow:
    start: datetime
    end: datetime
    mean: float | None = None
    min: float | None = None
    max: float | None = None
    sum: float | None = None
    state: float | None = None
    change: float | None = None


def _iso(t: datetime) -> str:
    return t.astimezone(UTC).isoformat()


def _ts(value: Any) -> datetime | None:
    if isinstance(value, (int, float)) and math.isfinite(value):
        return datetime.fromtimestamp(float(value), tz=UTC)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
        except ValueError:
            return None
    return None


def _chunks(start: datetime, end: datetime, size: timedelta) -> list[tuple[datetime, datetime]]:
    out, t = [], start
    while t < end:
        out.append((t, min(t + size, end)))
        t = out[-1][1]
    return out


async def raw_states(read: Read, entity_id: str, start: datetime, end: datetime,
                     chunk: timedelta = RAW_CHUNK) -> list[RawState]:
    """All states of one entity in [start, end), oldest first. The first element is HA's state at ``start``
    (``initial=True``) if HA knows one; later chunks do not repeat it."""
    out: list[RawState] = []
    for i, (a, b) in enumerate(_chunks(start, end, chunk)):
        result = await read({"type": "history/history_during_period", "start_time": _iso(a), "end_time": _iso(b),
                             "entity_ids": [entity_id], "minimal_response": True, "no_attributes": True,
                             "significant_changes_only": False, "include_start_time_state": i == 0})
        rows = (result or {}).get(entity_id) if isinstance(result, dict) else None
        for j, row in enumerate(rows or []):
            if not isinstance(row, dict):
                continue
            at = _ts(row.get("lu")) or _ts(row.get("lc")) or _ts(row.get("last_updated"))
            if at is None:
                continue
            state = row.get("s", row.get("state"))
            initial = i == 0 and j == 0 and at <= a
            out.append(RawState(at=max(at, a) if initial else at, state=None if state is None else str(state),
                                initial=initial))
    return out


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


async def statistics(read: Read, statistic_ids: list[str], start: datetime, end: datetime, period: str,
                     types: tuple[str, ...] = ("mean", "min", "max", "sum", "state", "change")) -> dict[str, list[StatRow]]:
    """Statistics rows per id in [start, end), split by time; units always W and Wh."""
    out: dict[str, list[StatRow]] = {sid: [] for sid in statistic_ids}
    if not statistic_ids:
        return out
    for a, b in _chunks(start, end, STAT_CHUNK[period]):
        result = await read({"type": "recorder/statistics_during_period", "start_time": _iso(a), "end_time": _iso(b),
                             "statistic_ids": list(statistic_ids), "period": period, "types": list(types),
                             "units": STAT_UNITS})
        for sid, rows in (result or {}).items() if isinstance(result, dict) else ():
            if sid not in out or not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                s = _ts(row.get("start", 0) / 1000 if isinstance(row.get("start"), (int, float)) else row.get("start"))
                e = _ts(row.get("end", 0) / 1000 if isinstance(row.get("end"), (int, float)) else row.get("end"))
                if s is None or e is None:
                    continue
                out[sid].append(StatRow(start=s, end=e, **{k: _num(row.get(k)) for k in
                                                           ("mean", "min", "max", "sum", "state", "change")}))
    return out


async def statistic_ids(read: Read) -> dict[str, dict[str, Any]]:
    """Statistic id → metadata (has_mean, has_sum, statistics_unit_of_measurement, unit_class …)."""
    result = await read({"type": "recorder/list_statistic_ids"})
    return {r["statistic_id"]: r for r in result or [] if isinstance(r, dict) and isinstance(r.get("statistic_id"), str)}


async def oldest_statistic(read: Read, statistic_ids: list[str], now: datetime, period: str = "5minute",
                           max_back: timedelta = timedelta(days=45)) -> datetime | None:
    """Start of the oldest row of any of the statistics within ``max_back`` – the recorder's keep window of 5-minute
    rows. A binary search over one-hour probes (about eleven small requests instead of weeks of rows); a hole in the
    data can only move the result later, i.e. towards the hourly tier, which is always correct. None if the last hour
    has no row."""
    async def first(t: datetime) -> datetime | None:
        rows = await statistics(read, statistic_ids, t, t + timedelta(hours=1), period, ("mean", "sum"))
        return min((r.start for rs in rows.values() for r in rs if r.mean is not None or r.sum is not None), default=None)

    hours = int(max_back / timedelta(hours=1))
    lo, hi = 1, hours                            # invariant: the probe lo hours back has a row
    found = await first(now - timedelta(hours=1))
    if found is None:
        return None
    while lo < hi:
        mid = (lo + hi + 1) // 2
        hit = await first(now - timedelta(hours=mid))
        if hit is not None:
            lo, found = mid, hit
        else:
            hi = mid - 1
    return found


class HaConnection:
    """A short-lived read-only connection for the command line (the hub itself reads over its live session)."""

    def __init__(self, ws_url: str, token: Callable[[], str], timeout: float = 30.0) -> None:
        self._url, self._token, self._timeout = ws_url, token, timeout
        self._ws: Any = None
        self._sock: GuardedHaSocket | None = None

    async def __aenter__(self) -> HaConnection:
        self._ws = await websockets.connect(self._url, max_size=16 * 2**20, open_timeout=10)
        self._sock = GuardedHaSocket(self._ws.send)
        if json.loads(await self._ws.recv()).get("type") != "auth_required":
            raise ConnectionError("unexpected handshake")
        await self._sock.send({"type": "auth", "access_token": self._token()})
        if json.loads(await self._ws.recv()).get("type") != "auth_ok":
            raise ConnectionError("authentication rejected")
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._ws is not None:
            await self._ws.close()

    async def read(self, message: dict[str, Any]) -> Any:
        from .ha_client import HaReadError
        assert self._sock is not None and self._ws is not None
        mid = await self._sock.send(message)
        while True:
            reply = json.loads(await asyncio_wait(self._ws.recv(), self._timeout))
            if reply.get("id") == mid and reply.get("type") == "result":
                break
        if not reply.get("success"):
            error = reply.get("error") or {}
            raise HaReadError(str(error.get("code") or "unknown"), str(error.get("message") or "")[:200])
        return reply.get("result")


async def asyncio_wait(awaitable: Any, timeout: float) -> Any:
    import asyncio
    return await asyncio.wait_for(awaitable, timeout)


async def probe(read: Read, entity_ids: list[str], now: datetime | None = None) -> dict[str, Any]:
    """Measurements the generic history relies on (P3 detail plan §9, T1–T4) – for ``cli history probe``."""
    now = now or datetime.now(UTC)
    report: dict[str, Any] = {"entities": {}}
    meta = await statistic_ids(read)
    day_start = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    for eid in entity_ids:
        info: dict[str, Any] = {"statistic": {k: meta[eid].get(k) for k in
                                              ("has_mean", "has_sum", "statistics_unit_of_measurement", "unit_class")}
                                if eid in meta else None}
        rows = await raw_states(read, eid, day_start, day_start + RAW_CHUNK)
        info["raw_rows_6h"] = len(rows)
        info["initial_state_at_request_start"] = bool(rows) and rows[0].initial and rows[0].at == day_start   # T1
        size = len(json.dumps([r.__dict__ for r in rows], default=str))
        info["raw_bytes_6h_approx"] = size                                                               # T3
        oldest = await raw_states(read, eid, now - timedelta(days=60), now - timedelta(days=60) + timedelta(hours=1))
        info["raw_reaches_60_days"] = any(not r.initial for r in oldest)
        report["entities"][eid] = info
    # T4: keep window of raw states and of 5-minute statistics for the first entity with a statistic
    with_stat = next((e for e in entity_ids if e in meta), None)
    if with_stat:
        five = await statistics(read, [with_stat], now - timedelta(days=40), now, "5minute", ("mean", "sum"))
        report["five_minute_since"] = five[with_stat][0].start.isoformat() if five[with_stat] else None
        hour = await statistics(read, [with_stat], now - timedelta(days=400), now, "hour", ("mean", "sum"))
        report["hour_since"] = hour[with_stat][0].start.isoformat() if hour[with_stat] else None
        lo, hi = 1, 60                                       # oldest day with raw states (binary search, ≤ 6 requests)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            t = now - timedelta(days=mid)
            rows = await raw_states(read, with_stat, t, t + timedelta(hours=6))
            lo, hi = (mid, hi) if any(not r.initial for r in rows) else (lo, mid - 1)
        report["raw_window_days"] = lo
    return report
