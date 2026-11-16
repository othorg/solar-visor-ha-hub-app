"""E3/DC RSCP: read-only detail values (rscp-plan.md, user decisions 2026-10-05).

Narrow read exception to the "HA only" rule: values that exist only via RSCP (battery modules, inverter phases and
temperatures, meters per phase, system flags). Never a second source for a value the hub takes from HA, never a
command. One worker thread owns the single session; it runs only while a client asks for the values (RSCP tab or
detail sheet open) and closes the session when nobody asks any more. HTTP handlers only read the cache.

Transport: own socket + Rijndael-256-CBC framing (pye3dc 0.10.0 encodes/decodes the RSCP payload and parses the
answers). Every outgoing message – including the authentication – passes ``ops_allowlist.check_rscp`` before it
is encoded; answers are read until the frame is complete (pye3dc reads a single ≤ 32 KB chunk); an error answer
for one request keeps the session (pye3dc closes it, so each probe of a missing index re-authenticated).
"""

from __future__ import annotations

import ipaddress
import json
import logging
import math
import os
import random
import socket
import tempfile
import struct
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from e3dc import E3DC, NotAvailableError
from e3dc._rscpLib import rscpDecode, rscpEncode, rscpFrame          # pinned pye3dc 0.10.0
from e3dc._rscpTags import RscpTag, RscpType
from py3rijndael import Rijndael

from . import profile
from .config import ConfigError, SecretFiles
from .ops_allowlist import check_rscp

log = logging.getLogger(__name__)

BLOCK = 32
HEADER = struct.Struct("<HHIIIH")           # magic, ctrl, seconds, seconds (high), ms, payload length
SOCKET_TIMEOUT_S = 5.0
REQUEST_DEADLINE_S = 10.0                   # one request incl. a fragmented answer (Codex review 2026-10-05)
IDLE_S = 30.0                               # session kept this long after the last client request
STALE_FACTOR = 3                            # a value older than 3 × its interval is stale
MAX_FRAME = 256 * 1024


