"""Read API v2 (plan ha-addon 4.6, P2 detail plan step 4): capabilities, live snapshot and diagnostics described by
the plant's modules. Registered on the public app next to ``/v1``; without a plant every route answers 503
``plant_not_configured``. No entity names leave the hub through these routes.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response

from . import __version__
from .journal import Journal
from .history_v2 import from_legacy, period_days, plant_period
from .models import AvailabilityResponse, BalanceProfile, CompareBasis, JobStatus, MetricState, RscpView
from .models_v2 import (
    API_VERSION_2,
    PlantCapabilities,
    PlantDiagnostics,
    PlantEnergyPeriod,
    PlantComparison,
    PlantConsumers,
    PlantPowerResponse,
    PlantWidgetSnapshot,
    PlantFeatures,
    PlantJournalItem,
    PlantModuleDiagnostics,
    PlantModuleInfo,
    PlantPointInfo,
    PlantProblem,
    PlantSnapshot,
    PlantStringInfo,
    PlantTrust,
    PointRole,
)
from .plant import DEFAULT_ICONS, Kind, Module, Plant, Point
from .plant_live import CONSUMPTION, GENERATION, HOME, PlantLive

NOT_CONFIGURED = "plant_not_configured"


def _points(points: dict[str, Point]) -> list[PlantPointInfo]:
    return [PlantPointInfo(id=pid, unit=p.unit, role=PointRole(p.role.value)) for pid, p in points.items()]


def metric_ids(plant: Plant) -> list[str]:
    """Every metric id of the snapshot: the plant's points plus what the hub calculates."""
    ids = set(plant.points()) | {f"{GENERATION}.power", f"{CONSUMPTION}.power", f"{HOME}.power", "grid.power"}
    return sorted(ids)


def module_icon(m: Module) -> str | None:
    """The module's symbol: chosen, else the default of its kind; the measured consumption has none."""
    return m.icon or DEFAULT_ICONS.get(m.kind)


def build_capabilities(plant: Plant, timezone: str | None = None, rscp: bool = False,
                       history: bool = False, consumers: bool = False, compare: bool = False) -> PlantCapabilities:
    modules = []
    for m in plant.active():
        if m.kind is Kind.CONSUMPTION and not m.points:
            continue                # calculated total: a metric, not a module with a sensor
        modules.append(PlantModuleInfo(
            id=m.id, kind=m.kind.value, name=m.name, balance=m.balance, icon=module_icon(m), points=_points(m.points),
            strings=[PlantStringInfo(id=s.id, name=s.name, points=_points(s.points)) for s in m.strings]))
    return PlantCapabilities(api_version=API_VERSION_2, hub_version=__version__, plant_revision=plant.revision,
                             timezone=timezone, modules=modules, metrics=metric_ids(plant),
                             features=PlantFeatures(rscp=rscp, history=history, widget=history, consumers=consumers,
                                                   compare=compare),
                             pv_peak_w=plant.pv_peak_w, rscp_pv=plant.rscp_pv)


def build_diagnostics(live: PlantLive, journal: Journal | None, now: datetime, timezone: str | None = None,
                      subjects: dict[str, str] | None = None) -> PlantDiagnostics:
    """``subjects`` translates journal subjects written under old metric ids (legacy profile) to the plant's ids."""
    snap = live.snapshot(now)
    modules = []
    for mid, status in snap.modules.items():
        ages = [(now - p.reported_at).total_seconds() for p in snap.metrics.values()
                if p.module == mid and not p.derived and p.reported_at]
        modules.append(PlantModuleDiagnostics(
            module_id=mid, state=status.state, last_seen_at=status.last_seen_at,
            newest_report_age_s=round(min(ages), 1) if ages else None,
            oldest_report_age_s=round(max(ages), 1) if ages else None,
            discarded_events=live.discarded_events.get(mid, 0)))
    problems = [PlantProblem(metric_id=mid, state=p.state, reasons=p.reasons)
                for mid, p in sorted(snap.metrics.items()) if p.state is not MetricState.OK]
    trust = [PlantTrust(module_id=mid, last_evidence_at=t.last_evidence, counter_anomalies=t.counter_anomalies)
             for mid, t in sorted(live.trust.items())]
    # free text of hub events stays in the hub: older entries name entities ("grid_power → sensor.x") – only the
    # reasons of quality rows are passed on
    items = [PlantJournalItem(at=e.at, kind=e.kind, subject=(subjects or {}).get(e.subject, e.subject), state=e.state,
                              detail=e.detail if e.kind == "quality" else None)
             for e in (journal.recent(20, events=10) if journal else [])]
    return PlantDiagnostics(hub_version=__version__, generated_at=now, ha_connected=live.connected,
                            ha_disconnected_since=live.disconnected_since, plant_revision=live.plant.revision,
                            timezone=timezone, modules=modules, problems=problems, trust=trust, journal=items)


