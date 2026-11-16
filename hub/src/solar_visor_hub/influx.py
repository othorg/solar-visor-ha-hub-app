"""Minimal InfluxDB (v2 with InfluxQL v1 compatibility) read adapter (plan §2.1/§3.4).

All requests pass ``GuardedInflux`` (allow-list: ``/query`` with SELECT/SHOW, ``/health``, ``/ping``).
P2 uses it for the connection test only; the history engine follows in P3.
"""

from __future__ import annotations

import asyncio
import re
import ssl
from datetime import UTC, datetime, timedelta
from collections.abc import Callable
from typing import Any

import httpx

from .influx_config import InfluxConfig, InfluxConnection
from .transport import GuardedInflux

MEASUREMENT = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")   # HA writes measurement = entity_id incl. domain


class InfluxError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class InfluxHttp:
    def __init__(self, base_url: str, token_provider: Callable[[], str], timeout: float = 5.0,
                 transport: httpx.AsyncBaseTransport | None = None, reuse: bool = False) -> None:
        self._base = base_url.rstrip("/")
        self._token = token_provider
        self._timeout = httpx.Timeout(timeout)
        self._transport = transport   # tests inject a mock transport
        self._client: httpx.AsyncClient | None = None
        self._reuse = reuse           # long-lived history client: one connection pool (Codex P3 finding 7)

    async def get(self, path: str, params: dict[str, str]) -> Any:
        headers = {} if path in ("/health", "/ping") else {"Authorization": f"Token {self._token()}"}
        if self._reuse:
            if self._client is None:
                self._client = httpx.AsyncClient(timeout=self._timeout, transport=self._transport)
            r = await self._client.get(self._base + path, params=params, headers=headers)
        else:
            async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
                r = await client.get(self._base + path, params=params, headers=headers)
        if r.status_code >= 400:
            raise InfluxError(r.status_code, _error_text(r))
        body = r.json() if r.content else {}
        for res in (body.get("results") or []) if isinstance(body, dict) else []:
            if "error" in res:
                raise InfluxError(200, str(res["error"]))
        return body


    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def _error_text(r: httpx.Response) -> str:
    try:
        data = r.json()
        return str(data.get("error") or data.get("message") or r.status_code)
    except ValueError:
        return str(r.status_code)


async def probe_influx(url: str, database: str, retention_policy: str | None, token_provider: Callable[[], str],
                       sample_measurement: str, transport: httpx.AsyncBaseTransport | None = None) -> tuple[str, str]:
    """Connection test: reachable, token accepted, database present, data of a mapped entity readable."""
    try:
        token_provider()
    except (OSError, ValueError):
        return "token_missing", "Kein Influx-Token auf dem Hub hinterlegt"
    if not MEASUREMENT.match(sample_measurement):
        return "config", "Ungültiger Messreihen-Name"
    influx = GuardedInflux(InfluxHttp(url, token_provider, transport=transport).get, database, retention_policy)
    try:
        await influx.health()
        await influx.query("SHOW MEASUREMENTS LIMIT 1")
        # identifier from the validated mapping (no user input); InfluxQL cannot bind identifiers
        res = await influx.query(f'SELECT last("value") FROM "{sample_measurement}" WHERE time > now() - 1d')
    except httpx.TimeoutException:
        return "timeout", "Zeitüberschreitung"
    except httpx.ConnectError as exc:
        if isinstance(exc.__cause__, ssl.SSLError) or "SSL" in str(exc):
            return "tls", "TLS-Fehler"
        if "nodename" in str(exc) or "Name or service" in str(exc):
            return "dns", "Hostname nicht auflösbar"
        return "connect", "Verbindung abgelehnt"
    except InfluxError as exc:
        if exc.status in (401, 403):
            return "auth_rejected", "InfluxDB lehnt den Token ab"
        if "database not found" in str(exc).lower() or exc.status == 404:
            return "database_missing", "Datenbank/Bucket nicht gefunden"
        return "error", "InfluxDB meldet einen Fehler"
    except httpx.HTTPError:            # read errors, protocol errors, resets after connect
        return "connect", "Verbindung während der Abfrage unterbrochen"
    except ValueError:                 # malformed JSON (e.g. a proxy error page)
        return "error", "Unerwartete Antwort (kein InfluxDB-JSON)"
    series = ((res.get("results") or [{}])[0].get("series") or []) if isinstance(res, dict) else []
    if not series:
        return "no_data", f"Erreichbar, aber keine Daten der letzten 24 h für {sample_measurement}"
    return "ok", "InfluxDB erreichbar, Token gültig, Daten vorhanden"


# --- history access (P3) -------------------------------------------------------------------------

