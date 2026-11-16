"""Read-only Home Assistant WebSocket client (plan §3.3).

- initial ``get_states`` → live model
- ``subscribe_entities`` for push updates (compressed state format)
- every ``freshness_interval_s`` a one-shot ``render_template`` returning ``last_reported`` for all
  mapped entities, unsubscribed immediately (finding C4); REST templates are never used (admin-only)
- reconnect with exponential backoff 1 s … 30 s + jitter; on disconnect all values become stale
All outbound messages pass ``GuardedHaSocket`` (allow-list).
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import socket
import ssl
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import websockets

from .consumers import ConsumerStore, parse_prefs
from .live import LiveModel
from .transport import GuardedHaSocket

log = logging.getLogger("solar_visor_hub.ha")
# websockets logs every frame at DEBUG – including the auth frame with the token. Never below INFO.
logging.getLogger("websockets.client").setLevel(logging.INFO)

TEMPLATE_SEPARATOR = "|"


def _ts(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=UTC)
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def freshness_template(entity_ids: list[str]) -> str:
    """Fixed, hub-generated template: one line ``entity|last_reported_iso`` per entity."""
    ids = json.dumps(sorted(entity_ids))
    return (
        "{% for e in " + ids + " %}{{ e }}" + TEMPLATE_SEPARATOR
        + "{{ states[e].last_reported.isoformat() if states[e] is not none else '' }}\n{% endfor %}"
    )


def parse_freshness(result: str) -> dict[str, datetime | None]:
    out: dict[str, datetime | None] = {}
    for line in result.strip().splitlines():
        entity, _, iso = line.partition(TEMPLATE_SEPARATOR)
        if entity:
            out[entity.strip()] = _ts(iso.strip()) if iso.strip() else None
    return out


CONSUMER_REFRESH_S = 600.0          # HA energy dashboard configuration changes rarely
CONSUMER_RETRY_S = 60.0


class HaClient:
    def __init__(self, url: str, token_provider: Callable[[], str], live: LiveModel,
                 freshness_interval_s: float = 15.0, now: Callable[[], datetime] = lambda: datetime.now(UTC),
                 consumers: ConsumerStore | None = None, ws_url: str | None = None) -> None:
        self._ws_url = ws_url or websocket_url(url)
        self._token = token_provider
        self._live = live
        self._interval = freshness_interval_s
        self._now = now
        self._entities = live.subscribed_entities()
        self.timezone: str | None = None        # of the plant, from Home Assistant's configuration (get_config)
        self.location: tuple[float, float] | None = None   # latitude, longitude (get_config): the PV night rule
        self._heavy = asyncio.Semaphore(1)      # history/statistics requests: one at a time
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._subscriptions: dict[int, Callable[[dict[str, Any]], None]] = {}
        self._units: dict[str, str | None] = {}
        self._session_connected = False
        self.connected_once = asyncio.Event()
        self._sock: GuardedHaSocket | None = None
        self._ws: Any = None
        self.consumers = consumers or ConsumerStore()
        self._consumer_sub: int | None = None

    # ------------------------------------------------------------------ public
    async def run_forever(self) -> None:
        delay = 1.0
        while True:
            self._session_connected = False
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # log and retry – never take down the listeners (no secrets in message)
                log.warning("HA connection lost: %s", type(exc).__name__)
            finally:
                self._live.set_connected(False, self._now())
                self._fail_pending()
            if self._session_connected:
                delay = 1.0  # a working session resets the backoff
            await asyncio.sleep(delay + random.uniform(0, delay / 4))
            delay = min(delay * 2, 30.0)

    async def discover(self) -> list[dict[str, Any]]:
        """Current HA states (entity_id, state, unit, device_class, friendly_name) for the mapping editor.
        Uses the running session's allow-listed ``get_states``; raises ConnectionError without session."""
        if self._sock is None or not self._session_connected:
            raise ConnectionError("no HA session")
        res = await self._call(self._sock, {"type": "get_states"}, timeout=20)
        out = []
        for st in res.get("result") or []:
            if not isinstance(st, dict) or "entity_id" not in st:
                continue
            attrs = st.get("attributes") or {}
            out.append({"entity_id": st["entity_id"], "state": st.get("state"),
                        "attributes": {k: attrs[k] for k in ("unit_of_measurement", "device_class", "friendly_name")
                                       if k in attrs}})
        return out

    @property
    def connected(self) -> bool:
        return self._session_connected and self._sock is not None

    async def consumer_statistics(self, statistic_ids: list[str], start: datetime) -> dict[str, Any]:
        """Hourly energy change (kWh) of the HA energy dashboard's consumers since ``start`` (read-only statistics)."""
        if self._sock is None or not self._session_connected:
            raise ConnectionError("no HA session")
        res = await self._call(self._sock, {"type": "recorder/statistics_during_period", "start_time": start.isoformat(),
                                            "statistic_ids": statistic_ids, "period": "hour", "types": ["change"],
                                            "units": {"energy": "kWh"}}, timeout=20)
        if not res.get("success"):
            raise RuntimeError("statistics rejected")
        result = res.get("result")
        return result if isinstance(result, dict) else {}

    async def read(self, message: dict[str, Any], timeout: float = 30.0, heavy: bool = True) -> Any:
        """One read-only request over the live session (history, statistics). Heavy requests run one at a time so the
        live updates keep flowing; the answer must stay well below the frame limit – callers split by time
        (ha_history.py). Small requests of the interface (``heavy=False``: administrators, registries) do not queue
        behind them. Raises ``ConnectionError`` without a session and ``HaReadError`` if HA refuses."""
        if not heavy:
            return self._result(await self._light(message, timeout))
        async with self._heavy:
            if self._sock is None or not self._session_connected:
                raise ConnectionError("no HA session")
            res = await self._call(self._sock, message, timeout=timeout)
        return self._result(res)

    async def _light(self, message: dict[str, Any], timeout: float) -> dict[str, Any]:
        if self._sock is None or not self._session_connected:
            raise ConnectionError("no HA session")
        return await self._call(self._sock, message, timeout=timeout)

    @staticmethod
    def _result(res: dict[str, Any]) -> Any:
        if not res.get("success"):
            error = res.get("error") or {}
            raise HaReadError(str(error.get("code") or "unknown"), str(error.get("message") or "")[:200])
        return res.get("result")

    def request_reconnect(self) -> None:
        """Mapping changed: drop the session; ``run_forever`` reconnects and subscribes the new entity list."""
        if self._ws is not None:
            asyncio.get_running_loop().create_task(self._ws.close())

    # ------------------------------------------------------------------ session
    async def _session(self) -> None:
        self._entities = self._live.subscribed_entities()   # mapping may have changed since the last session
        async with websockets.connect(self._ws_url, max_size=16 * 2**20, open_timeout=10, ping_interval=20) as ws:
            sock = GuardedHaSocket(ws.send)
            self._sock, self._ws = sock, ws
            hello = json.loads(await asyncio.wait_for(ws.recv(), 10))
            if hello.get("type") != "auth_required":
                raise AuthError("unexpected handshake")
            await sock.send({"type": "auth", "access_token": self._token()})
            auth = json.loads(await asyncio.wait_for(ws.recv(), 10))
            if auth.get("type") != "auth_ok":
                raise AuthError("authentication rejected")
            reader = asyncio.create_task(self._reader(ws))
            consumers: asyncio.Task[None] | None = None
            config: asyncio.Task[None] | None = None
            try:
                await self._initial_states(sock)
                await self._subscribe(sock)
                self._live.set_connected(True, self._now())
                self._session_connected = True
                self.connected_once.set()
                log.info("HA connected, %d entities mapped", len(self._entities))
                # consumers of the HA energy dashboard: never blocks or breaks the live session; without a plant
                # (setup) nothing is watched at all (Codex P4 review N2)
                consumers = asyncio.create_task(self._consumer_loop(sock)) if self._entities else None
                config = asyncio.create_task(self._read_config(sock))
                while True:
                    await self._refresh_freshness(sock)
                    await asyncio.sleep(self._interval)
                    if reader.done():
                        exc = reader.exception()
                        raise exc if exc else ConnectionError("HA websocket closed")
            finally:
                reader.cancel()
                if consumers is not None:
                    consumers.cancel()
                if config is not None:
                    config.cancel()
                self.consumers.disconnect()
                self._consumer_sub = None
                self._sock = self._ws = None

    async def _read_config(self, sock: GuardedHaSocket) -> None:
        """Time zone and location of the plant. Runs beside the session and never matters for it: an instance that
        does not answer leaves both as they were and is asked again later."""
        delay = 30.0
        while True:
            try:
                res = await self._call(sock, {"type": "get_config"}, timeout=10)
                result = res.get("result") if res.get("success") else None
                zone = result.get("time_zone") if isinstance(result, dict) else None
                if isinstance(zone, str) and zone:
                    lat, lon = result.get("latitude"), result.get("longitude")      # type: ignore[union-attr]
                    if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (lat, lon)) \
                            and -90 <= lat <= 90 and -180 <= lon <= 180:            # type: ignore[operator]
                        self.location = (float(lat), float(lon))                    # type: ignore[arg-type]
                    self.timezone = zone
                    return
            except asyncio.CancelledError:
                raise
            except Exception as exc:   # noqa: BLE001 – a side task: nothing it meets may reach the session
                log.debug("get_config failed: %s", type(exc).__name__)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 900.0)

    async def _reader(self, ws: Any) -> None:
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    log.warning("ignored non-JSON frame from HA")
                    continue
                self._dispatch(msg)
        finally:
            # wake up everybody waiting so the session notices the closed connection immediately
            self._fail_pending()

    def _dispatch(self, msg: dict[str, Any]) -> None:
        mid = msg.get("id")
        if msg.get("type") == "event" and mid in self._subscriptions:
            self._subscriptions[mid](msg.get("event", {}))
        elif msg.get("type") == "result" and mid in self._pending:
            fut = self._pending.pop(mid)
            if not fut.done():
                fut.set_result(msg)

    async def _call(self, sock: GuardedHaSocket, message: dict[str, Any], timeout: float = 20) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        mid = sock.next_id  # id that send() will assign
        self._pending[mid] = fut
        try:
            assigned = await sock.send(message)
        except (OSError, websockets.WebSocketException) as exc:
            self._pending.pop(mid, None)
            raise ConnectionError("HA websocket closed") from exc
        if assigned != mid:
            self._pending.pop(mid, None)
            raise RuntimeError("message id mismatch")
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(mid, None)    # a timed-out call must not leave its future behind

    async def _initial_states(self, sock: GuardedHaSocket) -> None:
        res = await self._call(sock, {"type": "get_states"}, timeout=30)
        now = self._now()
        wanted = set(self._entities)
        for st in res.get("result") or []:
            if isinstance(st, dict) and st.get("entity_id") in wanted:
                unit = (st.get("attributes") or {}).get("unit_of_measurement")
                self._units[st["entity_id"]] = unit
                self._live.apply_state(st["entity_id"], st.get("state"), unit, _ts(st.get("last_changed")), now,
                                       bootstrap=True, last_updated=_ts(st.get("last_updated")))

    async def _subscribe(self, sock: GuardedHaSocket) -> None:
        if not self._entities:          # setup: nothing to watch – an empty list would subscribe every entity of HA
            return
        mid = sock.next_id
        self._subscriptions[mid] = self._on_entities_event
        res = await self._call(sock, {"type": "subscribe_entities", "entity_ids": self._entities})
        if not res.get("success"):
            raise AuthError("subscribe_entities rejected")

    def _on_entities_event(self, event: dict[str, Any]) -> None:
        now = self._now()
        for entity_id, comp in (event.get("a") or {}).items():
            attrs = comp.get("a") or {}
            unit = attrs.get("unit_of_measurement")
            # "a" = full state on (re)subscription → baseline only, like get_states
            if self._live.apply_state(entity_id, comp.get("s"), unit, _ts(comp.get("lc")), now, bootstrap=True,
                                      last_updated=_ts(comp.get("lu"))):
                self._units[entity_id] = unit
        for entity_id, diff in (event.get("c") or {}).items():
            plus = diff.get("+") or {}
            unit = self._units.get(entity_id)
            if "a" in plus and "unit_of_measurement" in (plus["a"] or {}):
                unit = plus["a"]["unit_of_measurement"]
            if "unit_of_measurement" in ((diff.get("-") or {}).get("a") or []):
                unit = None
            if "s" in plus:
                if self._live.apply_state(entity_id, plus["s"], unit, _ts(plus.get("lc")) or now, now,
                                          last_updated=_ts(plus.get("lu"))):
                    self._units[entity_id] = unit
            elif unit != self._units.get(entity_id):
                # unit-only change: re-interpret the current state with the new unit (cache only if accepted)
                if self._live.apply_unit(entity_id, unit, _ts(plus.get("lu"))):
                    self._units[entity_id] = unit
        for entity_id in event.get("r") or []:
            self._live.apply_state(entity_id, "unavailable", self._units.get(entity_id), now, now)

    async def _consumer_loop(self, sock: GuardedHaSocket) -> None:
        """Consumers of the HA energy dashboard, next to the live session and never inside it: catalog from
        ``energy/get_prefs`` (every 10 min, every minute while it fails – e.g. during HA's start), the power sensors in
        their own subscription (re-tried until it holds), their freshness in their own one-shot template. A failure
        here only logs and retries; the live values are not touched."""
        loop = asyncio.get_running_loop()
        next_prefs = loop.time()
        if self.consumers.catalog:                   # after a reconnect: live values right away
            await self._guarded(self._subscribe_consumers(sock))
        while True:
            try:
                if loop.time() >= next_prefs:
                    res = await self._call(sock, {"type": "energy/get_prefs"}, timeout=20)
                    specs = (parse_prefs(res.get("result")) if res.get("success")
                             else [] if (res.get("error") or {}).get("code") == "not_found"   # never configured
                             else None)
                    if specs is None:
                        log.warning("energy/get_prefs rejected")
                        next_prefs = loop.time() + CONSUMER_RETRY_S
                    else:
                        if self.consumers.set_catalog(specs, self._now()) or self._consumer_sub is None:
                            await self._subscribe_consumers(sock)
                        next_prefs = loop.time() + CONSUMER_REFRESH_S
                elif self._consumer_sub is None and self.consumers.entities():
                    await self._subscribe_consumers(sock)
                powers = sorted(self.consumers.power_entities())
                if powers:
                    for entity_id, reported in (await self._render_freshness(sock, powers)).items():
                        self.consumers.apply_reported(entity_id, reported)
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                raise
            except (TimeoutError, ConnectionError, OSError, RuntimeError, websockets.WebSocketException) as exc:
                log.warning("HA consumers: %s", type(exc).__name__)
                await asyncio.sleep(CONSUMER_RETRY_S)

    async def _guarded(self, step: Any) -> None:
        try:
            await step
        except (TimeoutError, ConnectionError, OSError, RuntimeError, websockets.WebSocketException) as exc:
            log.warning("HA consumers: %s", type(exc).__name__)

    async def _subscribe_consumers(self, sock: GuardedHaSocket) -> None:
        if self._consumer_sub is not None:
            old, self._consumer_sub = self._consumer_sub, None
            self._subscriptions.pop(old, None)
            # values of the old subscription are no longer updated: unknown until the new one delivers its states
            self.consumers.reset_readings()
            try:
                await sock.send({"type": "unsubscribe_events", "subscription": old})
            except (OSError, websockets.WebSocketException):
                pass
        entities = self.consumers.entities()
        if not entities:
            return
        mid = sock.next_id
        self._subscriptions[mid] = self._on_consumer_event
        res = await self._call(sock, {"type": "subscribe_entities", "entity_ids": entities})
        if res.get("success"):
            self._consumer_sub = mid
        else:
            self._subscriptions.pop(mid, None)
            log.warning("subscribe_entities (consumers) rejected")

    def _on_consumer_event(self, event: dict[str, Any]) -> None:
        """Runs in the reader: a malformed consumer event must never break the live session."""
        try:
            self._apply_consumer_event(event)
        except (AttributeError, TypeError, ValueError) as exc:
            log.warning("ignored consumer event: %s", type(exc).__name__)

    def _apply_consumer_event(self, event: dict[str, Any]) -> None:
        for entity_id, comp in (event.get("a") or {}).items():
            attrs = comp.get("a") or {}
            self.consumers.apply(entity_id, comp.get("s"), attrs.get("unit_of_measurement"), _ts(comp.get("lc")),
                                 attrs.get("friendly_name"))
        for entity_id, diff in (event.get("c") or {}).items():
            plus = diff.get("+") or {}
            attrs = plus.get("a") or {}
            if "s" in plus:
                self.consumers.apply(entity_id, plus["s"], attrs.get("unit_of_measurement"),
                                     _ts(plus.get("lc")) or self._now(), attrs.get("friendly_name"))
            elif attrs:
                self.consumers.update_attributes(entity_id, attrs.get("unit_of_measurement"), attrs.get("friendly_name"))
        for entity_id in event.get("r") or []:
            self.consumers.apply(entity_id, "unavailable", None, self._now())

    async def _refresh_freshness(self, sock: GuardedHaSocket) -> None:
        if not self._entities:
            return
        reported = await self._render_freshness(sock, self._entities)
        for entity_id, at in reported.items():
            self._live.apply_reported(entity_id, at)

    async def _render_freshness(self, sock: GuardedHaSocket, entities: list[str]) -> dict[str, datetime | None]:
        """One-shot ``render_template`` returning ``last_reported`` per entity (empty if HA rejects it)."""
        mid = sock.next_id
        done: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()

        def on_event(event: dict[str, Any]) -> None:
            if not done.done():
                done.set_result(event)

        self._subscriptions[mid] = on_event
        try:
            res = await self._call(sock, {"type": "render_template", "template": freshness_template(entities)})
            if not res.get("success"):
                log.warning("render_template rejected")
                return {}
            event = await asyncio.wait_for(done, 10)
            return parse_freshness(str(event.get("result", "")))
        finally:
            self._subscriptions.pop(mid, None)
            # one-shot: stop HA from re-rendering on every state change (best effort on a live socket)
            try:
                await sock.send({"type": "unsubscribe_events", "subscription": mid})
            except (OSError, websockets.WebSocketException):
                pass

    def _fail_pending(self) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError("HA websocket closed"))
        self._pending.clear()
        self._subscriptions.clear()


