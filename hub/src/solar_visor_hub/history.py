"""Energy service (plan §3.4, P3): per local day from InfluxDB (+ K2 hub observations), cached in SQLite.

- One collector: day computations are serialized; identical in-flight requests share one shielded task
  (a cancelled waiter never cancels the work of others).
- Day cache: final 48 h after the day's end; the current day is recomputed after 5 min, days inside the
  48-h window after 30 min; a late K2 adoption invalidates the affected days (overrides finality).
  A failed query never lands in the cache (the request fails instead).
- Counters start from the highest trusted reading of their epoch before the window (unit-normalized, with
  its timestamp) – independent of which day was computed first; the first reading after the window closes
  the last interval.
- Journal veto only for source-side unavailability, never for hub connectivity.
- CPU-heavy integration runs in a worker thread so live processing on the event loop is not blocked.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import zlib
from collections import OrderedDict
from collections.abc import Callable, Iterable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Protocol

from . import profile
from .daycalc import DayResult, RawSeries, SlotEnergy, WideSeries, compute_day, coverage, derived_slots
from .energy import Point, merge_intervals
from .epochs import (
    CALCULATION_VERSION,
    EXTRA_SERIES,
    LOOKBACK,
    PIPELINE_EVIDENCE,
    SERIES,
    STATUS_LOOKBACK,
    SeriesSpec,
    epoch_start,
    long_lookback,
    series_for,
)
from .journal import EPOCH, clip, end_at_gaps, read_monitoring_gaps
from .models import BalanceProfile
from .periods import TZ, period_bounds, quarter_hours
from .power_history import DayColumns, day_columns, series_outages

log = logging.getLogger(__name__)
FINAL_AFTER = timedelta(hours=48)
REFRESH_TODAY = timedelta(minutes=5)
REFRESH_RECENT = timedelta(minutes=30)
REFRESH_POWER_TODAY = timedelta(seconds=60)
WRITE_LATENCY = timedelta(minutes=2)   # HA → InfluxDB; later K2 adoptions invalidate on their own
# grid meter counter intervals reaching into other days: the second meter is read this far around the window
# (daycalc.WideSeries); a longer interval keeps its straight line
WIDE_BEFORE = timedelta(hours=48)
WIDE_AFTER = timedelta(hours=24)        # = the counters' look-ahead (idle_hold)
GRID_METERS = ("grid_import_meter", "grid_export_meter")
GRID_CROSS = ("grid_import_e3dc", "grid_export_e3dc")
# 2: origin of the consumption (supply_* categories, energy view bars 2026-10-05). Older aggregates are rebuilt from
# the cached slot data of the same calculation version – no recomputation from InfluxDB.
AGGREGATE_VERSION = 2
POWER_DAYS_IN_MEMORY = 16          # ≈ 4–5 MB per day of 15-s columns: two weeks (≈ 80 MB) – switching between two
                                   # weeks must not recompute both every time (Codex review 2026-10-08)
# the plant's KOSTAL AC line (profile; none in the public placeholder)
KOSTAL_AC_MEASUREMENT = profile.KOSTAL_AC_MEASUREMENT
ENERGY_MEASUREMENTS = frozenset(s.measurement for s in SERIES if s.measurement)
SCALE = {"Wh": 1.0, "kWh": 1_000.0, "MWh": 1e6, "W": 1.0, "kW": 1_000.0, "MW": 1e6}
SOURCE_INVALID = ("unavailable", "not_numeric", "not_boolean", "non_finite")

Row = tuple[datetime, float | None, str | None]


class HistorySource(Protocol):
    async def points(self, measurement: str, start: datetime, end: datetime) -> list[Row]: ...
    async def carry_in(self, measurement: str, before: datetime, lookback: timedelta) -> Row | None: ...
    async def first_after(self, measurement: str, after: datetime, lookahead: timedelta) -> Row | None: ...
    async def max_in(self, measurement: str, start: datetime, end: datetime, unit: str | None) -> tuple[datetime, float] | None: ...


HubPoints = Callable[[str, datetime, datetime], list[tuple[datetime, float, str | None]]]
InvalidIntervals = Callable[[str, datetime, datetime], list[tuple[datetime, datetime]]]


def default_unit(measurement: str) -> str | None:
    return next((s.default_unit for s in SERIES if s.measurement == measurement and s.default_unit), None)


def scale(measurement: str, value: float | None, unit: str | None) -> float | None:
    if value is None:
        return None
    if not measurement.startswith("sensor."):
        return float(value)                       # binary sensors: 1/0
    return float(value) * SCALE.get(unit or default_unit(measurement) or "", 1.0)


class RawReader:
    """Reconciled raw timeline shared by energy and power history: InfluxDB ∪ adopted hub observations
    (K2), including the carry-in (Codex P3 findings 10/16)."""

    def __init__(self, source: HistorySource, hub_points: HubPoints | None = None) -> None:
        self.source, self._hub = source, hub_points

    async def series(self, m: str, start: datetime, end: datetime, lookback: timedelta) -> list[Point]:
        carry = await self.source.carry_in(m, start, lookback)
        rows: list[Row] = list(await self.source.points(m, start, end)) if end > start else []
        if self._hub:
            before = self._hub(m, start - lookback, start)
            if before and (carry is None or before[-1][0] > carry[0]):
                carry = before[-1]                # adopted hub observation is the latest before the window
            rows = sorted(rows + list(self._hub(m, start, end)), key=lambda r: r[0])
        return [Point(t, scale(m, v, u)) for t, v, u in ([carry] if carry else []) + rows]

    async def after(self, m: str, t: datetime, lookahead: timedelta) -> Point | None:
        r = await self.source.first_after(m, t, lookahead)
        if self._hub:
            hub = self._hub(m, t, t + lookahead)
            if hub and (r is None or hub[0][0] < r[0]):
                r = hub[0]
        return Point(r[0], scale(m, r[1], r[2])) if r else None

    async def epoch_max(self, m: str, start: datetime, end: datetime) -> Point | None:
        """Highest reading in [start, end), compared after unit normalization. Points without a unit field are
        scaled with the measurement's default unit (P4: openWB counter readings of early 2025 carry none)."""
        best: Point | None = None
        candidates = [(u, await self.source.max_in(m, start, end, u)) for u in ("Wh", "kWh")]
        du = default_unit(m)
        # the unfiltered maximum also covers unit-less points; it is only comparable if no point carries a
        # unit other than the default (InfluxQL cannot select "field missing")
        if du and all(c is None for u, c in candidates if u != du):
            candidates.append((du, await self.source.max_in(m, start, end, None)))
        elif du:
            # mixed units incl. unit-less readings (not present in the data as of 2026-10): normalize raw points of
            # the most recent 400 days instead of trusting an aggregate (Codex P4 finding 3)
            log.warning("epoch_max: mixed units for %s – raw scan", m)
            rows = await self.source.points(m, max(start, end - timedelta(days=400)), end)
            for t, v, u in rows:
                sv = scale(m, v, u)
                if sv is not None and (best is None or sv > best.value):   # type: ignore[operator]
                    best = Point(t, sv)
        for unit, c in candidates:
            if c is not None:
                v = scale(m, c[1], unit)
                if v is not None and (best is None or v > best.value):   # type: ignore[operator]
                    best = Point(c[0], v)
        return best


