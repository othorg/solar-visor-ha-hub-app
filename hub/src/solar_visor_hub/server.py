"""Process entry point: two listeners in one process (plan §4.2, finding C1).

- Port A (public, read-only API): ``SOLAR_VISOR_PUBLIC_BIND``  default ``0.0.0.0:8080``
- Port B (admin/pairing, LAN only): ``SOLAR_VISOR_ADMIN_BIND``  required, must be an explicit
  interface address (wildcards such as 0.0.0.0 / :: are rejected) and a different port.
  In the add-on (``SOLAR_VISOR_RUNTIME=addon``) the listener binds inside the container, where no LAN address
  exists: the default is ``0.0.0.0:8081`` and the Supervisor decides whether and where the port is published.
  The LAN boundary is kept there by ``HomeNetworkOnly`` (peers outside the home network get 403).
- Web interface (add-on only, ``ui.py``): Ingress listener ``SOLAR_VISOR_UI_BIND`` default ``0.0.0.0:8099``, not
  published; standalone only a development listener on loopback (``SOLAR_VISOR_UI_DEV_BIND``).

Usage: ``python -m solar_visor_hub.server``
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import signal
import sys
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import Any

import uvicorn

from . import __version__
from .api_v2 import history_failures
from .app import UPSTREAM_ERRORS, create_admin_app, create_public_app
from .config import MappingConfig, load_mapping
from .devices import DeviceStore
from .consumers_api import ConsumersService
from .ha_client import HaClient, probe_ha
from .energy_api import JobManager
from .epochs import EXTRA_SERIES
from .epochs import measurements as epoch_measurements
from .history import EnergyCache, EnergyService, journal_invalid
from .influx import InfluxHistory, probe_influx
from .influx_config import FILE_NAME as INFLUX_FILE
from .influx_config import InfluxConfig, InfluxConfigStore, InfluxConnection, migrate_from_options
from .observations import ObservationStore, run_reconciler
from .journal import Journal, QualityMonitor
from .mapping_store import MappingStore
from .rscp import RscpCollector, RscpConfigStore
from .live import LiveModel
from .plant import PlantError, PlantStore
from .plant_legacy import DERIVED as LEGACY_DERIVED
from .plant_legacy import TABLE as LEGACY_TABLE
from .plant_live import PlantLive
from .periods import TZ as LEGACY_TZ
from .plant_power import power_reply
from .plant_archive import PlantArchive
from .plant_history import PlantHistory
from .runtime import (
    EMPTY_MAPPING,
    MAPPING,
    PLANT,
    PLANT_LEGACY,
    SETUP,
    LiveFanout,
    NoLive,
    NotConfigured,
    V1Unavailable,
    choose_mode,
)
from .settings import ADDON, addon_options, load_settings, runtime
from .rscp_admin import RscpAdmin
from .ui import DevGuard, IngressGuard, UiContext, UiSettings, UserList, build_label, create_ui_app, load_ui_bind
from .ui_card import CardContext
from .ui_admin import AdminContext
from .ui_plant import PlantEditing

OLD_TO_NEW = {**LEGACY_TABLE, **LEGACY_DERIVED}      # journal subjects under old metric ids → plant ids
SAFE_MODE_ENV = "SOLAR_VISOR_SAFE_MODE"             # set for the restart after a failed start with the stored plant
RESTART = "restart"

log = logging.getLogger("solar_visor_hub")


@dataclass(frozen=True)
class Bind:
    host: str
    port: int


class BindError(ValueError):
    pass


def parse_bind(value: str) -> Bind:
    host, sep, port = value.rpartition(":")
    if not sep or not host or not port.isdigit():
        raise BindError(f"invalid bind address {value!r}, expected host:port")
    p = int(port)
    if not 1 <= p <= 65535:
        raise BindError(f"invalid port in {value!r}")
    return Bind(host.strip("[]"), p)


def load_binds(env: dict[str, str] | None = None) -> tuple[Bind, Bind]:
    env = dict(os.environ if env is None else env)
    public = parse_bind(env.get("SOLAR_VISOR_PUBLIC_BIND", "0.0.0.0:8080"))
    in_addon = runtime(env) == ADDON
    admin_raw = env.get("SOLAR_VISOR_ADMIN_BIND") or ("0.0.0.0:8081" if in_addon else None)
    if not admin_raw:
        raise BindError("SOLAR_VISOR_ADMIN_BIND is required (explicit LAN address, e.g. 192.168.1.10:8081)")
    admin = parse_bind(admin_raw)
    try:
        addr = ipaddress.ip_address(admin.host)
    except ValueError as exc:
        raise BindError("admin listener must bind to an explicit IP address") from exc
    if not in_addon:                     # the container has no LAN interface; publishing is the Supervisor's decision
        if addr.is_unspecified:
            raise BindError("admin listener must not bind to a wildcard address (0.0.0.0 / ::)")
        if not (addr.is_private or addr.is_loopback):
            raise BindError("admin listener must bind to a private (LAN) or loopback address")
    if admin.port == public.port:
        raise BindError("admin and public listeners must use different ports")
    return public, admin


VPN_SHARED = ipaddress.ip_network("100.64.0.0/10")    # carrier-grade NAT range: Tailscale and other VPNs hand it out


def is_home_peer(host: str) -> bool:
    """True for a peer of the home network: private, loopback, link-local or VPN (shared) address."""
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return addr.is_private or addr.is_loopback or addr.is_link_local or addr in VPN_SHARED


class HomeNetworkOnly:
    """ASGI guard of the admin listener in the add-on. There the listener cannot bind to a LAN address (the container
    has none) and the Supervisor publishes the port on the host, so the LAN boundary is enforced on the peer instead:
    the container sees the real client address (measured, P0). Forwarding headers are never read – a reverse proxy
    in the home network is itself a home peer and has to restrict /admin, as before."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self._warned_at = 0.0

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        client = scope.get("client")
        if client and is_home_peer(str(client[0])):
            await self.app(scope, receive, send)
            return
        now = time.monotonic()
        if now - self._warned_at > 60:                     # scanners must not flood the log
            self._warned_at = now
            log.warning("admin request from outside the home network rejected (%s) – is port B forwarded?",
                        client[0] if client else "unknown peer")
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        body = b'{"detail":"admin access only from the home network"}'
        await send({"type": "http.response.start", "status": 403,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


class _Server(uvicorn.Server):
    """uvicorn re-raises SIGTERM after its own shutdown, which kills the process before the journal can
    record a clean stop. Signals are handled once for both listeners in ``serve()`` instead."""

    @contextmanager
    def capture_signals(self) -> Generator[None, None, None]:
        yield


def stored_revision(path: Any) -> int | None:
    """Revision of the plant file as it is now (the running one may be older until the restart)."""
    try:
        revision = json.loads(path.read_text()).get("revision")
    except (OSError, ValueError, AttributeError):
        return None
    return revision if isinstance(revision, int) else None


def load_mode(settings: Any, env: dict[str, str] | None = None) -> Any:
    """The plant if there is one, else the packaged mapping plus overrides (standalone) or setup (add-on). In the
    add-on a plant that cannot be read or did not start (``SOLAR_VISOR_SAFE_MODE``) leads to setup with the reason –
    the interface stays reachable to repair it. Standalone has no interface: a broken plant stops the start."""
    env = dict(os.environ if env is None else env)
    in_addon = settings.runtime == ADDON
    if in_addon and env.get(SAFE_MODE_ENV):
        return choose_mode(None, addon=True, error=env[SAFE_MODE_ENV])
    try:
        plant = PlantStore(settings.plant_path).current
    except (PlantError, ValueError, OSError) as exc:
        if not in_addon:
            raise
        log.error("stored plant unusable – starting in setup mode: %s", type(exc).__name__)
        return choose_mode(None, addon=True, error=f"Gespeicherte Anlage unlesbar oder ungültig ({type(exc).__name__})")
    return choose_mode(plant, addon=in_addon)


async def serve(phase: dict[str, bool] | None = None) -> str | None:
    """Runs until a signal (``None``) or until the interface activated a new plant (``RESTART``). ``phase["started"]``
    is set once the runtime is built – a failure before that point is a failure of the start."""
    phase = phase if phase is not None else {}
    public, admin = load_binds()
    settings = load_settings()
    ui_bind = load_ui_bind(settings.runtime == ADDON)
    mode = load_mode(settings)
    plant = mode.plant
    store: MappingStore | None = None
    live: LiveModel | None = None
    mapping = EMPTY_MAPPING
    if mode.name == MAPPING:
        store = MappingStore(load_mapping(), settings.mapping_overrides)
    elif mode.projection is not None:
        store = MappingStore(mode.projection, None)       # the projection is the whole truth: no override file
    if store is not None:
        mapping = store.current
        live = LiveModel(mapping)
    plant_live = PlantLive(plant) if plant is not None else None
    no_live = NoLive() if live is None and plant_live is None else None
    feed = LiveFanout(live, plant_live) if no_live is None else LiveFanout(no_live)
    devices = DeviceStore(settings.devices_db)
    journal = Journal(settings.journal_db)
    journal.record_start(datetime.now(UTC), f"hub {__version__}, " + (
        "setup" if mode.name == SETUP else
        f"mapping revision {mapping.revision}" if plant is None else f"plant revision {plant.revision}"))
    ha = HaClient(settings.ha_url, settings.read_ha_token, feed, ws_url=settings.ha_ws_url)   # type: ignore[arg-type]
    # exactly one journal writer: the fixed model while it exists (old ids – the existing history reads them)
    monitor = (QualityMonitor(live, journal) if live is not None
               else QualityMonitor(plant_live, journal, points=plant_live.quality_points) if plant_live is not None
               else None)                                                  # setup: nothing to judge
    # InfluxDB connection: set in the web interface (add-on) and applied at once; address and token travel together
    startup_influx = InfluxConfig(settings.influx_url, settings.influx_db, settings.influx_rp)
    influx_store = InfluxConfigStore(settings.data_dir / INFLUX_FILE) if settings.runtime == ADDON else None
    if influx_store is not None:
        migrate_from_options(influx_store, addon_options(settings.data_dir / "options.json"), startup_influx)
    influx = InfluxConnection(influx_store, startup_influx, settings.secrets.influx_token, settings.secrets.read_influx_token)
    observations: ObservationStore | None = None
    history: InfluxHistory | None = None
    energy: EnergyService | None = None
    jobs: JobManager | None = None
    if mode.has_history and live is not None and store is not None:
        observations = ObservationStore(settings.data_dir / "observations.sqlite3", entities=set(epoch_measurements()))
        live.on_observation = observations.record
        history = InfluxHistory(settings.influx_url, settings.influx_db, settings.influx_rp,
                                settings.secrets.read_influx_token,
                                set(epoch_measurements()) | set(store.current.entities()) | {s.measurement for s in EXTRA_SERIES},
                                connection=influx if influx.configurable else None)
        energy = EnergyService(history, EnergyCache(settings.data_dir / "energy.sqlite3"),
                               journal_invalid(settings.journal_db), observations.hub_points)
        jobs = JobManager()
    plant_history: PlantHistory | None = None
    if mode.name == PLANT and plant is not None and plant_live is not None:
        # a plant without the existing calculation: history from the recorder, kept in the archive (P3)
        pl = plant_live
        plant_history = PlantHistory(plant, PlantArchive(settings.data_dir / "archive.sqlite3"), ha.read,
                                     lambda: ha.connected, lambda: {e: st.unit for e, st in pl.entities.items()},
                                     lambda: ha.timezone, invalid=journal_invalid(settings.journal_db, stale=True),
                                     location=lambda: ha.location)
    rscp_store = RscpConfigStore(settings.data_dir / "rscp.json")

    def string_names() -> dict[str, Any]:
        if plant is None:
            return dict(store.current.e3dc_strings) if store else {}
        if plant.rscp_pv is None:
            return {}
        return {f"string{s.id[-1]}": s.name for s in plant.modules[plant.rscp_pv].strings}

    rscp = RscpCollector(rscp_store, settings.secrets, string_names=string_names)
    rscp.start()

    async def influx_fetch(entity: str, start: datetime, end: datetime) -> list[tuple[datetime, float]]:
        assert history is not None
        return [(t, float(v)) for t, v, _ in await history.points(entity, start, end) if v is not None]

    async def flush_observations() -> None:
        assert observations is not None
        while True:
            await asyncio.sleep(2)
            try:
                observations.flush()
            except Exception:   # noqa: BLE001 – keep buffering, never stop live processing
                log.exception("observation flush failed – retrying")

    def on_mapping_change(new: MappingConfig, description: str) -> None:
        assert live is not None
        live.set_mapping(new)
        ha.request_reconnect()          # re-subscribe with the new entity list
        journal.record_event("mapping_changed", datetime.now(UTC), f"rev {new.revision}: {description}")
        log.info("mapping changed (revision %s): %s", new.revision, description)

    if mode.name == MAPPING and store is not None:
        store.subscribe(on_mapping_change)      # with a plant the old mapping is a read-only projection

    def sample_entity() -> str | None:
        if plant is not None:
            return plant.points()["pv_1.power"].entity
        return store.current.metrics["pv_internal"].entity if store is not None else None

    async def test_ha() -> tuple[str, str]:
        sample = sample_entity()
        if sample is None:
            return "error", "keine Anlage eingerichtet"
        return await probe_ha(settings.ha_url, settings.read_ha_token, sample, ws_url=settings.ha_ws_url)

    async def test_influx() -> tuple[str, str]:
        sample = sample_entity()
        if sample is None:
            return "error", "keine Anlage eingerichtet"
        cfg, generation = influx.config()                            # what was saved, with its own token
        return await probe_influx(cfg.url, cfg.database, cfg.retention_policy, lambda: influx.token(generation), sample)

    admin_app: Any = create_admin_app(devices, store, ha.discover, test_ha, test_influx, rscp=rscp,
                                      rscp_store=rscp_store, secrets=settings.secrets, mapping_locked=plant is not None)
    if settings.runtime == ADDON:
        admin_app = HomeNetworkOnly(admin_app)
    if plant_history is not None and jobs is None:
        jobs = JobManager()
    plant_context = {"timezone": lambda: ha.timezone, "rscp_enabled": lambda: rscp_store.current.enabled,
                     "journal_subjects": OLD_TO_NEW if mode.name == PLANT_LEGACY else None,
                     "legacy_energy": energy if mode.name == PLANT_LEGACY else None, "plant_history": plant_history,
                     "jobs": jobs, "upstream_errors": UPSTREAM_ERRORS}
    consumers = ConsumersService(ha, live, energy) if live is not None and energy is not None else None
    if mode.name == PLANT_LEGACY:
        plant_context["legacy_consumers"] = consumers
    public_app: Any = create_public_app(
        mapping, live, devices, journal, store, energy, jobs=jobs, rscp=rscp, consumers=consumers,
        plant_live=plant_live, plant_context=plant_context, setup=no_live)
    if mode.name == SETUP:
        public_app = NotConfigured(public_app)
    elif not mode.serves_v1:
        public_app = V1Unavailable(public_app)
    servers = [
        _Server(uvicorn.Config(public_app, host=public.host, port=public.port, log_level="info", proxy_headers=False,
                               access_log=False)),
        _Server(uvicorn.Config(admin_app, host=admin.host, port=admin.port, log_level="info", proxy_headers=False,
                               access_log=False)),
    ]
    restart = asyncio.Event()
    loop = asyncio.get_running_loop()

    def request_restart() -> None:
        loop.call_later(0.5, restart.set)               # the answer to the browser goes out first

    if ui_bind is not None:
        editing = PlantEditing(settings.plant_path, mode.name, lambda: plant.revision if plant else None,
                               lambda: loop.call_soon_threadsafe(request_restart), startup_error=mode.error,
                               archive_path=settings.data_dir / "archive.sqlite3",
                               timezone=lambda: ha.timezone)
        ui_app = create_ui_app(UiContext(mode=mode.name, running_revision=plant.revision if plant else None,
                                         stored_revision=lambda: stored_revision(settings.plant_path),
                                         settings=UiSettings(settings.data_dir / "ui.json"), editing=editing,
                                         startup_error=mode.error, devices=devices,
                                         read=lambda message: ha.read(message, timeout=15, heavy=False),
                                         admin=AdminContext(
                                             mode=mode.name, ha_connected=lambda: ha.connected,
                                             snapshot=(lambda: plant_live.snapshot(datetime.now(UTC))) if plant_live else None,
                                             module_names=lambda: {m.id: m.name for m in plant.active()} if plant else {},
                                             timezone=lambda: ha.timezone, plant_history=plant_history, devices=devices,
                                             test_ha=test_ha, test_influx=test_influx, secrets=settings.secrets,
                                             rscp=RscpAdmin(rscp, rscp_store, settings.secrets),
                                             influx=influx if influx.configurable else None),
                                         card=CardContext(
                                             plant=lambda: plant,
                                             snapshot=(lambda: plant_live.snapshot(datetime.now(UTC))) if plant_live else None,
                                             timezone=lambda: ha.timezone, build=build_label,
                                             power=partial(power_reply,
                                                           legacy_energy=plant_context["legacy_energy"],
                                                           plant_history=plant_history),
                                             history_errors=history_failures(UPSTREAM_ERRORS),
                                             history_timezone=(lambda: LEGACY_TZ.key) if mode.name == PLANT_LEGACY
                                             else None)))
        users = UserList(lambda message: ha.read(message, timeout=10, heavy=False))
        ui_asgi: Any = IngressGuard(ui_app, users) if ui_bind.ingress else DevGuard(ui_app)
        servers.append(_Server(uvicorn.Config(ui_asgi, host=ui_bind.host, port=ui_bind.port, log_level="info",
                                              proxy_headers=False, access_log=False)))
    log.info("mode %s: public API on %s:%s, admin on %s:%s%s, %d entities subscribed",
             mode.name, public.host, public.port, admin.host, admin.port,
             f", interface on {ui_bind.host}:{ui_bind.port} ({'ingress' if ui_bind.ingress else 'development'})"
             if ui_bind else "", len(feed.subscribed_entities()))
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: [setattr(s, "should_exit", True) for s in servers])
    phase["started"] = True
    server_tasks = [asyncio.create_task(s.serve()) for s in servers]
    restart_wait = asyncio.create_task(restart.wait())
    background = [asyncio.create_task(ha.run_forever())]
    if monitor is not None:
        background.append(asyncio.create_task(monitor.run_forever()))
    if plant_history is not None:
        background.append(asyncio.create_task(plant_history.run_forever()))
    if observations is not None and energy is not None:
        background += [asyncio.create_task(flush_observations()),
                       asyncio.create_task(run_reconciler(observations, influx_fetch,
                                                          on_adopted=energy.adopted))]   # overrides finality
    try:
        # SIGTERM stops a server → stop the other one and the background tasks, then journal a clean stop
        await asyncio.wait([*server_tasks, restart_wait], return_when=asyncio.FIRST_COMPLETED)
    finally:
        restart_wait.cancel()
        for s in servers:
            s.should_exit = True
        await asyncio.gather(*server_tasks, return_exceptions=True)
        for t in background:
            t.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        if observations is not None:
            try:
                observations.flush()
            except Exception:   # noqa: BLE001
                log.exception("final observation flush failed")
        await asyncio.to_thread(rscp.stop)   # closes the RSCP session (socket deadline bounds the wait)
        if jobs is not None:
            await jobs.aclose()              # jobs start computations – stop them first
        if plant_history is not None:
            await plant_history.aclose()
        if energy is not None:
            await energy.aclose()            # computations use the InfluxDB client – drain them before closing it
        if history is not None:
            await history.aclose()
        journal.record_stop(datetime.now(UTC))
        log.info("stopped cleanly" + (" – restarting with the new plant" if restart.is_set() else ""))
    return RESTART if restart.is_set() else None


def next_start(result: str | None, error: BaseException | None, started: bool, in_addon: bool,
               env: dict[str, str]) -> dict[str, str] | None:
    """Environment of the next start, or None to end the process. A new plant restarts as it is; a failed start in the
    add-on restarts once in setup mode with the reason (never a loop: the setup start itself does not retry)."""
    clean = {k: v for k, v in env.items() if k != SAFE_MODE_ENV}
    if error is None:
        return clean if result == RESTART else None
    if in_addon and not started and not env.get(SAFE_MODE_ENV):
        return {**clean, SAFE_MODE_ENV: f"Start mit der gespeicherten Anlage fehlgeschlagen ({type(error).__name__})"}
    return None


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    phase: dict[str, bool] = {}
    result: str | None = None
    error: BaseException | None = None
    try:
        result = asyncio.run(serve(phase))
    except Exception as exc:   # noqa: BLE001 – decided below: retry in setup mode or end with the error
        error = exc
        log.exception("hub failed")
    env = next_start(result, error, phase.get("started", False), runtime() == ADDON, dict(os.environ))
    if env is None:
        if error is not None:
            raise SystemExit(1)
        return
    # a fresh process: nothing of the old runtime survives (sockets and files are not inherited, PEP 446)
    os.execve(sys.executable, [sys.executable, "-m", "solar_visor_hub.server"], env)


if __name__ == "__main__":
    main()
