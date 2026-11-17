"""API assembly for /v1/energy, /v1/power and /v1/jobs (plan §4.1, P3)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta

from .daycalc import DERIVED, DERIVED_CATEGORIES, SUPPLY, SlotEnergy, derived_slots
from .daycalc import coverage as slot_coverage
from .epochs import CALCULATION_VERSION
from .history import DayAggregate, DayData, EnergyService, aggregate
from .kpis import BASE, FULL, kpis, totals
from .models import (
    Allocation,
    BalanceProfile,
    EnergyBucket,
    EnergyMethod,
    EnergyPeriod,
    EnergyTotals,
    JobStatus,
    PowerResponse,
)
from .periods import TZ, Slot, days, period_bounds, quarter_hours
from .power_history import EXTRA_SERIES_IDS, DayColumns, snap
from .power_history import series as power_columns_series

PUBLIC_CATEGORIES = ("pv_internal", "external", "generation", "consumption", "wallbox", "home",
                     "battery_charge", "battery_discharge", "grid_import", "grid_export", "mppt1", "mppt2", "mppt3",
                     "s10_string1", "s10_string2", *SUPPLY)
WEEK_BLOCK_HOURS = 6                 # week view: 28 bars of 6 local hours (user decision 2026-10-05)
JOB_THRESHOLD_DAYS = 7
PROVISIONAL = timedelta(minutes=2)


def _pad(n: int, reason: str = "no_source") -> list[SlotEnergy]:
    return [SlotEnergy(None, 0.0, reasons={reason}) for _ in range(n)]


def _public(cats: dict[str, list[SlotEnergy]], n: int, future: list[bool] | None = None) -> dict[str, list[SlotEnergy]]:
    """Public categories per slot: generation = PV + external, home = consumption − wallbox; grid is already
    selected by epoch in the engine. ``future``: parts that have not begun get the reason 'future' even for
    categories without any data."""
    def pad() -> list[SlotEnergy]:
        if future is None:
            return _pad(n)
        return [SlotEnergy(None, 0.0, reasons={"future" if f else "no_source"}) for f in future]
    out = {c: cats.get(c) or pad() for c in PUBLIC_CATEGORIES if c not in DERIVED_CATEGORIES}
    # per-day aggregates already carry the derived categories formed per slot; slot data is combined here
    derived = ({k: cats[k] for k in DERIVED_CATEGORIES} if all(k in cats for k in DERIVED_CATEGORIES)
               else derived_slots(out, n))
    return {**out, **derived}


def _bucket(start: datetime, end: datetime, label: str, offset: int, parts: list[SlotEnergy],
            profile: BalanceProfile, now: datetime) -> EnergyBucket:
    known = [p for p in parts if p.wh is not None]
    coverage = slot_coverage(known)
    reasons = sorted(set().union(*(p.reasons for p in parts))) if parts else []
    if any(p.confidence == "reconstructed" for p in known):
        reasons = sorted(set(reasons) | {"reconstructed"})
    return EnergyBucket(
        start=start, end=end, local_label=label, utc_offset_minutes=offset,
        energy_wh=round(sum(p.wh or 0.0 for p in known), 1) if known else None,
        method=EnergyMethod.COUNTER_DELTA if known and all(p.method == "counter_delta" for p in known) else EnergyMethod.SAMPLE_HOLD,
        coverage_fraction=round(min(1.0, coverage), 4), complete=bool(known) and coverage >= FULL,
        provisional=end > now - PROVISIONAL,
        unresolved_wh_max=round(sum(p.unresolved_wh for p in known), 1) or None,
        allocation=Allocation.TIME_ALLOCATED if any(p.time_allocated for p in known) else Allocation.OBSERVED,
        balance_profile=profile, calculation_version=CALCULATION_VERSION, quality_reasons=reasons)


def period_complete(buckets: dict[str, list[EnergyBucket]], t: EnergyTotals, now: datetime) -> bool:
    """Coverage of the whole elapsed period per base category incl. wallbox, discharge and export (Codex P5 finding 4)
    – not every bar: one short outage in a year is not an incomplete year (user 2026-10-08). A category without a
    source is not missing data."""
    elapsed = [b for b in buckets["consumption"] if b.start < now]
    return bool(elapsed) and all(t.coverage.get(c) is None or t.coverage[c] >= FULL for c in BASE)


async def energy_period(service: EnergyService, period: str, d: date, profile: BalanceProfile,
                        now: datetime) -> EnergyPeriod:
    if period != "day":
        return await _aggregated_period(service, period, d, profile, now)
    start, end = period_bounds(period, d)
    day_list = days(start, end)
    loaded = [(dd, quarter_hours(*period_bounds("day", dd)), await service.day(dd, profile)) for dd in day_list]
    names = set().union(*(day.categories for _, _, day in loaded)) if loaded else set()
    all_slots: list[Slot] = []
    raw: dict[str, list[SlotEnergy]] = {c: [] for c in names}
    parts: dict[str, list[tuple[float, float, float, float]]] = {}
    for _, slots, day in loaded:
        all_slots += slots
        for c in names:                       # align every category to the full timeline (Codex P3 finding 1)
            raw[c].extend(day.categories.get(c) or _pad(len(slots)))
        for k, v in day.kpi_parts.items():
            parts.setdefault(k, []).extend(v)
    pub = _public(raw, len(all_slots))
    buckets: dict[str, list[EnergyBucket]] = {}
    for c in PUBLIC_CATEGORIES:
        series = pub[c]
        if period == "day":
            buckets[c] = [_bucket(s.start, s.end, s.local_label, s.utc_offset_minutes, [e], profile, now)
                          for s, e in zip(all_slots, series, strict=True)]
            continue
        groups: dict[str, list[int]] = {}
        for i, s in enumerate(all_slots):
            local = s.start.astimezone(TZ)
            groups.setdefault(local.strftime("%Y-%m") if period == "year" else local.strftime("%Y-%m-%d"), []).append(i)
        buckets[c] = [_bucket(all_slots[idx[0]].start, all_slots[idx[-1]].end, key, all_slots[idx[0]].utc_offset_minutes,
                              [series[i] for i in idx], profile, now) for key, idx in groups.items()]
    t = totals(raw, [s.start for s in all_slots if s.start < now], {k: pub[k] for k in ("generation", "home")})
    return EnergyPeriod(period=period, local_date=d.isoformat(), balance_profile=profile, buckets=buckets, totals=t,
                        kpis=kpis(raw, parts, {"generation": pub["generation"]}),
                        complete=period_complete(buckets, t, now))


async def _aggregated_period(service: EnergyService, period: str, d: date, profile: BalanceProfile,
                             now: datetime) -> EnergyPeriod:
    """Totals and KPIs from per-day aggregates. Buckets: month per day, year per ISO week (edge weeks cut at the
    year) from the aggregates; week in 6-h blocks from the slot data of its seven days."""
    start, end = period_bounds(period, d)
    day_list = days(start, end)
    slot_days: list[DayData | None] = []
    if period == "week":
        slot_days, refreshing = await service.day_slots(day_list, profile)
        aggs = [aggregate(sd) if sd else DayAggregate(dd, {}, {}, now, False) for dd, sd in zip(day_list, slot_days, strict=True)]
    else:
        aggs, refreshing = await service.day_aggregates(day_list, profile)
    names = set().union(*(a.categories for a in aggs)) if aggs else set()
    future = [period_bounds("day", a.local_date)[0] >= now for a in aggs]
    raw = {c: [a.categories.get(c) or SlotEnergy(None, 0.0, reasons={"future" if f else "no_source"})
               for a, f in zip(aggs, future, strict=True)] for c in names}
    parts: dict[str, list[tuple[float, float, float, float]]] = {}
    for a in aggs:
        for k, v in a.kpi_parts.items():
            parts.setdefault(k, []).append(v)
    pub = _public(raw, len(aggs), future)
    bounds = [period_bounds("day", dd) for dd in day_list]
    buckets: dict[str, list[EnergyBucket]] = {}
    if period == "week":
        buckets = _week_blocks(day_list, slot_days, profile, now)
    for c in PUBLIC_CATEGORIES if period != "week" else ():
        series = pub[c]
        groups: dict[str, list[int]] = {}
        for i, dd in enumerate(day_list):
            iso = dd.isocalendar()
            groups.setdefault(f"{iso.year}-W{iso.week:02d}" if period == "year" else dd.isoformat(), []).append(i)
        buckets[c] = [_bucket(bounds[idx[0]][0], bounds[idx[-1]][1], key,
                              int(bounds[idx[0]][0].astimezone(TZ).utcoffset().total_seconds() // 60),   # type: ignore[union-attr]
                              [series[i] for i in idx], profile, now) for key, idx in groups.items()]
    t = totals(raw, [b[0] for b in bounds if b[0] < now], {k: pub[k] for k in ("generation", "home")})
    return EnergyPeriod(period=period, local_date=d.isoformat(), balance_profile=profile, buckets=buckets, totals=t,
                        kpis=kpis(raw, parts, {"generation": pub["generation"]}),
                        complete=period_complete(buckets, t, now), refreshing=refreshing)


def _week_blocks(day_list: list[date], slot_days: list[DayData | None], profile: BalanceProfile,
                 now: datetime) -> dict[str, list[EnergyBucket]]:
    """Week buckets of 6 local hours from the days' 15-min slots (23-/25-h days: one 5-/7-h block); categories are
    formed per slot as in the day view, days that have not begun are 'future'."""
    slots: list[Slot] = []
    raw: dict[str, list[SlotEnergy]] = {}
    for dd, sd in zip(day_list, slot_days, strict=True):
        day_slots = quarter_hours(*period_bounds("day", dd))
        cats = sd.categories if sd else {}
        future = [True] * len(day_slots) if sd is None else None
        for c, series in _public(cats, len(day_slots), future).items():
            raw.setdefault(c, []).extend(series)
        slots += day_slots
    groups: dict[str, list[int]] = {}
    for i, s in enumerate(slots):
        local = s.start.astimezone(TZ)
        block = local.hour // WEEK_BLOCK_HOURS * WEEK_BLOCK_HOURS
        groups.setdefault(f"{local.date().isoformat()}T{block:02d}:00", []).append(i)
    return {c: [_bucket(slots[idx[0]].start, slots[idx[-1]].end, key, slots[idx[0]].utc_offset_minutes,
                        [raw[c][i] for i in idx], profile, now) for key, idx in groups.items()]
            for c in PUBLIC_CATEGORIES}


class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, JobStatus] = {}
        self._keys: dict[tuple, str] = {}
        self._tasks: set[asyncio.Task[None]] = set()   # strong references while running

    def get(self, job_id: str) -> JobStatus | None:
        return self._jobs.get(job_id)

    async def aclose(self) -> None:
        """Shutdown: cancel running jobs (they would start computations against a closing data source)."""
        tasks = list(self._tasks)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def start(self, key: tuple, work: Callable[[Callable[[float], None]], Awaitable[None]]) -> JobStatus:
        """One job per key (identical requests share it)."""
        if key in self._keys and self._jobs[self._keys[key]].state == "running":
            return self._jobs[self._keys[key]]
        job = JobStatus(job_id=uuid.uuid4().hex[:12], state="running", progress=0.0)
        self._jobs[job.job_id], self._keys[key] = job, job.job_id

        def progress(p: float) -> None:
            self._jobs[job.job_id] = self._jobs[job.job_id].model_copy(update={"progress": round(min(1.0, p), 3)})

        async def run() -> None:
            try:
                await work(progress)
                self._jobs[job.job_id] = self._jobs[job.job_id].model_copy(update={"state": "done", "progress": 1.0})
            except Exception as exc:  # noqa: BLE001 – reported to the client as a job failure (no secrets)
                self._jobs[job.job_id] = self._jobs[job.job_id].model_copy(
                    update={"state": "failed", "detail": type(exc).__name__})

        task = asyncio.get_running_loop().create_task(run())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job


def missing_days(service: EnergyService, period: str, d: date, profile: BalanceProfile) -> list[date]:
    """Days of the period that need a computation (not cached or partially computed); days that have not begun are
    never computed (empty)."""
    return service.missing(days(*period_bounds(period, d)), profile)


# --- power history ---------------------------------------------------------------------------------

def overlapping_days(start: datetime, end: datetime) -> list[date]:
    """Local dates whose day overlaps [start, end)."""
    out, d = [], start.astimezone(TZ).date()
    last = (end - timedelta(microseconds=1)).astimezone(TZ).date()
    while d <= last:
        out.append(d)
        d += timedelta(days=1)
    return out


async def power_history(service: EnergyService, ids: list[str], start: datetime, end: datetime, max_points: int,
                        profile: BalanceProfile = BalanceProfile.E3DC_BALANCE) -> PowerResponse:
    """Columns of the energy engine's validated intervals (power_history) for every local day in the range;
    bounds snapped to the 15-s column grid."""
    start, end = snap(start, end)
    cols: list[DayColumns] = []
    for d in overlapping_days(start, end):
        main = await service.power_columns(d, profile) if any(i not in EXTRA_SERIES_IDS for i in ids) else None
        extra = await service.extra_columns(d) if any(i in EXTRA_SERIES_IDS for i in ids) else None
        if main is None or extra is None:
            cols.append(main or extra)                        # type: ignore[arg-type]
        else:                                                 # same day grid: union of the series
            cols.append(DayColumns(main.start, main.n, {**main.series, **extra.series}, {**main.outages, **extra.outages},
                                   min(main.computed_at, extra.computed_at), main.final and extra.final))
    return PowerResponse(start=start, end=end, calculation_version=CALCULATION_VERSION,
                         series=power_columns_series(cols, ids, start, end, max_points))


def utc(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("timezone required")
    return dt.astimezone(UTC)