class RscpError(Exception):
    """Transport/protocol failure – the session is closed."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class RscpAuthError(RscpError):
    def __init__(self) -> None:
        super().__init__("auth_failed")


class RscpDenied(NotAvailableError):
    """An error answer to one read after a successful login (e.g. ACCESS_DENIED): that part is unavailable, the
    session and the login are fine."""

    def __init__(self, code: str = "access_denied") -> None:
        super().__init__(code)
        self.code = code


# --- session ---------------------------------------------------------------------------------------------------

class RscpSession:
    """One RSCP connection. ``request`` is the only way out; it checks the message before encoding it."""

    def __init__(self, host: str, port: int, user: str, password: str, key: str,
                 timeout: float = SOCKET_TIMEOUT_S, connect: Callable[..., socket.socket] = socket.create_connection,
                 deadline_s: float = REQUEST_DEADLINE_S):
        key_bytes = key.encode("utf-8")
        if len(key_bytes) > BLOCK:
            raise RscpError("key_too_long")
        self._cipher = Rijndael(key_bytes.ljust(BLOCK, b"\xff"), block_size=BLOCK)
        self._enc_iv = self._dec_iv = b"\xff" * BLOCK
        self._user, self._password = user, password
        try:
            self._sock = connect((host, port), timeout=timeout)
        except TimeoutError:
            raise RscpError("timeout") from None
        except OSError:
            raise RscpError("unreachable") from None
        self._sock.settimeout(timeout)
        self._timeout, self._deadline_s = timeout, deadline_s
        self._deadline = 0.0
        self._authenticated = False
        self._pending = b""                 # received ciphertext not yet decrypted (incomplete block)
        self._plain = b""                   # decrypted bytes not yet consumed by a frame

    def authenticate(self) -> int:
        answer = self.request((RscpTag.RSCP_REQ_AUTHENTICATION, RscpType.Container, [
            (RscpTag.RSCP_AUTHENTICATION_USER, RscpType.CString, self._user.encode("utf-8")),
            (RscpTag.RSCP_AUTHENTICATION_PASSWORD, RscpType.CString, self._password.encode("utf-8")),
        ]))
        level = answer[2] if answer[0] == "RSCP_AUTHENTICATION" else 0
        if not isinstance(level, int) or level < 1:
            self.close()
            raise RscpAuthError()
        self._authenticated = True
        return level

    def request(self, message: Any) -> Any:
        check_rscp(message)                 # before encoding – nothing unchecked leaves the hub
        self._deadline = time.monotonic() + self._deadline_s
        self._send(rscpFrame(rscpEncode(message)))
        answer = self._receive()
        if answer[1] == "Error":
            if answer[2] == "RSCP_ERR_NOT_AVAILABLE":
                raise NotAvailableError()   # pye3dc's discovery expects this; the session stays open
            if self._authenticated:         # one read refused – the login and the session are fine
                raise RscpDenied("access_denied" if answer[2] == "RSCP_ERR_ACCESS_DENIED" else "device_error")
            self.close()
            if answer[2] == "RSCP_ERR_ACCESS_DENIED":
                raise RscpAuthError()
            raise RscpError("device_error")
        return answer

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass

    def _send(self, plain: bytes) -> None:
        plain += b"\x00" * (-len(plain) % BLOCK)
        out = bytearray()
        for i in range(0, len(plain), BLOCK):
            block = bytes(a ^ b for a, b in zip(plain[i:i + BLOCK], self._enc_iv, strict=True))
            self._enc_iv = self._cipher.encrypt(block)
            out += self._enc_iv
        try:
            self._sock.sendall(bytes(out))
        except TimeoutError:
            raise RscpError("timeout") from None
        except OSError:
            raise RscpError("connection_lost") from None

    def _receive(self) -> Any:
        """Reads until one complete frame is decrypted (answers may arrive in several TCP segments)."""
        while True:
            if len(self._plain) >= HEADER.size:
                magic, ctrl, _s, _s2, _ms, length = HEADER.unpack_from(self._plain)
                if magic != 0xDCE3:                       # E3 DC on the wire (little-endian field)
                    raise RscpError("protocol")
                total = HEADER.size + length + (4 if ctrl & 0x1000 else 0)
                if total > MAX_FRAME:
                    raise RscpError("protocol")
                if len(self._plain) >= total:
                    frame = self._plain[:total]
                    self._plain = b""                     # the rest is the zero padding of the last block
                    try:
                        return rscpDecode(frame)[0]
                    except Exception:                     # noqa: BLE001 – malformed answer, close the session
                        raise RscpError("protocol") from None
            remaining = self._deadline - time.monotonic()
            if remaining <= 0:
                raise RscpError("timeout")     # a trickling answer must not hold the worker forever
            try:
                self._sock.settimeout(min(self._timeout, remaining))
                chunk = self._sock.recv(32 * 1024)
            except TimeoutError:
                raise RscpError("timeout") from None
            except OSError:
                raise RscpError("connection_lost") from None
            if not chunk:
                raise RscpError("connection_closed")     # the device drops sessions with a wrong RSCP key
            data = self._pending + chunk
            full = len(data) - len(data) % BLOCK
            self._pending = data[full:]
            for i in range(0, full, BLOCK):
                block = data[i:i + BLOCK]
                self._plain += bytes(a ^ b for a, b in zip(self._cipher.decrypt(block), self._dec_iv, strict=True))
                self._dec_iv = block


class _Reader(E3DC):
    """pye3dc's getters (request layout and answer parsing) on our session: no constructor I/O, no retries."""

    def __init__(self, session: RscpSession, layout: dict[str, list[dict[str, Any]]] | None,
                 cancelled: Callable[[], bool] = lambda: False) -> None:  # noqa: D107
        # every attribute E3DC.__init__ sets (pye3dc 0.10.0) – the getters read them (poll(): lastRequest)
        self.session, self._cancelled = session, cancelled
        self.connectType = self.CONNECT_LOCAL
        self.username = None
        self.serialNumber = None
        self.serialNumberPrefix = None
        self.jar = None
        self.guid = None
        self.lastRequestTime = -1
        self.lastRequest = None
        self.connected = True
        self.deratePercent = self.deratePower = self.installedPeakPower = self.installedBatteryCapacity = None
        self.externalSourceAvailable = self.macAddress = self.model = self.maxAcPower = None
        self.maxBatChargePower = self.maxBatDischargePower = self.startDischargeDefault = None
        self.powermeters = list(layout["powermeters"]) if layout else []
        self.pvis = list(layout["pvis"]) if layout else []
        self.batteries = list(layout["batteries"]) if layout else []
        self.pmIndexExt = None

    def sendRequest(self, request, retries: int = 0, keepAlive: bool = False):  # type: ignore[override]  # noqa: ARG002
        if self._cancelled():
            raise RscpError("cancelled")    # shutdown or reconfiguration between two requests
        return self.session.request(request)


