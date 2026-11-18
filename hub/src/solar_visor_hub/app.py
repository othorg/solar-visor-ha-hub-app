"""HTTP applications (plan §4.1/§4.2).

Two separate ASGI apps, served on two listeners:
- ``public_app`` (port A): read-only API, may be exposed via NPM. Contains NO admin or pairing routes.
- ``admin_app`` (port B): administration and device pairing, bound to the LAN interface only.
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections.abc import Awaitable, Callable
from typing import Any, Literal
from datetime import UTC, date, datetime, timedelta

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator

from . import profile
from . import API_VERSION, __version__
from .config import MappingConfig, SecretFiles, load_mapping
from .runtime import EMPTY_MAPPING
from .bridge import BRIDGE_VERSIONS, VERSIONS_HEADER, note_bridge
from .devices import Device, DeviceStore, PairingError
import httpx

from .energy_api import (
    JOB_THRESHOLD_DAYS,
    JobManager,
    energy_period,
    missing_days,
    power_history,
    utc,
)
from .comparison import KINDS as COMPARE_KINDS
from .comparison import MAX_DAYS as COMPARE_MAX_DAYS
from .comparison import CompareError, Range
from .api_v2 import register as register_v2
from .comparison import validate as validate_comparison
from .comparison_api import compare as compare_periods
from .comparison_api import missing as compare_missing
from .consumers_api import ConsumersService
from .epochs import DATA_SINCE
from .power_history import MAX_RANGE as MAX_POWER_RANGE
from .power_history import SERIES as POWER_SERIES
from .widget import widget_snapshot
from .history import EnergyService
from .periods import TZ, period_bounds
from .journal import Journal
from .rscp import RscpCollector, RscpConfigStore
from .rscp_admin import RscpAdmin, RscpUpdateError
from .plant_live import PlantLive
from .live import DERIVED_METRICS, LiveModel
from .mapping_store import MappingError, MappingStore
from .normalize import compatible_unit
from .models import (
    BalanceProfile,
    Capabilities,
    CompareBasis,
    CompareCapabilities,
    EnergyComparison,
    HomeConsumers,
    ConfigView,
    Diagnostics,
    AvailabilityInterval,
    AvailabilityResponse,
    AvailabilitySeries,
    TimeInterval,
    WidgetSnapshot,
    EnergyPeriod,
    JobStatus,
    PowerResponse,
    Unit,
    JournalItem,
    MetricConfig,
    MetricProblem,
    MetricState,
    RscpConfigUpdate,
    RscpConfigView,
    RscpView,
    Snapshot,
    SourceConfigView,
    SourceDiagnostics,
    WallboxTrust,
)

AVAILABILITY_RANGE = timedelta(days=31)


def _bearer(authorization: str | None) -> str | None:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


def build_capabilities(mapping: MappingConfig, rscp: bool = False, consumers: bool = False) -> Capabilities:
    """Capabilities for a mapping; also exported as contract fixture for the Swift tests."""
    return Capabilities(
        rscp=rscp,
        api_version=API_VERSION,
        hub_version=__version__,
        components=sorted(mapping.sources),
        metrics=sorted([*mapping.metrics, *DERIVED_METRICS]),
        balance_profiles=list(BalanceProfile),
        kostal_strings={k: v for k, v in mapping.kostal_strings.items() if k.startswith("mppt") and isinstance(v, str)},
        e3dc_strings={k: v for k, v in mapping.e3dc_strings.items() if k.startswith("string") and isinstance(v, str)},
        compare=CompareCapabilities(kinds=list(COMPARE_KINDS), bases=list(CompareBasis), max_days=COMPARE_MAX_DAYS,
                                    data_since=DATA_SINCE),
        consumers=consumers,
    )


# InfluxDB down/erroneous (httpx transport errors, Influx error results, malformed answers)
UPSTREAM_ERRORS = (OSError, RuntimeError, TimeoutError, httpx.HTTPError, ValueError)


def build_config_view(mapping: MappingConfig) -> ConfigView:
    return ConfigView(
        mapping_revision=mapping.revision,
        sources=[SourceConfigView(source_id=sid, kind=src.kind, stale_after_s=src.stale_after_s, profile=src.profile)
                 for sid, src in sorted(mapping.sources.items())],
        metrics=[MetricConfig(metric_id=m.metric_id, source_id=m.source, entity=m.entity, unit=m.unit, sign=m.sign,
                              measurement_boundary=m.boundary, nonnegative=m.nonnegative, counter=m.counter)
                 for m in sorted(mapping.metrics.values(), key=lambda m: m.metric_id)],
        derived_metrics=list(DERIVED_METRICS),
        kostal_strings=build_capabilities(mapping).kostal_strings,
        e3dc_strings=build_capabilities(mapping).e3dc_strings,
    )


def build_diagnostics(mapping: MappingConfig, live: LiveModel, journal: Journal | None, now: datetime) -> Diagnostics:
    snap = live.snapshot(now)
    sources = []
    for sid, src in sorted(mapping.sources.items()):
        pts = [p for p in snap.metrics.values() if p.source_id == sid]
        if not pts:
            continue
        ages = [(now - p.reported_at).total_seconds() for p in pts if p.reported_at]
        status = snap.sources.get(sid)
        sources.append(SourceDiagnostics(
            source_id=sid, state=status.state if status else MetricState.UNKNOWN, stale_after_s=src.stale_after_s,
            last_seen_at=status.last_seen_at if status else None,
            newest_report_age_s=round(min(ages), 1) if ages else None,
            oldest_report_age_s=round(max(ages), 1) if ages else None,
            discarded_events=live.discarded_events.get(sid, 0)))
    problems = [MetricProblem(metric_id=mid, state=p.state, reasons=p.reasons)
                for mid, p in sorted(snap.metrics.items()) if p.state is not MetricState.OK]
    items = [JournalItem(at=e.at, kind=e.kind, subject=e.subject, state=e.state, detail=e.detail)
             for e in (journal.recent(20, events=10) if journal else [])]
    return Diagnostics(
        hub_version=__version__, generated_at=now, ha_connected=live.connected,
        ha_disconnected_since=live.disconnected_since, mapping_revision=mapping.revision,
        sources=sources, problems=problems,
        wallbox_trust=WallboxTrust(last_evidence_at=live.trust.last_evidence,
                                   counter_anomalies=live.trust.counter_anomalies),
        journal=items)


def create_public_app(mapping: MappingConfig | None = None, live: LiveModel | None = None,
                      devices: DeviceStore | None = None, journal: Journal | None = None,
                      store: MappingStore | None = None, energy: EnergyService | None = None,
                      jobs: JobManager | None = None, rscp: RscpCollector | None = None,
                      consumers: ConsumersService | None = None, plant_live: PlantLive | None = None,
                      plant_context: dict[str, Any] | None = None, setup: Any = None) -> FastAPI:
    """``plant_live`` (with optional ``plant_context`` for ``api_v2.register``) serves ``/v2`` next to ``/v1``.
    ``setup``: the session evaluator of the setup mode – ready means "connected to Home Assistant, waiting to be set
    up" (status ``setup``)."""
    # the packaged mapping only with the plant's own profile (plan hub-publish P4); otherwise nothing is mapped
    mapping = mapping or (store.current if store else (load_mapping() if profile.AVAILABLE else EMPTY_MAPPING))

    def current() -> MappingConfig:
        return store.current if store else mapping  # editor changes apply without restart
    app = FastAPI(
        title="Solar Visor Hub – public read API",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,  # contract is published in contracts/openapi.json, not served
    )
    _sanitized_validation_errors(app)
    app.add_middleware(GZipMiddleware, minimum_size=4096)   # energy day ≈ 450 KB JSON, power ≈ 60–400 KB

    @app.middleware("http")
    async def bridge_mark(request: Request, call_next: Any) -> Any:
        note_bridge(request.headers)               # forwarded by the Home Assistant integration (bridge.py)
        return await call_next(request)

    def require_read(authorization: str | None = Header(default=None)) -> Device:
        device = devices.authenticate(_bearer(authorization), "read") if devices else None
        if device is None:
            raise HTTPException(status_code=401, detail="unauthorized", headers={"WWW-Authenticate": "Bearer"})
        return device

    @app.get("/health/live")
    def health_live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    def health_ready(response: Response) -> dict[str, str]:
        response.headers[VERSIONS_HEADER] = ",".join(str(v) for v in BRIDGE_VERSIONS)
        source = live if live is not None else plant_live       # a plant without the legacy profile has no v1 model
        if source is None and setup is not None:
            status = "setup" if setup.connected else "degraded"
            if status != "setup":
                response.status_code = 503
            return {"status": status}
        status = "down" if source is None else ("ok" if source.connected else "degraded")
        if status != "ok":
            response.status_code = 503  # status-code based probes must notice degradation
        return {"status": status}

    @app.get(f"/{API_VERSION}/capabilities", response_model=Capabilities)
    def capabilities(_: Device = Depends(require_read)) -> Capabilities:
        return build_capabilities(current(), rscp=rscp is not None, consumers=consumers is not None)

    @app.get(f"/{API_VERSION}/rscp", response_model=RscpView)
    def rscp_route(_: Device = Depends(require_read)) -> RscpView:
        """E3/DC detail values from the cache; asking keeps the session open for a while (polling only while the
        RSCP tab or a detail sheet is open – user decision 2026-10-05). Never waits for the device."""
        if rscp is None:
            raise HTTPException(status_code=503, detail="rscp unavailable")
        rscp.demand()
        return RscpView.model_validate(rscp.view())

    @app.get(f"/{API_VERSION}/config", response_model=ConfigView)
    def config(_: Device = Depends(require_read)) -> ConfigView:
        return build_config_view(current())

    # async: reads the live model on the event loop thread (no concurrent mutation)
    @app.get(f"/{API_VERSION}/diagnostics", response_model=Diagnostics)
    async def diagnostics(_: Device = Depends(require_read)) -> Diagnostics:
        if live is None:
            raise HTTPException(status_code=503, detail="live model unavailable")
        return build_diagnostics(current(), live, journal, datetime.now(UTC))

    @app.get(f"/{API_VERSION}/energy", response_model=EnergyPeriod | JobStatus,
             responses={202: {"model": JobStatus, "description": "computation started – poll /v1/jobs/{id}"}})
    async def energy_route(response: Response, period: Literal["day", "week", "month", "year"] = "day",
                           date_: date = Query(alias="date"),
                           balance_profile: BalanceProfile = Query(BalanceProfile.E3DC_BALANCE, alias="balanceProfile"),
                           _: Device = Depends(require_read)) -> EnergyPeriod | JobStatus:
        if energy is None:
            raise HTTPException(status_code=503, detail="history unavailable")
        todo = missing_days(energy, period, date_, balance_profile)
        if jobs is not None and len(todo) > JOB_THRESHOLD_DAYS:
            async def work(progress) -> None:
                for i, dd in enumerate(todo):
                    await energy.day(dd, balance_profile)
                    progress((i + 1) / len(todo))
            response.status_code = 202
            first_day = period_bounds(period, date_)[0]          # one job per period, not per requested date
            return jobs.start(("energy", period, first_day, balance_profile), work)
        try:
            return await energy_period(energy, period, date_, balance_profile, datetime.now(UTC))
        except UPSTREAM_ERRORS as exc:   # InfluxDB unreachable/erroneous – nothing is cached
            raise HTTPException(status_code=503, detail="history unavailable") from exc

    @app.get(f"/{API_VERSION}/compare", response_model=EnergyComparison | JobStatus,
             responses={202: {"model": JobStatus, "description": "computation started – poll /v1/jobs/{id}"}})
    async def compare_route(response: Response, kind: Literal["day", "week", "month", "year", "range"],
                            a_from: date = Query(alias="aFrom"), a_to: date = Query(alias="aToExclusive"),
                            b_from: date = Query(alias="bFrom"), b_to: date = Query(alias="bToExclusive"),
                            basis: CompareBasis | None = None,
                            balance_profile: BalanceProfile = Query(BalanceProfile.E3DC_BALANCE, alias="balanceProfile"),
                            _: Device = Depends(require_read)) -> EnergyComparison | JobStatus:
        """Two periods compared (comparison plan 2026-10-06): local calendar days [from, toExclusive); the hub aligns,
        cuts running periods, rates the quality and offers the sensible bases."""
        if energy is None:
            raise HTTPException(status_code=503, detail="history unavailable")
        now = energy.now()
        a, b = Range(a_from, a_to), Range(b_from, b_to)
        try:
            validate_comparison(kind, a, b, now.astimezone(TZ).date())
        except CompareError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        todo = compare_missing(energy, a, b, balance_profile)
        if jobs is not None and len(todo) > JOB_THRESHOLD_DAYS:
            async def work(progress) -> None:
                for i, dd in enumerate(todo):
                    await energy.day(dd, balance_profile)
                    progress((i + 1) / len(todo))
            response.status_code = 202
            # keyed by the request, not by the shrinking set of missing days (Codex review 2026-10-06)
            return jobs.start(("compare", kind, a_from, a_to, b_from, b_to, balance_profile), work)
        try:
            return await compare_periods(energy, kind, a, b, basis, balance_profile, now)
        except UPSTREAM_ERRORS as exc:   # InfluxDB unreachable/erroneous – nothing is cached
            raise HTTPException(status_code=503, detail="history unavailable") from exc

    @app.get(f"/{API_VERSION}/consumers", response_model=HomeConsumers)
    async def consumers_route(balance_profile: BalanceProfile = Query(BalanceProfile.E3DC_BALANCE, alias="balanceProfile"),
                              _: Device = Depends(require_read)) -> HomeConsumers:
        """Consumers of the HA energy dashboard for the home detail sheet: now, today, not tracked (read-only)."""
        if consumers is None:
            raise HTTPException(status_code=503, detail="consumers unavailable")
        return await consumers.view(balance_profile)

    @app.get(f"/{API_VERSION}/availability", response_model=AvailabilityResponse)
    def availability_route(metrics: str, from_: str = Query(alias="from"), to: str = Query(),
                           _: Device = Depends(require_read)) -> AvailabilityResponse:
        """Intervals in which metrics were not ok (journal) – for hatching outages in history charts."""
        if journal is None:
            raise HTTPException(status_code=503, detail="journal unavailable")
        try:
            start, end = utc(from_), utc(to)
        except ValueError:
            raise HTTPException(status_code=422, detail="from/to must be ISO 8601 with timezone") from None
        ids = [m for m in metrics.split(",") if m]
        cfg = current()
        if not start < end or end - start > AVAILABILITY_RANGE or not 1 <= len(ids) <= 16 or any(
                m not in cfg.metrics and m not in DERIVED_METRICS for m in ids):
            raise HTTPException(status_code=422, detail="1–16 known metrics, range ≤ 31 days")
        since = journal.first_record()
        return AvailabilityResponse(start=start, end=end, journal_since=since, series=[
            AvailabilitySeries(metric_id=m, intervals=[
                AvailabilityInterval(start=a, end=b, state=MetricState(st), reasons=[r for r in rs.split(",") if r])
                for a, b, st, rs in journal.intervals(m, start, end)]) for m in ids],
            monitoring_gaps=[TimeInterval(start=a, end=b) for a, b in journal.monitoring_gaps(start, end)])

    @app.get(f"/{API_VERSION}/jobs/{{job_id}}", response_model=JobStatus)
    def job_route(job_id: str, _: Device = Depends(require_read)) -> JobStatus:
        job = jobs.get(job_id) if jobs else None
        if job is None:
            raise HTTPException(status_code=404, detail="unknown job")
        return job

    @app.get(f"/{API_VERSION}/power", response_model=PowerResponse)
    async def power_route(series: str, from_: str = Query(alias="from"), to: str = Query(),
                          max_points: int = Query(600, alias="maxPoints", ge=10, le=2000),
                          balance_profile: BalanceProfile = Query(BalanceProfile.E3DC_BALANCE, alias="balanceProfile"),
                          _: Device = Depends(require_read)) -> PowerResponse:
        """Power history from the energy engine's validated intervals (same rules, epochs and vetoes as
        /v1/energy). Points are column statistics (mean over the covered time, min, max), never samples."""
        if energy is None:
            raise HTTPException(status_code=503, detail="history unavailable")
        try:
            start, end = utc(from_), utc(to)
        except ValueError:
            raise HTTPException(status_code=422, detail="from/to must be ISO 8601 with timezone") from None
        if not start < end or end - start > MAX_POWER_RANGE:
            raise HTTPException(status_code=422, detail="range must be positive and at most 8 days")
        ids = [m for m in series.split(",") if m]
        if not 1 <= len(ids) <= len(POWER_SERIES) or any(m not in POWER_SERIES for m in ids) or len(set(ids)) != len(ids):
            raise HTTPException(status_code=422, detail=f"1–{len(POWER_SERIES)} distinct series of: {', '.join(POWER_SERIES)}")
        try:
            resp = await power_history(energy, ids, start, end, max_points, balance_profile)
        except UPSTREAM_ERRORS as exc:
            raise HTTPException(status_code=503, detail="history unavailable") from exc
        if journal is not None:
            resp = resp.model_copy(update={
                "journal_since": journal.first_record(),
                "monitoring_gaps": [TimeInterval(start=a, end=b)
                                    for a, b in journal.monitoring_gaps(resp.start, resp.end)]})
        return resp

    @app.get(f"/{API_VERSION}/widget-snapshot", response_model=WidgetSnapshot)
    async def widget_route(response: Response, if_none_match: str | None = Header(default=None),
                           _: Device = Depends(require_read)) -> WidgetSnapshot | Response:
        """Compact snapshot for complications/widgets (live + last quarter hour + today + sparkline)."""
        now = datetime.now(UTC)
        snap = live.snapshot(now) if live else None
        w = await widget_snapshot(snap, energy, now, UPSTREAM_ERRORS)
        payload = w.model_dump_json(exclude={"generated_at"})
        etag = 'W/"' + hashlib.sha256(payload.encode()).hexdigest()[:20] + '"'
        if if_none_match in (etag, etag[2:]):
            return Response(status_code=304, headers={"ETag": etag, "Vary": "Accept-Encoding"})
        response.headers["ETag"] = etag
        response.headers["Vary"] = "Accept-Encoding"
        response.headers["Cache-Control"] = "no-store"
        return w

    # async: runs on the event loop thread that also applies HA updates → no concurrent mutation
    @app.get(f"/{API_VERSION}/snapshot", response_model=Snapshot)
    async def snapshot(response: Response, profile: BalanceProfile = BalanceProfile.E3DC_BALANCE,
                 if_none_match: str | None = Header(default=None),
                 _: Device = Depends(require_read)) -> Snapshot | Response:
        if live is None:
            raise HTTPException(status_code=503, detail="live model not available")
        snap = live.snapshot(datetime.now(UTC), profile)
        # fingerprint without per-request timestamps so unchanged data yields 304
        payload = snap.model_dump_json(exclude={"snapshot_id": True, "generated_at": True, "metrics": {"__all__": {"observed_at"}}})
        # weak validator: the same snapshot may be sent gzip-encoded or identity (GZip middleware)
        etag = 'W/"' + hashlib.sha256(payload.encode()).hexdigest()[:20] + '"'
        if if_none_match in (etag, etag[2:]):
            return Response(status_code=304, headers={"ETag": etag, "Vary": "Accept-Encoding"})
        response.headers["ETag"] = etag
        response.headers["Vary"] = "Accept-Encoding"
        response.headers["Cache-Control"] = "no-store"
        return snap

    register_v2(app, require_read, plant_live, journal=journal, rscp=rscp, **(plant_context or {}))
    return app