class AuthError(RuntimeError):
    pass


class HaReadError(RuntimeError):
    """Home Assistant answered a read request with an error (code from HA, e.g. ``invalid_format``)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def websocket_url(url: str) -> str:
    """WebSocket endpoint of a Home Assistant base URL (the Supervisor proxy passes its own, see settings)."""
    return url.rstrip("/").replace("http", "ws", 1) + "/api/websocket"


async def probe_ha(url: str, token_provider: Callable[[], str], sample_entity: str,
                   timeout: float = 8.0, ws_url: str | None = None) -> tuple[str, str]:
    """Connection test for the setup flow (plan §4.1 ``POST /admin/test/ha``) on a separate, short-lived
    connection. Returns (result class, German detail). Never includes the token or the URL."""
    try:
        token = token_provider()
    except (OSError, ValueError):
        return "token_missing", "Kein HA-Token auf dem Hub hinterlegt"
    ws_url = ws_url or websocket_url(url)
    try:
        async with asyncio.timeout(timeout):
            async with websockets.connect(ws_url, open_timeout=timeout, max_size=2**20) as ws:
                sock = GuardedHaSocket(ws.send)
                hello = json.loads(await ws.recv())
                await sock.send({"type": "auth", "access_token": token})
                if json.loads(await ws.recv()).get("type") != "auth_ok":
                    return "auth_rejected", "Home Assistant lehnt den Token ab"
                mid = await sock.send({"type": "render_template", "template": freshness_template([sample_entity])})
                result: dict[str, datetime | None] | None = None
                while result is None:
                    msg = json.loads(await ws.recv())
                    if msg.get("id") != mid:
                        continue
                    if msg.get("type") == "result" and not msg.get("success"):
                        return "no_last_reported", "Template-Abfrage abgelehnt"
                    if msg.get("type") == "event":
                        result = parse_freshness(str((msg.get("event") or {}).get("result", "")))
                await sock.send({"type": "unsubscribe_events", "subscription": mid})
                if result.get(sample_entity) is None:
                    return "no_last_reported", "Meldezeitpunkte (last_reported) fehlen"
                return "ok", f"Home Assistant {hello.get('ha_version', '?')} erreichbar, Meldezeitpunkte vorhanden"
    except TimeoutError:
        return "timeout", "Zeitüberschreitung"
    except socket.gaierror:
        return "dns", "Hostname nicht auflösbar"
    except ssl.SSLError:
        return "tls", "TLS-Fehler"
    except (OSError, websockets.WebSocketException):
        return "connect", "Verbindung abgelehnt oder unterbrochen"
    except (ValueError, KeyError, TypeError):   # malformed frames
        return "error", "Unerwartete Antwort von Home Assistant"