# --- configuration ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class RscpConfig:
    enabled: bool = False                   # rolled out disabled, enabled deliberately (plan phase 5)
    host: str = profile.RSCP_HOST          # TCP proxy into the E3/DC subnet (profile; empty: not set)
    port: int = 5033
    power_interval_s: int = 5               # "Leistungsdaten"
    system_interval_s: int = 30             # "Systemdaten"


def validate(cfg: RscpConfig) -> RscpConfig:
    try:
        addr = ipaddress.ip_address(cfg.host)
    except ValueError:
        raise ConfigError("host must be an IP address") from None
    if not addr.is_private:
        raise ConfigError("host must be a LAN address")
    if not 1 <= cfg.port <= 65535:
        raise ConfigError("port out of range")
    for v in (cfg.power_interval_s, cfg.system_interval_s):
        if not 1 <= v <= 30:
            raise ConfigError("intervals must be 1–30 s")
    return cfg


class RscpConfigStore:
    """Non-secret settings in the data directory (atomic write); the credentials stay in the secrets directory."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._mtime = self._stat()
        self._cfg = self._load()

    def _stat(self) -> int:
        try:
            return self.path.stat().st_mtime_ns
        except FileNotFoundError:
            return 0

    def _load(self) -> RscpConfig:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return validate(RscpConfig(**{k: raw[k] for k in RscpConfig.__dataclass_fields__ if k in raw}))
        except FileNotFoundError:
            return RscpConfig()
        except (ValueError, TypeError, ConfigError):
            log.warning("rscp config unreadable – defaults (disabled)")
            return RscpConfig()

    @property
    def current(self) -> RscpConfig:
        """Re-read when the file changed (CLI ``rscp-config`` while the hub runs)."""
        with self._lock:
            mtime = self._stat()
            if mtime != self._mtime:
                self._mtime, self._cfg = mtime, self._load()
            return self._cfg

    def update(self, **changes: Any) -> RscpConfig:
        with self._lock:
            cfg = validate(replace(self._cfg, **changes))
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=f".{self.path.name}.")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(asdict(cfg)))
            os.replace(tmp, self.path)
            self._cfg, self._mtime = cfg, self._stat()
            return cfg


# --- catalog: raw readings → typed groups ----------------------------------------------------------------------

NUMERIC_UNITS = {"W", "V", "A", "VA", "var", "%", "°C", "Wh", "Ah", "count", "code"}


def _v(id_: str, label: str, value: Any, unit: str | None = None) -> dict[str, Any]:
    """A value of the declared kind only: a nested error answer (text where a number belongs) or a non-finite
    reading becomes unknown instead of breaking the response (Codex review 2026-10-05)."""
    if unit in NUMERIC_UNITS:
        if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
            value = None
        elif isinstance(value, float):
            value = round(value, 3)
    elif unit == "bool":
        value = value if isinstance(value, bool) else None
    elif unit == "text":
        value = value if isinstance(value, str) else None
    return {"id": id_, "label": label, "value": value, "unit": unit}


def mask_serial(serial: Any) -> str | None:
    """Only the last four characters (pye3dc strips the "P10-" model prefix, so the digits start at once – review
    2026-10-05); a leading model prefix stays readable."""
    s = str(serial or "").strip()
    if len(s) < 6:
        return None
    prefix = s[:4] if "-" in s[:4] and not s[:3].isdigit() else ""
    return f"{prefix}…{s[-4:]}"


PM_TITLES = {1: "Netzzähler (Hausanschluss)", 2: "Zusatzzähler (externe Erzeugung)", 3: "Zusatzzähler (bidirektional)",
             4: "Zusatzzähler (Verbrauch)", 5: "Zusatzzähler (Erzeugung)", 6: "Wallbox-Zähler", 7: "Zähler (Farm)"}
FLAGS = [("dcdcAlive", "DCDC-Wandler aktiv"), ("powerMeterAlive", "Zähler aktiv"), ("batteryModuleAlive", "Batteriemodul aktiv"),
         ("pvModuleAlive", "PV-Modul aktiv"), ("pvInverterInited", "Wechselrichter initialisiert"),
         ("serverConnectionAlive", "Portalverbindung"), ("pvDerated", "PV abgeregelt"), ("emsAlive", "EMS aktiv"),
         ("acModeBlocked", "AC-Betrieb gesperrt"), ("emergencyPowerStarted", "Notstrom aktiv"),
         ("emergencyPowerOverride", "Notstrom übersteuert"), ("emergencyReserveReached", "Notstromreserve erreicht"),
         ("powerSaveEnabled", "Stromsparmodus"), ("chargeIdlePeriodActive", "Ladesperrzeit aktiv"),
         ("dischargeIdlePeriodActive", "Entladesperrzeit aktiv"), ("waitForWeatherBreakthrough", "Wetterprognose wartet"),
         ("rescueBatteryEnabled", "Batterie-Rettung aktiv"), ("socSyncRequested", "SOC-Abgleich angefordert")]


def overview_group(poll: dict[str, Any]) -> dict[str, Any]:
    prod, cons = poll.get("production", {}), poll.get("consumption", {})
    return {"id": "overview", "title": "Leistungen", "values": [
        _v("pv", "PV (S10)", prod.get("solar"), "W"), _v("additional", "Zusatz (KOSTAL)", prod.get("add"), "W"),
        _v("grid", "Netz (+ Bezug)", prod.get("grid"), "W"), _v("battery", "Batterie (+ Laden)", cons.get("battery"), "W"),
        _v("house", "Hausverbrauch", cons.get("house"), "W"), _v("wallbox", "Wallbox (E3/DC)", cons.get("wallbox"), "W"),
        _v("soc", "Ladezustand", poll.get("stateOfCharge"), "%"), _v("autarky", "Autarkie", poll.get("autarky"), "%"),
        _v("self_consumption", "Eigenverbrauch", poll.get("selfConsumption"), "%")], "items": []}


def battery_group(batteries: list[dict[str, Any]]) -> dict[str, Any]:
    items = []
    for b in batteries:
        n = b.get("index", 0) + 1
        items.append({"id": f"bat{n}", "title": f"Batterie {n}", "details": [], "values": [
            _v("rsoc", "Ladezustand (relativ)", b.get("rsoc"), "%"), _v("rsoc_real", "Ladezustand (real)", b.get("rsocReal"), "%"),
            _v("asoc", "Ladezustand (absolut)", b.get("asoc"), "%"), _v("cycles", "Ladezyklen", b.get("chargeCycles"), "count"),
            _v("voltage", "Klemmenspannung", b.get("terminalVoltage"), "V"), _v("module_voltage", "Modulspannung", b.get("moduleVoltage"), "V"),
            _v("current", "Strom", b.get("current"), "A"), _v("fcc", "Volle Ladekapazität", b.get("fcc"), "Ah"),
            _v("design_capacity", "Nennkapazität", b.get("designCapacity"), "Ah"),
            _v("remaining", "Nutzbare Restkapazität", b.get("usuableRemainingCapacity"), "Ah"),
            _v("max_cell_temp", "Zelltemperatur max.", b.get("maxDcbCellTemp"), "°C"),
            _v("min_cell_temp", "Zelltemperatur min.", b.get("minDcbCellTemp"), "°C"),
            _v("status", "Statuscode", b.get("statusCode"), "code"), _v("error", "Fehlercode", b.get("errorCode"), "code"),
            _v("name", "Bezeichnung", b.get("deviceName"), "text")]})
        for key, dcb in sorted((b.get("dcbs") or {}).items(), key=lambda kv: int(kv[0])):
            volts = [x for x in (dcb.get("voltages") or []) if isinstance(x, int | float)]
            temps = [x for x in (dcb.get("temperatures") or []) if isinstance(x, int | float)]
            m = int(key) + 1
            items.append({"id": f"bat{n}_dcb{m}", "title": f"Batterie {n} · Modul {m}", "values": [
                _v("soc", "Ladezustand", dcb.get("soc"), "%"), _v("soh", "Gesundheit (SOH)", dcb.get("soh"), "%"),
                _v("cycles", "Zyklen", dcb.get("cycleCount"), "count"), _v("voltage", "Spannung", dcb.get("voltage"), "V"),
                _v("current", "Strom", dcb.get("current"), "A"),
                _v("cell_v_min", "Zellspannung min.", min(volts) if volts else None, "V"),
                _v("cell_v_max", "Zellspannung max.", max(volts) if volts else None, "V"),
                _v("temp_min", "Temperatur min.", min(temps) if temps else None, "°C"),
                _v("temp_max", "Temperatur max.", max(temps) if temps else None, "°C"),
                _v("maker", "Hersteller", dcb.get("manufactureName"), "text")],
                "details": [_v(f"cell{i + 1}", f"Zelle {i + 1}", x, "V") for i, x in enumerate(volts)]
                         + [_v(f"sensor{i + 1}", f"Temperatursensor {i + 1}", x, "°C") for i, x in enumerate(temps)]})
    return {"id": "battery", "title": "Batterie", "values": [], "items": items}


def inverter_group(pvi: dict[str, Any], string_names: dict[str, str]) -> dict[str, Any]:
    temps = (pvi.get("temperature") or {}).get("values") or []
    state = pvi.get("deviceState") or {}
    values = [_v("on_grid", "Netzparallel", pvi.get("onGrid"), "bool"), _v("working", "In Betrieb", state.get("working"), "bool")]
    values += [_v(f"temp{i + 1}", f"Temperatur {i + 1}", t, "°C") for i, t in enumerate(temps)]
    items = []
    for key, s in sorted((pvi.get("strings") or {}).items(), key=lambda kv: int(kv[0])):
        n = int(key) + 1
        name = string_names.get(f"string{n}")
        items.append({"id": f"string{n}", "title": f"String {n}" + (f" · {name}" if name else ""), "details": [], "values": [
            _v("power", "Leistung", s.get("power"), "W"), _v("voltage", "Spannung", s.get("voltage"), "V"),
            _v("current", "Strom", s.get("current"), "A"), _v("energy", "Energie gesamt", s.get("energyAll"), "Wh")]})
    for key, p in sorted((pvi.get("phases") or {}).items(), key=lambda kv: int(kv[0])):
        n = int(key) + 1
        items.append({"id": f"phase{n}", "title": f"Phase L{n}", "details": [], "values": [
            _v("power", "Wirkleistung", p.get("power"), "W"), _v("voltage", "Spannung", p.get("voltage"), "V"),
            _v("current", "Strom", p.get("current"), "A"), _v("apparent", "Scheinleistung", p.get("apparentPower"), "VA"),
            _v("reactive", "Blindleistung", p.get("reactivePower"), "var"), _v("energy", "Energie gesamt", p.get("energyAll"), "Wh")]})
    return {"id": "inverter", "title": "Wechselrichter & PV", "values": values, "items": items}


PRODUCTION_METERS = {2, 5}                   # additional production: the device reports production negative


def _signed(value: Any, factor: int) -> Any:
    return value * factor if isinstance(value, int | float) and not isinstance(value, bool) else value


def meter_group(meters: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-phase power and energy with their sign in the label (review 2026-10-05): the grid meter + = import, a
    production meter turned production-positive (like pye3dc's EMS_POWER_ADD)."""
    items = []
    for m in meters:
        power, volt, energy = m.get("power") or {}, m.get("voltage") or {}, m.get("energy") or {}
        production = m.get("type") in PRODUCTION_METERS
        factor, sign = (-1, "Erzeugung") if production else (1, "+ Bezug")
        values = [_v(f"power_{p.lower()}", f"Leistung {p} ({sign})", _signed(power.get(p), factor), "W") for p in ("L1", "L2", "L3")]
        values += [_v(f"voltage_{p.lower()}", f"Spannung {p}", volt.get(p), "V") for p in ("L1", "L2", "L3")]
        values += [_v(f"energy_{p.lower()}", f"Energie {p} ({'Erzeugung' if production else 'Saldo, + Bezug'})",
                      _signed(energy.get(p), factor), "Wh") for p in ("L1", "L2", "L3")]
        items.append({"id": f"pm{m.get('index')}", "title": PM_TITLES.get(m.get("type"), f"Zähler {m.get('index')}"),
                      "values": values, "details": []})
    return {"id": "meters", "title": "Zähler", "values": [], "items": items}