class EnrollRequest(BaseModel):
    """Exactly one of ``code`` (8 digits, typed in at home) and ``invite`` (the pairing text's invitation, from
    anywhere – plan ha-remote-access §3.1)."""
    code: str | None = Field(default=None, min_length=8, max_length=8, pattern=r"^\d{8}$")
    invite: str | None = Field(default=None, min_length=32, max_length=32, pattern=r"^[A-Za-z0-9_-]{32}$")
    device_name: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def _one_secret(self) -> EnrollRequest:
        if (self.code is None) == (self.invite is None):
            raise ValueError("code or invite")
        return self


class DelegateRequest(BaseModel):
    device_name: str = Field(min_length=1, max_length=64)


class EnrollResponse(BaseModel):
    token: str
    scope: str


class _FailureLimiter:
    """At most ``limit`` events (failed pairing attempts, issued tokens) per ``window_s`` (whole listener).
    Sync routes run in a thread pool – check and record are atomic under a lock."""

    def __init__(self, limit: int = 10, window_s: float = 600) -> None:
        self.limit, self.window = limit, window_s
        self.failures: list[float] = []
        self._lock = threading.Lock()

    def _prune(self) -> None:
        now = time.monotonic()
        self.failures = [t for t in self.failures if now - t < self.window]

    def blocked(self) -> bool:
        with self._lock:
            self._prune()
            return len(self.failures) >= self.limit

    def record(self) -> None:
        with self._lock:
            self.failures.append(time.monotonic())

    def take(self) -> bool:
        """Reserves one event if the limit allows it (check and record in one step)."""
        return self.reserve() is not None

    def reserve(self) -> float | None:
        """Counts an attempt before it runs (concurrent attempts cannot exceed the limit); ``release`` takes it back
        when the attempt succeeded, so only failures remain counted."""
        with self._lock:
            self._prune()
            if len(self.failures) >= self.limit:
                return None
            stamp = time.monotonic()
            self.failures.append(stamp)
            return stamp

    def release(self, stamp: float) -> None:
        with self._lock:
            if stamp in self.failures:
                self.failures.remove(stamp)