@dataclass
class DayAggregate:
    """Per-day sums for week/month/year views (no decompression of 96 slots per day)."""
    local_date: date
    categories: dict[str, SlotEnergy]                 # wh sum, covered/applicable seconds, unresolved sum
    kpi_parts: dict[str, tuple[float, float, float, float]]
    computed_at: datetime
    final: bool


def aggregate(day: "DayData") -> DayAggregate:
    cats: dict[str, SlotEnergy] = {}
    n = len(next(iter(day.categories.values()), []))
    for c, slots in {**day.categories, **derived_slots(day.categories, n)}.items():
        app = [e for e in slots if e.wh is not None]
        reasons = set().union(*(e.reasons for e in slots)) if slots else set()
        if not app:
            cats[c] = SlotEnergy(None, 0.0, 0.0, slots[0].method if slots else "sample_hold", reasons=reasons)
            continue
        cats[c] = SlotEnergy(sum(e.wh or 0.0 for e in app), coverage(app),
                             sum(e.unresolved_wh for e in app), app[0].method, any(e.time_allocated for e in app),
                             "reconstructed" if any(e.confidence == "reconstructed" for e in app) else "observed",
                             reasons, sum(e.applicable_s for e in app))
    parts = {k: (sum(r[0] for r in v), sum(r[1] for r in v), sum(r[2] for r in v), sum(r[3] for r in v))
             for k, v in day.kpi_parts.items()}
    return DayAggregate(day.local_date, cats, parts, day.computed_at, day.final)


@dataclass
class DayData:
    local_date: date
    profile: BalanceProfile
    categories: dict[str, list[SlotEnergy]]
    kpi_parts: dict[str, list[tuple[float, float, float, float]]]
    high_water_end: dict[str, float | None]
    computed_at: datetime
    final: bool