def status_group(status: dict[str, Any]) -> dict[str, Any]:
    return {"id": "status", "title": "Systemstatus", "items": [],
            "values": [_v(k, label, status.get(k), "bool") for k, label in FLAGS if k in status]}


def limits_group(settings: dict[str, Any], info: dict[str, Any]) -> dict[str, Any]:
    return {"id": "limits", "title": "Leistungsgrenzen & Anlage", "items": [], "values": [
        _v("max_charge", "Max. Ladeleistung", settings.get("maxChargePower"), "W"),
        _v("max_discharge", "Max. Entladeleistung", settings.get("maxDischargePower"), "W"),
        _v("discharge_start", "Entladestart ab", settings.get("dischargeStartPower"), "W"),
        _v("limits_used", "Leistungsgrenzen aktiv", settings.get("powerLimitsUsed"), "bool"),
        _v("power_save", "Stromsparmodus", settings.get("powerSaveEnabled"), "bool"),
        _v("weather_charge", "Wetterbasiertes Laden", settings.get("weatherRegulatedChargeEnabled"), "bool"),
        _v("peak_power", "Installierte PV-Leistung", info.get("installedPeakPower"), "W"),
        _v("battery_capacity", "Installierte Batteriekapazität", info.get("installedBatteryCapacity"), "Wh"),
        _v("max_ac", "Max. AC-Leistung", info.get("maxAcPower"), "W"),
        _v("derate_percent", "Einspeisebegrenzung", info.get("deratePercent"), "%"),
        _v("derate_power", "Einspeisebegrenzung ab", info.get("deratePower"), "W"),
        _v("external_source", "Zusatzquelle vorhanden", bool(info.get("externalSourceAvailable"))
           if info.get("externalSourceAvailable") is not None else None, "bool")]}