def _rfc3339(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rows(res: Any) -> list[list[Any]]:
    series = ((res.get("results") or [{}])[0].get("series") or []) if isinstance(res, dict) else []
    return series[0].get("values") or [] if series else []


class InfluxHistory:
    """Predefined read templates only (plan §3.4): measurement names from the data epochs / mapping,
    checked against ``SHOW MEASUREMENTS``; time bounds as RFC3339 literals; ≤ 1 day of raw points per query;
    at most ``parallel`` concurrent queries."""

    def __init__(self, url: str, database: str, retention_policy: str | None, token_provider: Callable[[], str],
                 allowed: set[str], timeout: float = 15.0, parallel: int = 2,
                 transport: httpx.AsyncBaseTransport | None = None, connection: InfluxConnection | None = None) -> None:
        self._timeout, self._transport, self._connection = timeout, transport, connection
        self._key: tuple[InfluxConfig, int] | None = None
        self._http = InfluxHttp(url, token_provider, timeout, transport=transport, reuse=True)
        self._q = GuardedInflux(self._http.get, database, retention_policy)
        self._wanted = {m for m in allowed if MEASUREMENT.match(m)}
        self._present: set[str] | None = None
        self._present_lock = asyncio.Lock()      # concurrent first fetches share one SHOW MEASUREMENTS
        self._sem = asyncio.Semaphore(parallel)

    async def _follow(self) -> None:
        """Add-on: the connection set in the web interface applies at once – a new client for a new address, the token
        bound to that generation of the connection."""
        if self._connection is None:
            return
        cfg, generation = self._connection.config()
        if (cfg, generation) == self._key:
            return
        old, conn = self._http, self._connection
        self._http = InfluxHttp(cfg.url, lambda: conn.token(generation), self._timeout, transport=self._transport, reuse=True)
        self._q = GuardedInflux(self._http.get, cfg.database, cfg.retention_policy)
        if self._key is not None:
            self._present = None                 # another database may hold other measurements
        self._key = (cfg, generation)
        await old.aclose()

    async def _query(self, statement: str) -> Any:
        await self._follow()
        async with self._sem:
            return await self._q.query(statement)

    async def present(self) -> set[str]:
        """Allow-list = wanted measurements that exist (cached after the first successful check – per connection)."""
        await self._follow()
        if self._present is None:
            async with self._present_lock:
                if self._present is None:
                    names = {r[0] for r in _rows(await self._query("SHOW MEASUREMENTS"))}
                    self._present = self._wanted & names
        return self._present

    async def _check(self, measurement: str) -> bool:
        return measurement in await self.present()

    async def points(self, measurement: str, start: datetime, end: datetime) -> list[tuple[datetime, float | None, str | None]]:
        """Raw change points in [start, end), chunked by day. Binary sensors carry no unit."""
        if not await self._check(measurement):
            return []
        fields = '"value", "unit_of_measurement_str"' if measurement.startswith("sensor.") else '"value"'
        out: list[tuple[datetime, float | None, str | None]] = []
        t = start
        while t < end:
            chunk_end = min(end, t + timedelta(days=1))
            res = await self._query(f'SELECT {fields} FROM "{measurement}" WHERE time >= \'{_rfc3339(t)}\' '
                                    f'AND time < \'{_rfc3339(chunk_end)}\' ORDER BY time')
            for r in _rows(res):
                out.append((datetime.fromtimestamp(r[0] / 1000, UTC), r[1], r[2] if len(r) > 2 else None))
            t = chunk_end
        return out

    async def carry_in(self, measurement: str, before: datetime, lookback: timedelta):
        if not await self._check(measurement):
            return None
        fields = '"value", "unit_of_measurement_str"' if measurement.startswith("sensor.") else '"value"'
        rows = _rows(await self._query(
            f'SELECT {fields} FROM "{measurement}" WHERE time < \'{_rfc3339(before)}\' '
            f'AND time >= \'{_rfc3339(before - lookback)}\' ORDER BY time DESC LIMIT 1'))
        if not rows:
            return None
        r = rows[0]
        return datetime.fromtimestamp(r[0] / 1000, UTC), r[1], r[2] if len(r) > 2 else None

    async def first_after(self, measurement: str, after: datetime, lookahead: timedelta):
        """First point at/after ``after`` (closes a counter's last interval at the window end)."""
        if not await self._check(measurement):
            return None
        fields = '"value", "unit_of_measurement_str"' if measurement.startswith("sensor.") else '"value"'
        rows = _rows(await self._query(
            f'SELECT {fields} FROM "{measurement}" WHERE time >= \'{_rfc3339(after)}\' '
            f'AND time < \'{_rfc3339(after + lookahead)}\' ORDER BY time ASC LIMIT 1'))
        if not rows:
            return None
        r = rows[0]
        return datetime.fromtimestamp(r[0] / 1000, UTC), r[1], r[2] if len(r) > 2 else None

    async def max_in(self, measurement: str, start: datetime, end: datetime,
                     unit: str | None) -> tuple[datetime, float] | None:
        """Maximum raw value (and its time) in [start, end) for points with the given unit (None = any).
        Callers normalize per unit before comparing (Codex P3 finding 12)."""
        if not await self._check(measurement) or end <= start:
            return None
        unit_filter = f' AND "unit_of_measurement_str" = \'{unit}\'' if unit in ("Wh", "kWh", "MWh") else ""
        rows = _rows(await self._query(
            f'SELECT max("value") FROM "{measurement}" WHERE time >= \'{_rfc3339(start)}\' '
            f'AND time < \'{_rfc3339(end)}\'{unit_filter}'))
        if not rows or rows[0][1] is None:
            return None
        return datetime.fromtimestamp(rows[0][0] / 1000, UTC), float(rows[0][1])

    async def aclose(self) -> None:
        await self._http.aclose()