Probe = Callable[[], Awaitable[tuple[str, str]]]
Discover = Callable[[], Awaitable[list[dict[str, Any]]]]


class MappingUpdate(BaseModel):
    entity: str = Field(min_length=3, max_length=128)
    sign: Literal[1, -1] = 1


def sanitize_validation_errors(app: FastAPI) -> None:
    """FastAPI's default 422 echoes the request input – never acceptable for codes/tokens (port B and the interface)."""

    async def handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        fields = [".".join(str(p) for p in e.get("loc", ())) for e in exc.errors()]
        return JSONResponse(status_code=422, content={"detail": "invalid request", "fields": fields})

    app.add_exception_handler(RequestValidationError, handler)  # type: ignore[arg-type]


_sanitized_validation_errors = sanitize_validation_errors


def create_admin_app(devices: DeviceStore | None = None, store: MappingStore | None = None,
                     discover: Discover | None = None, test_ha: Probe | None = None,
                     test_influx: Probe | None = None, rscp: RscpCollector | None = None,
                     rscp_store: RscpConfigStore | None = None, secrets: SecretFiles | None = None,
                     mapping_locked: bool = False) -> FastAPI:
    """``mapping_locked``: a plant is the source of truth – the old mapping is shown, not edited (409)."""
    app = FastAPI(title="Solar Visor Hub – admin (LAN only)", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
    _sanitized_validation_errors(app)

    @app.middleware("http")
    async def bridge_mark(request: Request, call_next: Any) -> Any:
        note_bridge(request.headers)
        return await call_next(request)

    # separate budgets: a flood of wrong codes must not block invitations or the watch, and vice versa
    code_failures = _FailureLimiter()                         # 8 digits: 10 failures / 10 min (whole listener)
    invite_failures = _FailureLimiter(limit=100, window_s=600)  # 192-bit invitations: unguessable, only against load
    delegate_failures = _FailureLimiter()
    issued = _FailureLimiter(limit=20, window_s=3600)   # delegations per hour (whole listener)

    def require_admin(authorization: str | None = Header(default=None)) -> Device:
        token = _bearer(authorization)
        device = devices.authenticate(token, "admin") if devices else None
        if device is None:
            if devices is not None and devices.authenticate(token, "read") is not None:
                raise HTTPException(status_code=403, detail="admin scope required")
            raise HTTPException(status_code=401, detail="unauthorized", headers={"WWW-Authenticate": "Bearer"})
        return device

    @app.get("/admin/health")
    def admin_health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/admin/v1/devices/delegate", response_model=EnrollResponse)
    def delegate(body: DelegateRequest, authorization: str | None = Header(default=None)) -> EnrollResponse:
        """LAN only: a paired device derives a read token for its companion (watch); replaces an older one."""
        if devices is None:
            raise HTTPException(status_code=503, detail="device store unavailable")
        slot = delegate_failures.reserve()
        if slot is None:
            raise HTTPException(status_code=429, detail="too many attempts")
        if not issued.take():                         # issuance is bounded too (each one is a database write)
            delegate_failures.release(slot)
            raise HTTPException(status_code=429, detail="too many attempts")
        try:
            token = devices.delegate(_bearer(authorization), body.device_name)
        except PairingError:
            raise HTTPException(status_code=403, detail="not allowed") from None
        delegate_failures.release(slot)
        return EnrollResponse(token=token, scope="read")

    @app.post("/admin/v1/devices/enroll", response_model=EnrollResponse)
    def enroll(body: EnrollRequest) -> EnrollResponse:
        if devices is None:
            raise HTTPException(status_code=503, detail="device store not available")
        failures = code_failures if body.code is not None else invite_failures
        slot = failures.reserve()
        if slot is None:
            raise HTTPException(status_code=429, detail="too many failed attempts")
        try:
            token, scope = devices.enroll(body.code, body.device_name, invite=body.invite)
        except PairingError:
            raise HTTPException(status_code=403, detail="invalid or expired pairing code") from None
        failures.release(slot)
        return EnrollResponse(token=token, scope=scope)

    # --- mapping editor (P2 WP5) ------------------------------------------------------------
    def need_store() -> MappingStore:
        if store is None:
            raise HTTPException(status_code=503, detail="mapping store not available")
        return store

    def need_editable() -> None:
        if mapping_locked:
            raise HTTPException(status_code=409, detail={"code": "managed_by_plant",
                                                         "message": "Die Zuordnung wird über die Anlage verwaltet"})

    async def ha_states() -> list[dict[str, Any]]:
        if discover is None:
            raise HTTPException(status_code=503, detail="home assistant not available")
        try:
            return await discover()
        except (ConnectionError, TimeoutError):
            raise HTTPException(status_code=503, detail="home assistant not connected") from None

    # all store routes are async: listeners mutate the live model and must run on the event loop thread
    @app.get("/admin/v1/config/mapping")
    async def get_mapping(_: Device = Depends(require_admin)) -> dict[str, Any]:
        st = need_store()
        return {"mapping_revision": st.current.revision, "metrics": st.entries()}

    @app.put("/admin/v1/config/mapping/{metric_id}")
    async def put_mapping(metric_id: str, body: MappingUpdate, _: Device = Depends(require_admin)) -> dict[str, Any]:
        need_editable()
        st = need_store()
        state = next((x for x in await ha_states() if x["entity_id"] == body.entity), None)
        try:
            st.set(metric_id, body.entity, body.sign, state)
        except MappingError as exc:
            code = 404 if exc.code == "unknown_metric" else 422
            raise HTTPException(status_code=code, detail={"code": exc.code, "message": str(exc)}) from None
        return {"mapping_revision": st.current.revision, "metrics": st.entries()}

    @app.delete("/admin/v1/config/mapping/{metric_id}")
    async def reset_mapping(metric_id: str, _: Device = Depends(require_admin)) -> dict[str, Any]:
        need_editable()
        st = need_store()
        try:
            st.clear(metric_id)
        except MappingError as exc:
            raise HTTPException(status_code=404, detail={"code": exc.code, "message": str(exc)}) from None
        return {"mapping_revision": st.current.revision, "metrics": st.entries()}

    @app.get("/admin/v1/discovery")
    async def discovery(unit: str | None = None, q: str | None = None, limit: int = 300,
                        _: Device = Depends(require_admin)) -> dict[str, Any]:
        """Candidate entities; ``unit`` = canonical unit of the target metric (W, Wh, V, A, %, bool, text)."""

        target = Unit(unit) if unit in {u.value for u in Unit} else None
        needle = (q or "").lower()
        items = []
        for st in await ha_states():
            eid = st["entity_id"]
            attrs = st.get("attributes") or {}
            domain = eid.split(".", 1)[0]
            if domain not in ("sensor", "binary_sensor", "select"):
                continue
            if target is not None and not compatible_unit(attrs.get("unit_of_measurement"), target, domain):
                continue
            name = str(attrs.get("friendly_name") or "")
            if needle and needle not in eid.lower() and needle not in name.lower():
                continue
            items.append({"entity_id": eid, "friendly_name": name, "state": st.get("state"),
                          "unit": attrs.get("unit_of_measurement"), "device_class": attrs.get("device_class")})
        items.sort(key=lambda x: x["entity_id"])
        return {"total": len(items), "items": items[: max(1, min(limit, 1000))]}

    # --- E3/DC RSCP configuration (credentials write-only; rscp_admin.py) -----------------------
    rscp_admin = RscpAdmin(rscp, rscp_store, secrets) if rscp and rscp_store and secrets else None

    def rscp_parts() -> RscpAdmin:
        if rscp_admin is None:
            raise HTTPException(status_code=503, detail="rscp unavailable")
        return rscp_admin

    @app.get("/admin/v1/rscp/config", response_model=RscpConfigView)
    def get_rscp_config(_: Device = Depends(require_admin)) -> RscpConfigView:
        return rscp_parts().view()

    @app.put("/admin/v1/rscp/config", response_model=RscpConfigView)
    def put_rscp_config(body: RscpConfigUpdate, _: Device = Depends(require_admin)) -> RscpConfigView:
        """Everything is validated before anything is written; credentials are never echoed or logged."""
        try:
            return rscp_parts().update(body)
        except RscpUpdateError as exc:
            raise HTTPException(status_code=exc.status,
                                detail=exc.message if exc.status == 503 else exc.detail) from None

    # --- connection tests (no secrets in responses) ------------------------------------------
    @app.post("/admin/v1/test/ha")
    async def test_ha_route(_: Device = Depends(require_admin)) -> dict[str, str]:
        if test_ha is None:
            raise HTTPException(status_code=503, detail="not configured")
        result, detail = await test_ha()
        return {"result": result, "detail": detail}

    @app.post("/admin/v1/test/influx")
    async def test_influx_route(_: Device = Depends(require_admin)) -> dict[str, str]:
        if test_influx is None:
            raise HTTPException(status_code=503, detail="not configured")
        result, detail = await test_influx()
        return {"result": result, "detail": detail}

    return app