def _matches(if_none_match: str | None, etag: str) -> bool:
    """If-None-Match with weak comparison: a list of validators or ``*`` (RFC 9110 §13.1.2)."""
    if not if_none_match:
        return False
    wanted = {v.strip().removeprefix("W/") for v in if_none_match.split(",")}
    return "*" in wanted or etag.removeprefix("W/") in wanted


def register(app: FastAPI, require_read: Callable[..., Any], live: PlantLive | None, journal: Journal | None = None,
             rscp: Any = None, timezone: Callable[[], str | None] | None = None,
             rscp_enabled: Callable[[], bool] | None = None,
             journal_subjects: dict[str, str] | None = None, legacy_energy: Any = None, plant_history: Any = None,
             jobs: Any = None, upstream_errors: tuple[type[BaseException], ...] = (OSError,),
             legacy_consumers: Any = None) -> None:
    """``legacy_energy``: the existing calculation (imported plant); ``plant_history``: the generic history of any other
    plant; ``jobs``: shared with /v1 – the same computation never runs twice."""
    base = f"/{API_VERSION_2}"
    history_errors = (*upstream_errors, ConnectionError, TimeoutError, RuntimeError)

    def plant_live() -> PlantLive:
        if live is None:
            raise HTTPException(status_code=503, detail=NOT_CONFIGURED)
        return live

    def zone() -> str | None:
        return timezone() if timezone else None

    @app.get(f"{base}/capabilities", response_model=PlantCapabilities)
    def capabilities_v2(_: Any = Depends(require_read)) -> PlantCapabilities:
        served = rscp is not None and (rscp_enabled() if rscp_enabled else True)
        return build_capabilities(plant_live().plant, zone(), rscp=served,
                                  history=legacy_energy is not None or plant_history is not None,
                                  consumers=legacy_consumers is not None, compare=legacy_energy is not None)

    # async: runs on the event loop thread that also applies HA updates → no concurrent mutation
    @app.get(f"{base}/snapshot", response_model=PlantSnapshot)
    async def snapshot_v2(response: Response, if_none_match: str | None = Header(default=None),
                          _: Any = Depends(require_read)) -> PlantSnapshot | Response:
        snap = plant_live().snapshot(datetime.now(UTC))
        # fingerprint without per-request timestamps so unchanged data yields 304
        payload = snap.model_dump_json(exclude={"snapshot_id": True, "generated_at": True,
                                                "metrics": {"__all__": {"observed_at"}}})
        etag = 'W/"' + hashlib.sha256(payload.encode()).hexdigest()[:20] + '"'    # weak: gzip or identity
        if _matches(if_none_match, etag):
            return Response(status_code=304, headers={"ETag": etag, "Vary": "Accept-Encoding"})
        response.headers["ETag"] = etag
        response.headers["Vary"] = "Accept-Encoding"
        response.headers["Cache-Control"] = "no-store"
        return snap

    @app.get(f"{base}/diagnostics", response_model=PlantDiagnostics)
    async def diagnostics_v2(_: Any = Depends(require_read)) -> PlantDiagnostics:
        return build_diagnostics(plant_live(), journal, datetime.now(UTC), zone(), journal_subjects)

    @app.get(f"{base}/energy", response_model=PlantEnergyPeriod | JobStatus,
             responses={202: {"model": JobStatus, "description": "computation started – poll /v2/jobs/{id}"}})
    async def energy_v2(response: Response, period: Literal["day", "week", "month", "year"] = "day",
                        date_: date = Query(alias="date"), _: Any = Depends(require_read)) -> PlantEnergyPeriod | JobStatus:
        from .energy_api import JOB_THRESHOLD_DAYS, energy_period, missing_days
        from .periods import period_bounds
        live_ = plant_live()
        now = datetime.now(UTC)
        if legacy_energy is not None:                       # the imported plant: the existing calculation, renamed
            profile = BalanceProfile.E3DC_BALANCE
            todo = missing_days(legacy_energy, period, date_, profile)
            if jobs is not None and len(todo) > JOB_THRESHOLD_DAYS:
                async def work(progress: Callable[[float], None]) -> None:
                    for i, dd in enumerate(todo):
                        await legacy_energy.day(dd, profile)
                        progress((i + 1) / len(todo))
                response.status_code = 202
                return jobs.start(("energy", period, period_bounds(period, date_)[0], profile), work)
            try:
                return from_legacy(await energy_period(legacy_energy, period, date_, profile, now), live_.plant.revision)
            except upstream_errors as exc:
                raise HTTPException(status_code=503, detail="history unavailable") from exc
        if plant_history is None:
            raise HTTPException(status_code=503, detail="history unavailable")
        tz = plant_history.tz
        day_list = period_days(period, date_, tz)
        todo = await plant_history.missing([dd for dd in day_list if period_bounds("day", dd, tz)[0] < now])
        if jobs is not None and len(todo) > JOB_THRESHOLD_DAYS:
            response.status_code = 202
            return jobs.start(("energy_v2", period, period_bounds(period, date_, tz)[0]),
                              lambda progress: plant_history.days(todo, progress))
        try:
            loaded = [(dd, await plant_history.day(dd)) for dd in day_list]
        except history_errors as exc:
            raise HTTPException(status_code=503, detail="history unavailable") from exc
        return plant_period(period, date_, tz, loaded, now, live_.plant.revision,
                            "consumption.power" in live_.plant.points())

    @app.get(f"{base}/power", response_model=PlantPowerResponse)
    async def power_v2(series: str, from_: str = Query(alias="from"), to: str = Query(),
                       max_points: int = Query(600, alias="maxPoints", ge=10, le=2000),
                       _: Any = Depends(require_read)) -> PlantPowerResponse:
        """Power history under the live metric ids (same meaning and sign as the snapshot)."""
        from .energy_api import power_history, utc
        from .models import TimeInterval
        from .plant_power import LEGACY_SERIES, MAX_RANGE, allowed_series, plant_power
        from .plant_power import from_legacy as power_from_legacy
        live_ = plant_live()
        try:
            start, end = utc(from_), utc(to)
        except ValueError:
            raise HTTPException(status_code=422, detail="from/to must be ISO 8601 with timezone") from None
        if not start < end or end - start > MAX_RANGE:
            raise HTTPException(status_code=422, detail="range must be positive and at most 8 days")
        ids = [m for m in series.split(",") if m]
        revisions = None
        if legacy_energy is None and plant_history is not None:
            revisions = await plant_history.revisions(start, end, live_.plant)
        allowed = (sorted(k for k in LEGACY_SERIES if k.split(".")[0] in {m.id for m in live_.plant.active()}
                          or k.startswith(("generation", "consumption", "home")))
                   if legacy_energy is not None else allowed_series(live_.plant, list((revisions or {}).values())))
        if not 1 <= len(ids) <= 19 or any(m not in allowed for m in ids) or len(set(ids)) != len(ids):
            raise HTTPException(status_code=422, detail=f"1–19 distinct series of: {', '.join(allowed)}")
        try:
            if legacy_energy is not None:
                resp = power_from_legacy(await power_history(legacy_energy, [LEGACY_SERIES[i][0] for i in ids], start,
                                                             end, max_points), ids)
            elif plant_history is not None:
                resp = await plant_power(plant_history, live_.plant, ids, start, end, max_points, revisions)
            else:
                raise HTTPException(status_code=503, detail="history unavailable")
        except history_errors as exc:
            raise HTTPException(status_code=503, detail="history unavailable") from exc
        if journal is not None:
            resp = resp.model_copy(update={
                "journal_since": journal.first_record(),
                "monitoring_gaps": [TimeInterval(start=a, end=b) for a, b in journal.monitoring_gaps(resp.start, resp.end)]})
        return resp

    @app.get(f"{base}/widget-snapshot", response_model=PlantWidgetSnapshot)
    async def widget_v2(response: Response, if_none_match: str | None = Header(default=None),
                        _: Any = Depends(require_read)) -> PlantWidgetSnapshot | Response:
        """Compact snapshot for complications and widgets; the history part may be missing, the live part never."""
        from .widget import widget_snapshot
        from .widget_v2 import from_legacy as widget_from_legacy
        from .widget_v2 import plant_widget
        live_ = plant_live()
        now = datetime.now(UTC)
        snap = live_.snapshot(now)
        if legacy_energy is not None:
            w = widget_from_legacy(await widget_snapshot(None, legacy_energy, now, upstream_errors), snap, live_.plant)
        else:
            w = await plant_widget(snap, live_.plant, plant_history, now, history_errors)
        payload = w.model_dump_json(exclude={"generated_at"})
        etag = 'W/"' + hashlib.sha256(payload.encode()).hexdigest()[:20] + '"'
        if _matches(if_none_match, etag):
            return Response(status_code=304, headers={"ETag": etag, "Vary": "Accept-Encoding"})
        response.headers["ETag"] = etag
        response.headers["Vary"] = "Accept-Encoding"
        response.headers["Cache-Control"] = "no-store"
        return w

    @app.get(f"{base}/consumers", response_model=PlantConsumers)
    async def consumers_v2(_: Any = Depends(require_read)) -> PlantConsumers:
        """Consumers of the HA energy dashboard for the home detail sheet (imported plant only, so far)."""
        from .consumers_v2 import from_legacy as consumers_from_legacy
        plant_live()
        if legacy_consumers is None:
            raise HTTPException(status_code=503, detail="consumers unavailable")
        return consumers_from_legacy(await legacy_consumers.view(BalanceProfile.E3DC_BALANCE))

    @app.get(f"{base}/compare", response_model=PlantComparison | JobStatus,
             responses={202: {"model": JobStatus, "description": "computation started – poll /v2/jobs/{id}"}})
    async def compare_v2(response: Response, kind: Literal["day", "week", "month", "year", "range"],
                         a_from: date = Query(alias="aFrom"), a_to: date = Query(alias="aToExclusive"),
                         b_from: date = Query(alias="bFrom"), b_to: date = Query(alias="bToExclusive"),
                         basis: CompareBasis | None = None, _: Any = Depends(require_read)) -> PlantComparison | JobStatus:
        """Two periods compared (imported plant only, so far): local days [from, toExclusive)."""
        from .comparison import CompareError, Range
        from .comparison import validate as validate_comparison
        from .comparison_api import compare as compare_periods
        from .comparison_api import missing as compare_missing
        from .comparison_v2 import from_legacy as comparison_from_legacy
        from .energy_api import JOB_THRESHOLD_DAYS
        from .periods import TZ
        plant_live()
        if legacy_energy is None:
            raise HTTPException(status_code=503, detail="comparison unavailable")
        profile = BalanceProfile.E3DC_BALANCE
        now = legacy_energy.now()
        a, b = Range(a_from, a_to), Range(b_from, b_to)
        try:
            validate_comparison(kind, a, b, now.astimezone(TZ).date())
        except CompareError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        todo = compare_missing(legacy_energy, a, b, profile)
        if jobs is not None and len(todo) > JOB_THRESHOLD_DAYS:
            async def work(progress: Callable[[float], None]) -> None:
                for i, dd in enumerate(todo):
                    await legacy_energy.day(dd, profile)
                    progress((i + 1) / len(todo))
            response.status_code = 202
            return jobs.start(("compare", kind, a_from, a_to, b_from, b_to, profile), work)
        try:
            return comparison_from_legacy(await compare_periods(legacy_energy, kind, a, b, basis, profile, now))
        except upstream_errors as exc:
            raise HTTPException(status_code=503, detail="history unavailable") from exc

    @app.get(f"{base}/availability", response_model=AvailabilityResponse)
    def availability_v2(metrics: str, from_: str = Query(alias="from"), to: str = Query(),
                        _: Any = Depends(require_read)) -> AvailabilityResponse:
        """Intervals in which metrics were not ok (journal) – for hatching outages in history charts; metric ids of
        the snapshot. The imported plant's journal is written under the old ids and read through the id table."""
        from datetime import timedelta

        from .energy_api import utc
        from .models import AvailabilityInterval, AvailabilitySeries, TimeInterval
        live_ = plant_live()
        if journal is None:
            raise HTTPException(status_code=503, detail="journal unavailable")
        try:
            start, end = utc(from_), utc(to)
        except ValueError:
            raise HTTPException(status_code=422, detail="from/to must be ISO 8601 with timezone") from None
        ids = [m for m in metrics.split(",") if m]
        known = set(metric_ids(live_.plant))
        if not start < end or end - start > timedelta(days=31) or not 1 <= len(ids) <= 16 or any(m not in known for m in ids):
            raise HTTPException(status_code=422, detail="1–16 metrics of the snapshot, range ≤ 31 days")
        stored = {new: old for old, new in (journal_subjects or {}).items()}      # v2 id → id in the journal
        return AvailabilityResponse(start=start, end=end, journal_since=journal.first_record(), series=[
            AvailabilitySeries(metric_id=m, intervals=[
                AvailabilityInterval(start=a, end=b, state=MetricState(st), reasons=[r for r in rs.split(",") if r])
                for a, b, st, rs in journal.intervals(stored.get(m, m), start, end)]) for m in ids],
            monitoring_gaps=[TimeInterval(start=a, end=b) for a, b in journal.monitoring_gaps(start, end)])

    @app.get(f"{base}/jobs/{{job_id}}", response_model=JobStatus)
    def job_v2(job_id: str, _: Any = Depends(require_read)) -> JobStatus:
        job = jobs.get(job_id) if jobs is not None else None
        if job is None:
            raise HTTPException(status_code=404, detail="unknown job")
        return job

    @app.get(f"{base}/rscp", response_model=RscpView)
    def rscp_v2(_: Any = Depends(require_read)) -> RscpView:
        """E3/DC detail values from the cache; asking keeps the session open for a while. Never waits for the device."""
        plant_live()
        if rscp is None:
            raise HTTPException(status_code=503, detail="rscp unavailable")
        rscp.demand()
        return RscpView.model_validate(rscp.view())