# --- collector ---------------------------------------------------------------------------------------------------

@dataclass
class _Group:
    data: dict[str, Any]
    at: datetime
    interval_s: int


class RscpCollector:
    """Single session owner in a worker thread. ``demand()`` (HTTP) keeps it alive; ``view()`` reads the cache."""

    def __init__(self, store: RscpConfigStore, secrets: SecretFiles,
                 string_names: Callable[[], dict[str, str]] = dict,
                 session_factory: Callable[..., RscpSession] = RscpSession,
                 reader_factory: Callable[..., Any] = _Reader,
                 clock: Callable[[], float] = time.monotonic, now: Callable[[], datetime] = lambda: datetime.now(UTC),
                 idle_s: float = IDLE_S) -> None:
        self.store, self.secrets, self._names = store, secrets, string_names
        self._factory, self._clock, self._now, self._idle = session_factory, clock, now, idle_s
        self._reader_factory = reader_factory
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._demand_until = 0.0
        self._generation = 0                # bumped by reconfigure() (admin API): new credentials/address
        self._groups: dict[str, _Group] = {}
        self._device: dict[str, Any] = {}
        self._state = "idle"
        self._error: str | None = None
        self._last_attempt: datetime | None = None
        self._last_success: datetime | None = None
        self._info: dict[str, Any] = {}
        # worker-thread state
        self._session: RscpSession | None = None
        self._reader: Any = None
        self._key: tuple[int, tuple[str, int]] | None = None   # (generation, target) the layout and values belong to
        self._layout: dict[str, Any] | None = None
        self._unsupported: set[str] = set()  # groups the device refused – skipped until the configuration changes
        self._failed: tuple[Any, ...] | None = None   # (generation, target, secret stamp) of a refused login
        self._failures = 0
        self._next_power = self._next_system = self._backoff_until = 0.0

    # -- called from the event loop --------------------------------------------------------------------------------
    def demand(self) -> None:
        with self._lock:
            self._demand_until = self._clock() + self._idle
        self._wake.set()

    def reconfigure(self) -> None:
        with self._lock:
            self._generation += 1
            self._groups.clear()            # values of another device/address must not linger
            self._device = {}
        self._wake.set()

    def view(self) -> dict[str, Any]:
        cfg = self.store.current
        now = self._now()
        with self._lock:
            groups = []
            for g in self._groups.values():
                stale = (now - g.at).total_seconds() > STALE_FACTOR * g.interval_s
                groups.append({**g.data, "observed_at": g.at, "stale": stale})
            state = self._state
            if state == "auth_failed" and self._failed != (self._generation, (cfg.host, cfg.port), self._secret_stamp()):
                state = "idle"              # new credentials or address: the next request tries again
            if not cfg.enabled:
                state = "disabled"
            elif not self._secrets_present():
                state = "not_configured"
            return {"schema_version": 1, "state": state, "enabled": cfg.enabled,
                    "power_interval_s": cfg.power_interval_s, "system_interval_s": cfg.system_interval_s,
                    "last_attempt": self._last_attempt, "last_success": self._last_success,
                    "error": self._error if state in ("error", "auth_failed", "degraded") else None,
                    "device": dict(self._device) or None,
                    "groups": sorted(groups, key=lambda g: ORDER.index(g["id"]) if g["id"] in ORDER else 99)}

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="rscp", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = SOCKET_TIMEOUT_S + 2) -> None:
        self._stop.set()
        self._wake.set()
        session = self._session
        if session is not None:
            session.close()                 # interrupts a blocking receive in the worker
        if self._thread:
            self._thread.join(timeout)

    # -- worker thread ---------------------------------------------------------------------------------------------
    def _secrets_present(self) -> bool:
        s = self.secrets
        return all(p is not None and p.exists() and p.stat().st_size > 0 for p in (s.rscp_user, s.rscp_password, s.rscp_key))

    def _set(self, state: str, error: str | None = None) -> None:
        with self._lock:
            self._state, self._error = state, error

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                wait = self._step()
            except Exception as exc:        # noqa: BLE001 – the worker must survive anything (type only, no content)
                log.error("rscp worker step failed: %s", type(exc).__name__)
                self._close()
                self._set("error", "internal")
                wait = 30.0
            self._wake.wait(wait)
            self._wake.clear()
        self._close()

    def _close(self) -> None:
        if self._session is not None:
            self._session.close()
        self._session = self._reader = None

    def _step(self) -> float:
        """One worker iteration (tests drive it directly); returns the seconds until the next one."""
        cfg = self.store.current
        with self._lock:
            demanded, generation = self._clock() < self._demand_until, self._generation
        target = (cfg.host, cfg.port)
        key = (generation, target)
        if key != self._key:                # new settings, credentials or address – also while no session is open
            self._close()
            self._key, self._layout, self._unsupported = key, None, set()
            self._backoff_until, self._failures = 0.0, 0
            with self._lock:
                if generation == self._generation:
                    self._groups.clear()
                    self._device = {}
        if self._session is not None and (not demanded or not cfg.enabled):
            self._close()
        if not cfg.enabled or not demanded or not self._secrets_present():
            if self._state != "auth_failed" or not cfg.enabled:
                self._set("idle")
            return 5.0
        if self._failed == (generation, target, self._secret_stamp()):
            self._set("auth_failed", "auth_failed")
            return 5.0                      # refused login: wait for new settings or secrets (admin API or CLI)
        if self._clock() < self._backoff_until:
            return self._backoff_until - self._clock()
        attempt_stamp = self._secret_stamp()
        try:
            if self._session is None:
                self._connect(cfg, key)
            reader = self._reader
            assert reader is not None
            now_c, read = self._clock(), False
            if now_c >= self._next_power:
                self._store(key, "overview", overview_group(reader.poll(keepAlive=True)), cfg.power_interval_s)
                self._next_power, read = now_c + cfg.power_interval_s, True
            if now_c >= self._next_system:
                self._read_system(reader, key, cfg.system_interval_s)
                self._next_system, read = now_c + cfg.system_interval_s, True
            self._failures = 0
            with self._lock:
                if read:
                    self._last_success = self._now()   # only after a device read
                degraded = any(g.data.get("error") for g in self._groups.values())
            self._set("degraded" if degraded else "connected")
            return max(0.05, min(self._next_power, self._next_system) - self._clock())
        except RscpAuthError:
            self._failed = (generation, target, attempt_stamp)   # the credentials actually tried
            self._set("auth_failed", "auth_failed")
            log.warning("rscp authentication failed – waiting for new credentials")
            self._close()
            return 5.0
        except (RscpError, NotAvailableError, OSError, ValueError, KeyError, TypeError, ConfigError) as exc:
            code = exc.code if isinstance(exc, RscpError) else type(exc).__name__.lower()
            self._failures += 1
            delay = min(60.0, 2.0 ** self._failures) * (0.8 + 0.4 * random.random())
            self._backoff_until = self._clock() + delay
            self._set("error", code)
            log.warning("rscp read failed (%s), retry in about %.0f s", code, delay)
            self._close()
            return delay

    def _cancelled(self, key: tuple[int, tuple[str, int]]) -> bool:
        cfg = self.store.current
        return self._stop.is_set() or key != (self._generation, (cfg.host, cfg.port))

    def _connect(self, cfg: RscpConfig, key: tuple[int, tuple[str, int]]) -> None:
        self._set("connecting")
        with self._lock:
            self._last_attempt = self._now()
        self._session = self._factory(cfg.host, cfg.port, self._secret("rscp_user"), self._secret("rscp_password"),
                                      self._secret("rscp_key"))
        self._session.authenticate()
        reader = self._reader = self._reader_factory(self._session, self._layout, lambda: self._cancelled(key))
        reader.get_system_info_static(keepAlive=True)
        if self._layout is None:            # once per configuration: the discovery probes every index
            pvis = [{**p, "strings": max(p.get("strings") or 0, _max_strings(reader, p["index"]))}
                    for p in reader.get_pvis(keepAlive=True)]
            self._layout = {"batteries": reader.get_batteries(keepAlive=True), "pvis": pvis,
                            "powermeters": reader.get_powermeters(keepAlive=True)}
        reader.batteries, reader.pvis, reader.powermeters = (list(self._layout["batteries"]), list(self._layout["pvis"]),
                                                             list(self._layout["powermeters"]))
        info = reader.get_system_info(keepAlive=True)
        with self._lock:
            if key[0] == self._generation:
                self._device = {"model": info.get("model"), "firmware": info.get("release"),
                                "serial_masked": mask_serial(info.get("serial"))}
                self._info = info
        self._next_power = self._next_system = 0.0

    def _secret_stamp(self) -> tuple[int, ...]:
        s = self.secrets
        return tuple(p.stat().st_mtime_ns if p is not None and p.exists() else 0
                     for p in (s.rscp_user, s.rscp_password, s.rscp_key))

    def _secret(self, name: str) -> str:
        path = getattr(self.secrets, name)
        value = path.read_text(encoding="utf-8").rstrip("\r\n")
        if not value:
            raise ConfigError(f"{name} empty")
        return value

    def _store(self, key: tuple[int, tuple[str, int]], gid: str, data: dict[str, Any], interval: int) -> None:
        with self._lock:
            if key[0] == self._generation:  # a read that outlived a reconfiguration is dropped
                self._groups[gid] = _Group(data, self._now(), interval)

    def _read_system(self, reader: Any, key: tuple[int, tuple[str, int]], interval: int) -> None:
        """Each part on its own: one refused or unreadable part marks its group, the others still update; a refused
        part is not asked again until the configuration changes (plan: suspend until rediscovery)."""
        layout = self._layout or {}
        pvi = (layout.get("pvis") or [None])[0]
        parts: list[tuple[str, Callable[[], dict[str, Any]]]] = [
            ("battery", lambda: battery_group([reader.get_battery_data(batIndex=b["index"],
                                                                       dcbs=list(range(b.get("dcbs") or 0)), keepAlive=True)
                                               for b in layout.get("batteries", [])])),
            ("inverter", lambda: inverter_group(reader.get_pvi_data(
                pviIndex=pvi["index"], strings=list(range(pvi.get("strings") or 0)),
                phases=list(range(pvi.get("phases") or 0)), keepAlive=True), self._names())),
            ("meters", lambda: meter_group([reader.get_powermeter_data(pmIndex=m["index"], keepAlive=True) | {"type": m.get("type")}
                                            for m in layout.get("powermeters", [])])),
            ("status", lambda: status_group(reader.get_system_status(keepAlive=True))),
            ("limits", lambda: limits_group(reader.get_power_settings(keepAlive=True), self._info)),
        ]
        for gid, fn in parts:
            if gid in self._unsupported or (gid == "inverter" and pvi is None):
                continue
            try:
                self._store(key, gid, fn(), interval)
            except (NotAvailableError, KeyError, TypeError, ValueError, IndexError) as exc:   # transport errors propagate
                code = getattr(exc, "code", "not_available") if isinstance(exc, NotAvailableError) else "unreadable"
                if isinstance(exc, NotAvailableError):
                    self._unsupported.add(gid)
                log.info("rscp group %s: %s", gid, code)
                with self._lock:
                    if key[0] != self._generation:
                        continue
                    old = self._groups.get(gid)
                    base = old.data if old else {"id": gid, "title": TITLES.get(gid, gid), "values": [], "items": []}
                    self._groups[gid] = _Group({**base, "error": code}, old.at if old else self._now(), interval)


ORDER = ["overview", "battery", "inverter", "meters", "status", "limits"]
TITLES = {"overview": "Leistungen", "battery": "Batterie", "inverter": "Wechselrichter & PV", "meters": "Zähler",
          "status": "Systemstatus", "limits": "Leistungsgrenzen & Anlage"}


def _max_strings(reader: Any, pvi_index: int) -> int:
    """The device reports the *used* string count (1 here) although both strings carry power (spike 2026-10-05):
    ask for the maximum."""
    answer = reader.sendRequest((RscpTag.PVI_REQ_DATA, RscpType.Container, [
        (RscpTag.PVI_INDEX, RscpType.Uint16, pvi_index), (RscpTag.PVI_REQ_DC_MAX_STRING_COUNT, RscpType.NoneType, None)]))
    for tag, _typ, value in answer[2] if isinstance(answer[2], list) else []:
        if tag == "PVI_DC_MAX_STRING_COUNT" and isinstance(value, list):
            for t2, _t, v2 in value:
                if t2 == "PVI_VALUE" and isinstance(v2, int):
                    return v2
        if tag == "PVI_DC_MAX_STRING_COUNT" and isinstance(value, int):
            return value
    return 0