class EnergyCache:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS days (
                local_date TEXT NOT NULL, profile TEXT NOT NULL, calc_version INTEGER NOT NULL,
                computed_at TEXT NOT NULL, final INTEGER NOT NULL, payload BLOB NOT NULL,
                PRIMARY KEY (local_date, profile))""")
            db.execute("""CREATE TABLE IF NOT EXISTS day_aggregates (
                local_date TEXT NOT NULL, profile TEXT NOT NULL, calc_version INTEGER NOT NULL,
                computed_at TEXT NOT NULL, final INTEGER NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY (local_date, profile))""")

    def get(self, d: date, profile: BalanceProfile) -> DayData | None:
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute("SELECT calc_version, computed_at, final, payload FROM days WHERE local_date=? AND profile=?",
                             (d.isoformat(), profile.value)).fetchone()
        if row is None or row[0] != CALCULATION_VERSION or _before_day(d, row[1]):
            return None
        p = json.loads(zlib.decompress(row[3]))
        cats = {k: [SlotEnergy(e[0], e[1], e[2], e[3], bool(e[4]), e[5], set(e[6]), e[7]) for e in v]
                for k, v in p["categories"].items()}
        parts = {k: [tuple(r) for r in v] for k, v in p["kpi_parts"].items()}
        return DayData(d, profile, cats, parts, p["hwm"], datetime.fromisoformat(row[1]), bool(row[2]))

    def put(self, day: DayData) -> None:
        payload = {"categories": {k: [[e.wh, e.coverage, e.unresolved_wh, e.method, e.time_allocated, e.confidence,
                                       sorted(e.reasons), e.applicable_s] for e in v] for k, v in day.categories.items()},
                   "kpi_parts": day.kpi_parts, "hwm": day.high_water_end}
        blob = zlib.compress(json.dumps(payload, separators=(",", ":")).encode())
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT OR REPLACE INTO days VALUES (?, ?, ?, ?, ?, ?)",
                       (day.local_date.isoformat(), day.profile.value, CALCULATION_VERSION,
                        day.computed_at.isoformat(), int(day.final), blob))
            db.execute("INSERT OR REPLACE INTO day_aggregates VALUES (?, ?, ?, ?, ?, ?)", self._aggregate_row(day))

    def put_aggregates(self, rebuilt: Iterable[tuple[DayData, DayAggregate]]) -> set[date]:
        """Rewrites only the aggregates of cached days (rebuilt after an aggregate version change), keeping their
        ``computed_at``/``final``. Each row is written only while its source slot data is still the cached revision –
        a refresh or invalidation in between wins (Codex review 2026-10-05). Returns the days actually written."""
        written: set[date] = set()
        with closing(sqlite3.connect(self.path)) as db, db:
            for d, a in rebuilt:
                cur = db.execute("INSERT OR REPLACE INTO day_aggregates SELECT ?, ?, ?, ?, ?, ? WHERE EXISTS ("
                                 "SELECT 1 FROM days WHERE local_date=? AND profile=? AND calc_version=? AND computed_at=?)",
                                 (*self._aggregate_row(d, a), d.local_date.isoformat(), d.profile.value,
                                  CALCULATION_VERSION, d.computed_at.isoformat()))
                if cur.rowcount == 1:
                    written.add(d.local_date)
        return written

    @staticmethod
    def _aggregate_row(day: DayData, agg: DayAggregate | None = None) -> tuple:
        agg = agg or aggregate(day)
        payload = json.dumps({"v": AGGREGATE_VERSION,
                              "categories": {k: [e.wh, e.coverage, e.unresolved_wh, e.method, e.time_allocated,
                                                 e.confidence, sorted(e.reasons), e.applicable_s]
                                             for k, e in agg.categories.items()},
                              "kpi_parts": agg.kpi_parts}, separators=(",", ":"))
        return (day.local_date.isoformat(), day.profile.value, CALCULATION_VERSION, day.computed_at.isoformat(),
                int(day.final), payload)

    def aggregates(self, first: date, last: date, profile: BalanceProfile) -> dict[date, DayAggregate]:
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute("SELECT local_date, computed_at, final, payload FROM day_aggregates WHERE profile=? AND "
                              "calc_version=? AND local_date BETWEEN ? AND ?",
                              (profile.value, CALCULATION_VERSION, first.isoformat(), last.isoformat())).fetchall()
        out = {}
        for d, ca, fin, payload in rows:
            p = json.loads(payload)
            if p.get("v") != AGGREGATE_VERSION or _before_day(date.fromisoformat(d), ca):   # rebuilt / recomputed
                continue
            cats = {k: SlotEnergy(e[0], e[1], e[2], e[3], bool(e[4]), e[5], set(e[6]), e[7])
                    for k, e in p["categories"].items()}
            out[date.fromisoformat(d)] = DayAggregate(date.fromisoformat(d), cats,
                                                      {k: tuple(v) for k, v in p["kpi_parts"].items()},
                                                      datetime.fromisoformat(ca), bool(fin))
        return out

    def day_meta(self, first: date, last: date, profile: BalanceProfile) -> dict[date, tuple[datetime, bool]]:
        """``computed_at`` and ``final`` of the cached days (rows computed before their day began excluded)."""
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute("SELECT local_date, computed_at, final FROM days WHERE profile=? AND calc_version=? AND "
                              "local_date BETWEEN ? AND ?",
                              (profile.value, CALCULATION_VERSION, first.isoformat(), last.isoformat())).fetchall()
        return {date.fromisoformat(d): (datetime.fromisoformat(ca), bool(fin)) for d, ca, fin in rows
                if not _before_day(date.fromisoformat(d), ca)}

    def cached_dates(self, first: date, last: date, profile: BalanceProfile) -> set[date]:
        return set(self.day_meta(first, last, profile))

    def computed_before(self, dates: Iterable[date], before: datetime) -> set[date]:
        """Cached days (any profile) whose result was computed before ``before`` – what an invalidation would drop."""
        ds = sorted({d.isoformat() for d in dates})
        if not ds:
            return set()
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute(f"SELECT DISTINCT local_date FROM days WHERE computed_at < ? AND local_date IN "
                              f"({','.join('?' * len(ds))})", (before.isoformat(), *ds)).fetchall()
        return {date.fromisoformat(r[0]) for r in rows}

    def invalidate(self, dates: Iterable[date], computed_before: datetime | None = None) -> int:
        """Drops cached days; with ``computed_before`` only results computed before that time."""
        before = (computed_before or datetime.max.replace(tzinfo=UTC)).isoformat()
        keys = [(d.isoformat(), before) for d in set(dates)]
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executemany("DELETE FROM day_aggregates WHERE local_date=? AND computed_at < ?", keys)
            cur = db.executemany("DELETE FROM days WHERE local_date=? AND computed_at < ?", keys)
            return cur.rowcount


def _before_day(d: date, computed_at: str) -> bool:
    """A result computed before its day began (cached by hubs before the P5 review) holds no data – never served."""
    return datetime.fromisoformat(computed_at) < period_bounds("day", d)[0]


def _log_failure(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception() is not None:
        log.warning("background refresh failed: %s", type(task.exception()).__name__)


LONG_LOOKBACK = frozenset(s.measurement for s in SERIES if s.measurement and long_lookback(s))


def affected_dates(times: Iterable[datetime], long: bool = False) -> set[date]:
    """Local dates whose result can change when observations at ``times`` change: the day itself and the next day
    through its carry-in – for long-lookback measurements every day up to STATUS_LOOKBACK later (Codex review
    2026-10-08: an adopted E3/DC PV reading can be the carry-in of many later days)."""
    span = STATUS_LOOKBACK.days + 1 if long else 1
    return {t.astimezone(TZ).date() + timedelta(days=i) for t in times for i in range(span + 1)}


class EnergyService:
    def __init__(self, source: HistorySource, cache: EnergyCache, invalid: InvalidIntervals | None = None,
                 hub_points: HubPoints | None = None, now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self.reader = RawReader(source, hub_points)
        self.cache, self._invalid, self._now = cache, invalid, now
        self._lock = asyncio.Lock()                      # one collector
        self._inflight: dict[tuple[date, BalanceProfile], asyncio.Task[DayData]] = {}
        self._power: OrderedDict[tuple[date, BalanceProfile], DayColumns] = OrderedDict()
        self._power_inflight: dict[tuple[date, BalanceProfile], asyncio.Task[DayColumns]] = {}
        self._extra: OrderedDict[date, DayColumns] = OrderedDict()         # motorhome and PV detail charts
        self._extra_inflight: dict[tuple[date], asyncio.Task[DayColumns]] = {}
        self._generation: dict[date, int] = {}           # bumped by invalidate(): in-flight results become stale
        self._tasks: set[asyncio.Task] = set()           # every computation incl. ones dropped by invalidate()
        self._closed = False
        self._adopted_at: dict[str, datetime] = {}       # K2, per measurement: available long after its timestamp

    def now(self) -> datetime:
        return self._now()

    def invalidate(self, dates: Iterable[date], computed_before: datetime | None = None) -> int:
        """Drops cached day results and power columns (late K2 adoption, closed counter intervals). A computation
        already running for such a day must not publish its result, and new callers must not join it
        (Codex P5 WP1b finding 1). ``computed_before``: only results older than that (a counter interval closed
        by a reading – results computed after it already saw the reading through the lookahead)."""
        ds = set(dates)
        bump = ds
        if computed_before is not None:
            # only days whose state actually changes: a dropped row, a dropped column cache or a running computation
            # (it may not have seen the closing reading) – a no-op must not make comparisons look inconsistent
            # (code review 2026-10-06)
            bump = self.cache.computed_before(ds, computed_before)
            bump |= {k[0] for store in (self._inflight, self._power_inflight, self._extra_inflight) for k in store
                     if k[0] in ds}
            bump |= {d for d, v in self._extra.items() if d in ds and v.computed_at < computed_before}
            bump |= {k[0] for k, v in self._power.items() if k[0] in ds and v.computed_at < computed_before}
        for d in bump:
            self._generation[d] = self._generation.get(d, 0) + 1
        for store in (self._inflight, self._power_inflight, self._extra_inflight):
            for key in [k for k in store if k[0] in ds]:
                store.pop(key, None)
        for d in [d for d, v in self._extra.items() if d in ds and (computed_before is None or v.computed_at < computed_before)]:
            self._extra.pop(d, None)
        for key in [k for k, v in self._power.items() if k[0] in ds
                    and (computed_before is None or v.computed_at < computed_before)]:
            self._power.pop(key, None)
        return self.cache.invalidate(ds, computed_before)

    def adopted(self, observations: Iterable[tuple[str, datetime]]) -> int:
        """K2 adopted hub observations. Only measurements of the energy engine matter (live-only entities such as
        IOmeter phase powers are adopted every few minutes and must not touch energy results): their days are
        invalid, and – because a counter reading may close an interval spanning earlier days – the availability
        time per measurement is remembered for the counter-span rule (Codex P5 WP1b verify finding 1)."""
        relevant = [(m, t) for m, t in observations if m in ENERGY_MEASUREMENTS]
        for m, _t in relevant:
            self._adopted_at[m] = self._now()
        if not relevant:
            return 0
        return self.invalidate(affected_dates(t for m, t in relevant if m not in LONG_LOOKBACK)
                               | affected_dates((t for m, t in relevant if m in LONG_LOOKBACK), long=True))

    async def extra_columns(self, d: date) -> DayColumns:
        """15-s columns of the motorhome and PV detail charts (strings, KOSTAL DC sum): own measurements, not part of
        the energy balance, so computed and cached separately – the main power columns stay as fast."""
        cols = self._extra.get(d)
        if cols is not None:
            _, end = period_bounds("day", d)
            if cols.final or self._now() - cols.computed_at < (REFRESH_POWER_TODAY if self._now() < end else REFRESH_RECENT):
                self._extra.move_to_end(d)
                return cols
        task = self._join(self._extra_inflight, (d,), lambda: asyncio.get_running_loop().create_task(self._run_extra(d)))
        return await asyncio.shield(task)

    async def _run_extra(self, d: date) -> DayColumns:
        generation = self._generation.get(d, 0)
        start, end = period_bounds("day", d)
        now = self._now()
        fetch_end = max(start, min(end, now))
        specs = [s for s in EXTRA_SERIES if s.valid_to > start and s.valid_from < end]
        raw: dict[str, RawSeries] = {}
        invalid: dict[str, list[tuple[datetime, datetime]]] = {}
        for s in specs:
            raw[s.measurement] = RawSeries(await self.reader.series(
                s.measurement, start, fetch_end, STATUS_LOOKBACK if s.kind == "soc" else LOOKBACK))
            if self._invalid:
                invalid[s.measurement] = self._invalid(s.measurement, start, fetch_end)
        evidence = await self.reader.series(PIPELINE_EVIDENCE, start, fetch_end, LOOKBACK)
        slots = quarter_hours(start, end)
        power = await asyncio.to_thread(compute_day, slots, [s for s in specs if s.kind != "soc"], raw, invalid, fetch_end,
                                        evidence)
        soc = await asyncio.to_thread(compute_day, slots, [s for s in specs if s.kind == "soc"], raw, invalid, fetch_end)
        own = {c for s in specs if s.kind != "soc" for c in s.categories}
        timelines = {c: t for c, t in power.timelines.items() if c in own}
        if "battery_soc" in soc.timelines:                    # compute_day names every SOC timeline battery_soc
            timelines["womo_soc"] = soc.timelines["battery_soc"]
        deps = [("womo_soc" if c == "battery_soc" else c, s.measurement, s.valid_from, s.valid_to)
                for s in specs for c in s.categories]
        cols = await asyncio.to_thread(day_columns, timelines, start, end, series_outages(deps, invalid), now,
                                       end + FINAL_AFTER <= now)       # late InfluxDB writes: same rule as the days
        if self._generation.get(d, 0) != generation:
            return cols                                      # invalidated while computing – not cached
        self._extra[d] = cols
        self._extra.move_to_end(d)
        while len(self._extra) > POWER_DAYS_IN_MEMORY:
            self._extra.popitem(last=False)
        return cols

    def _join(self, store: dict, key: tuple, start: Callable[[], asyncio.Task]) -> asyncio.Task:
        task = store.get(key)
        if task is None:
            if self._closed:
                raise RuntimeError("energy service closed")
            task = start()
            store[key] = task
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            # only remove *this* task – an invalidation may already have registered a newer one under the key
            task.add_done_callback(lambda t, k=key: store.pop(k) if store.get(k) is t else None)
        return task

    def partial(self, computed_at: datetime, final: bool, d: date) -> bool:
        """Computed while the day was still running although it has ended (plus the write latency): its tail is
        missing while its coverage looks complete (slots after the computation are not applicable). Such a day is
        never fresh – it is recomputed like a missing day, never served stale (comparison plan 2026-10-06). The day
        that ended less than the write latency ago is still running for everyone (no repeated recomputation)."""
        if final:
            return False
        _, end = period_bounds("day", d)
        return end + WRITE_LATENCY <= self._now() and computed_at < end + WRITE_LATENCY

    def missing(self, days_: list[date], profile: BalanceProfile) -> list[date]:
        """Days that have begun and need a computation: not cached or only partially computed (``partial``)."""
        if not days_:
            return []
        meta = self.cache.day_meta(min(days_), max(days_), profile)
        now = self._now()
        return [d for d in days_ if period_bounds("day", d)[0] < now
                and (d not in meta or self.partial(meta[d][0], meta[d][1], d))]

    def generations(self, dates: Iterable[date]) -> dict[date, int]:
        """Invalidation counters of the days: a change between two reads means a day was invalidated meanwhile."""
        return {d: self._generation.get(d, 0) for d in dates}

    def _fresh(self, day: DayData) -> bool:
        if day.final:
            return True
        if self.partial(day.computed_at, day.final, day.local_date):
            return False
        _, end = period_bounds("day", day.local_date)
        age = self._now() - day.computed_at
        return age < (REFRESH_TODAY if self._now() < end else REFRESH_RECENT)

    async def day(self, d: date, profile: BalanceProfile = BalanceProfile.E3DC_BALANCE) -> DayData:
        cached = self.cache.get(d, profile)
        if cached and self._fresh(cached):
            return cached
        key = (d, profile)
        task = self._join(self._inflight, key, lambda: asyncio.get_running_loop().create_task(self._run(d, profile)))
        return await asyncio.shield(task)               # waiters may be cancelled, the shared work is not

    def _fresh_meta(self, computed_at: datetime, final: bool, d: date) -> bool:
        if final:
            return True
        if self.partial(computed_at, final, d):
            return False
        _, end = period_bounds("day", d)
        return self._now() - computed_at < (REFRESH_TODAY if self._now() < end else REFRESH_RECENT)

    async def day_aggregates(self, days_: list[date], profile: BalanceProfile) -> tuple[list[DayAggregate], bool]:
        """Aggregates for many days (week/month/year) and whether some are being refreshed. Days that have not begun
        are empty (never computed, cached or refreshed). Missing and partially computed days (``partial``) are
        computed; a cached but no longer fresh day (today, the 48-h window) is served as is and refreshed in the
        background – the period views stay fast, the day view keeps its own freshness (stale-while-revalidate; the
        client re-requests)."""
        if not days_:
            return [], False
        now = self._now()
        cached = self.cache.aggregates(days_[0], days_[-1], profile)
        # aggregate version changed: rebuild from the cached slot data in a worker thread and persist at once, before
        # any computation below can fail (review 2026-10-05)
        older = [d for d in days_ if d not in cached and period_bounds("day", d)[0] < now]
        if older:
            cached |= await asyncio.to_thread(self._rebuild_aggregates, older, profile)
        out, refreshing = [], False
        for d in days_:
            if period_bounds("day", d)[0] >= now:
                out.append(DayAggregate(d, {}, {}, now, False))
                continue
            a = cached.get(d)
            if a is None or self.partial(a.computed_at, a.final, d):
                a = aggregate(await self.day(d, profile))
            elif not self._fresh_meta(a.computed_at, a.final, d):
                self._refresh_in_background(d, profile)
                refreshing = True
            out.append(a)
        return out, refreshing

    def _rebuild_aggregates(self, days_: list[date], profile: BalanceProfile) -> dict[date, DayAggregate]:
        """Only aggregates that were persisted are served: a day invalidated or refreshed meanwhile is left to the
        regular computation below (Codex review 2026-10-05)."""
        rebuilt = [(day, aggregate(day)) for d in days_ if (day := self.cache.get(d, profile)) is not None]
        written = self.cache.put_aggregates(rebuilt) if rebuilt else set()
        return {day.local_date: a for day, a in rebuilt if day.local_date in written}

    async def day_slots(self, days_: list[date], profile: BalanceProfile) -> tuple[list[DayData | None], bool]:
        """Slot data of a few days (week in 6-h buckets) with the freshness of ``day_aggregates``: missing and partially
        computed days are computed, a cached but no longer fresh day is served as is and refreshed in the background
        (``refreshing``); days that have not begun are None – never computed or cached."""
        now = self._now()
        out: list[DayData | None] = []
        refreshing = False
        for d in days_:
            if period_bounds("day", d)[0] >= now:
                out.append(None)
                continue
            day = self.cache.get(d, profile)
            if day is None or self.partial(day.computed_at, day.final, d):
                day = await self.day(d, profile)
            elif not self._fresh(day):
                self._refresh_in_background(d, profile)
                refreshing = True
            out.append(day)
        return out, refreshing

    async def aclose(self) -> None:
        """Shutdown: no new computations; cancel and await all running ones (also those an invalidation detached
        from the in-flight maps) before their data source is closed."""
        self._closed = True
        tasks = list(self._tasks)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def _refresh_in_background(self, d: date, profile: BalanceProfile) -> None:
        if self._closed:
            return
        def start() -> asyncio.Task:
            task = asyncio.get_running_loop().create_task(self._run(d, profile))
            task.add_done_callback(_log_failure)
            return task
        self._join(self._inflight, (d, profile), start)

    async def power_columns(self, d: date, profile: BalanceProfile = BalanceProfile.E3DC_BALANCE) -> DayColumns:
        """15-s power columns of a local day from the same computation as its energy (in memory, LRU)."""
        key = (d, profile)
        cols = self._power.get(key)
        if cols is not None:
            _, end = period_bounds("day", d)
            age = self._now() - cols.computed_at
            if cols.final or age < (REFRESH_POWER_TODAY if self._now() < end else REFRESH_RECENT):
                self._power.move_to_end(key)
                return cols
        task = self._join(self._power_inflight, key,
                          lambda: asyncio.get_running_loop().create_task(self._run_power(d, profile)))
        return await asyncio.shield(task)

    async def _run_power(self, d: date, profile: BalanceProfile) -> DayColumns:
        async with self._lock:
            generation = self._generation.get(d, 0)
            day, result, ctx = await self._compute_full(d, profile)
            start, end, fetch_end, slots, specs, invalid, evidence = ctx
            timelines = dict(result.timelines)
            # optional KOSTAL AC line (UI spec §3): its own measurement, independent of the balance profile
            spec = next((s for s in series_for(start, end, BalanceProfile.KOSTAL_AC_COMPARISON)
                         if s.measurement == KOSTAL_AC_MEASUREMENT), None)
            if spec is not None and fetch_end > start:
                m = spec.measurement
                raw_k = {m: RawSeries(await self.reader.series(m, start, fetch_end, LOOKBACK))}
                inv_k = {m: self._invalid(m, start, fetch_end)} if self._invalid else {}
                invalid = {**invalid, **inv_k}
                kostal = await asyncio.to_thread(compute_day, slots, [spec], raw_k, inv_k, fetch_end, evidence)
                if "external" in kostal.timelines:
                    timelines["kostal_ac"] = kostal.timelines["external"]
            deps = [(c, sp.measurement, sp.valid_from, sp.valid_to) for sp in specs if sp.measurement
                    for c in sp.categories]
            if spec is not None:
                deps.append(("kostal_ac", spec.measurement, spec.valid_from, spec.valid_to))
            cols = await asyncio.to_thread(day_columns, timelines, start, end, series_outages(deps, invalid),
                                           day.computed_at, day.final)
        if self._generation.get(d, 0) != generation:
            log.info("%s invalidated while computing – result not cached", d)
            return cols
        self.cache.put(day)
        self._power[(d, profile)] = cols
        self._power.move_to_end((d, profile))
        while len(self._power) > POWER_DAYS_IN_MEMORY:
            self._power.popitem(last=False)
        return cols

    async def _run(self, d: date, profile: BalanceProfile) -> DayData:
        async with self._lock:
            generation = self._generation.get(d, 0)
            result = await self._compute(d, profile)
        if self._generation.get(d, 0) != generation:
            log.info("%s invalidated while computing – result not cached", d)
        else:
            self.cache.put(result)
        return result

    async def _compute(self, d: date, profile: BalanceProfile) -> DayData:
        day, _, _ = await self._compute_full(d, profile)
        return day

    async def _wide(self, specs: list[SeriesSpec], raw: dict[str, RawSeries], start: datetime, end: datetime,
                    fetch_end: datetime) -> dict[str, WideSeries]:
        """The grid meter's counter intervals that reach into the previous or next day (daycalc ``SILENT_METER_WH``):
        the second meter's readings and both measurements' journal vetoes from the meter's last reading before the
        window to its first reading after it (at most WIDE_BEFORE / WIDE_AFTER)."""
        meters = [s for s in specs if s.kind == "counter" and s.categories[0] in GRID_METERS]
        cross = sorted({s.measurement for s in specs if s.kind == "power" and set(s.categories) & set(GRID_CROSS)})
        if not meters or not cross:
            return {}
        series = [raw.get(s.measurement, RawSeries([])) for s in meters]
        times = [p.t for r in series for p in r.points if p.value is not None] + [r.base.t for r in series if r.base]
        lo = max(start - WIDE_BEFORE, max((t for t in times if t < start), default=start))
        hi = min(end + WIDE_AFTER, min((t for t in times if t >= end), default=end)) if fetch_end >= end else fetch_end
        out: dict[str, WideSeries] = {}
        for m in cross:
            inside = raw.get(m, RawSeries([])).points
            before = await self.reader.series(m, lo, start, LOOKBACK) if lo < start else [p for p in inside if p.t < start]
            after = [p for p in await self.reader.series(m, end, hi, LOOKBACK) if p.t >= end] if hi > end else []
            out[m] = WideSeries(lo, hi, before + [p for p in inside if p.t >= start] + after,
                                self._invalid(m, lo, hi) if self._invalid else [])
        for s in meters:
            out[s.measurement] = WideSeries(lo, hi, [], self._invalid(s.measurement, lo, hi) if self._invalid else [])
        return out

    async def _compute_full(self, d: date, profile: BalanceProfile) -> tuple[DayData, DayResult, tuple]:
        start, end = period_bounds("day", d)
        now = self._now()
        fetch_end = max(start, min(end, now))
        specs = series_for(start, end, profile)
        raw: dict[str, RawSeries] = {}
        invalid: dict[str, list[tuple[datetime, datetime]]] = {}
        async def fetch(m: str) -> None:
            spec = next(s for s in specs if s.measurement == m)
            kind = spec.kind
            pts = await self.reader.series(m, start, fetch_end, STATUS_LOOKBACK if long_lookback(spec) else LOOKBACK)
            base = None
            if kind == "counter":
                if fetch_end >= end:
                    hold = max(s.idle_hold for s in specs if s.measurement == m)
                    nxt = await self.reader.after(m, end, hold)
                    if nxt is not None:
                        pts.append(nxt)
                es = epoch_start(m, start)
                base = await self.reader.epoch_max(m, es, start) if es < start else None
            raw[m] = RawSeries(pts, base)
            if self._invalid:
                invalid[m] = self._invalid(m, start, fetch_end)

        # measurements concurrently; the InfluxDB source keeps its own limit of parallel queries. A failure cancels
        # the remaining queries and propagates unchanged (upstream error → 503, nothing cached).
        tasks = [asyncio.ensure_future(fetch(m)) for m in sorted({s.measurement for s in specs if s.measurement})]
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        slots = quarter_hours(start, end)
        wide = await self._wide(specs, raw, start, end, fetch_end)
        result = await asyncio.to_thread(compute_day, slots, specs, raw, invalid, fetch_end, None, wide)
        for m, (spans_from, closed_at) in result.counter_spans.items():
            # a counter interval closed inside this day began on earlier days: their cached results (computed before
            # the closing reading existed, possibly already final) must be recomputed (Codex P4 finding 5)
            first = spans_from.astimezone(TZ).date()
            stale = [first + timedelta(days=i) for i in range((d - first).days)]
            if stale:
                # results computed before the closing reading was *available*: written to InfluxDB (≈ its timestamp)
                # or adopted later by K2
                # only this counter's own availability matters (Codex P5 views review finding 7)
                closed = closed_at + WRITE_LATENCY
                if m in self._adopted_at:
                    closed = max(closed, self._adopted_at[m])
                self.invalidate(stale, computed_before=closed)
        day = DayData(d, profile, result.categories, result.kpi_parts, result.high_water_end, now,
                      end + FINAL_AFTER <= now)
        return day, result, (start, end, fetch_end, slots, specs, invalid,
                             raw.get(PIPELINE_EVIDENCE, RawSeries([])).points)


def journal_invalid(journal_db: Path, stale: bool = False) -> InvalidIntervals:
    """Intervals in which a metric fed by the entity was unknown for source reasons (journal transitions),
    excluding monitoring gaps of the hub. ``stale``: a source that stopped reporting (``report_too_old``, judged by
    HA's ``last_reported``) counts as well – the generic history of a plant (user decision 2026-11-15: a value that
    nobody confirms is no measurement); the existing calculation keeps its rules."""
    def query(entity: str, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
        try:
            with closing(sqlite3.connect(journal_db)) as db:
                rows = db.execute("SELECT metric_id, at_utc, state, reasons FROM metric_quality WHERE entity=? "
                                  "AND at_utc < ? ORDER BY metric_id, at_utc", (entity, end.isoformat())).fetchall()
                gaps = read_monitoring_gaps(db, EPOCH, end)
        except sqlite3.Error:
            return []
        out, open_since = [], {}
        for mid, at, state, reasons in rows:
            t = datetime.fromisoformat(at)
            bad = (state == "unknown" and any(r in (reasons or "") for r in SOURCE_INVALID)) or \
                (stale and state == "stale" and "report_too_old" in (reasons or ""))
            if bad and mid in open_since and any(g0 < t and g1 > open_since[mid] for g0, g1 in gaps):
                out.append((open_since.pop(mid), t))   # ended at the gap; the row after the restart is new evidence
            if bad and mid not in open_since:
                open_since[mid] = t
            elif not bad and mid in open_since:
                a = open_since.pop(mid)
                if t > start:
                    out.append((a, t))
        for a in open_since.values():
            out.append((a, end))
        # a veto ends where monitoring stopped and needs new evidence after a restart; while the hub was down
        # InfluxDB stands alone, as for all history before the hub existed (Codex P5 findings 5, WP1b 4)
        return merge_intervals(clip(end_at_gaps(out, gaps), start, end))
    return query
