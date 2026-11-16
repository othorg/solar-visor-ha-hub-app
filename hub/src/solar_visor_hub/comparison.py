"""Comparison of two periods (comparison plan 2026-10-06 v2, §3) – pure functions; ``comparison_api`` loads the days.

- Periods are local calendar days [first, end) in Europe/Berlin. A running period is cut at ``cut`` – the last quarter
  hour covered by the computed data and the write latency – and the other period at the same offset (same clock time
  on the paired day). Stored quarter hours are never split.
- Bases: ``aligned`` (only paired, elapsed sections – days without a partner, a DST hour that exists on one side
  only and everything after the cut are excluded and listed), ``totals`` (actual sums, a running period "so far"),
  ``per_day`` (sum of the calendar-complete days ÷ their number). The hub decides which bases make sense.
- Quality: coverage against the expected calendar seconds (solar measures: daylight only – the PV night rule covers
  the night even without data), unknown is null and never 0; tiers from coverage and directed uncertainty bounds;
  percent only against a positive reference; ratios in percentage points.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache

from .daycalc import SlotEnergy, derived_slots
from .epochs import CALCULATION_VERSION, DATA_SINCE, IOMETER_FROM
from .history import WRITE_LATENCY, DayAggregate, DayData, aggregate
from .kpis import FULL
from .models import (
    BalanceProfile,
    CompareAxisPoint,
    CompareBasis,
    CompareBasisOption,
    CompareDelta,
    CompareExclusion,
    CompareMetric,
    CompareNote,
    CompareSeries,
    CompareSide,
    CompareTier,
    CompareValue,
    EnergyComparison,
    Unit,
)
from .periods import TZ, local_midnight_utc, period_bounds, quarter_hours
from .plant_events import events_between
from .sun import night_intervals

KINDS = ("day", "week", "month", "year", "range")
MAX_DAYS = 366
DAILY_POINTS_MAX = 61           # free ranges up to 61 days: one point per day, from 62 days 7-day blocks (plan §2)
MIN_COVERAGE = 0.95
TOLERANCE_WH = 50.0             # change tolerance max(50 Wh, 1 %) – same order as the balance threshold T_E
TOLERANCE_SHARE = 0.01
COVERED_EPS_S = 1.0             # covered time below this is none (unknown, never 0)
NIGHT_TOLERANCE_S = 60.0        # PV: daylight coverage this small without energy is the night grid, not data
RATIO_TOLERANCE_PP = 0.05       # a ratio change below the displayed precision (0,1 percentage points) is unchanged

ENERGY_METRICS = ("generation", "pv_internal", "external", "consumption", "home", "wallbox", "battery_charge",
                  "battery_discharge", "grid_import", "grid_export")
RATIO_METRICS = ("autarky", "self_consumption")
SUPPLEMENTARY = ("grid_import_e3dc", "grid_export_e3dc")
SOLAR = frozenset({"generation", "pv_internal", "external"})
RATIO_INPUTS = {"autarky": ("consumption", "grid_import"), "self_consumption": ("pv_internal", "external", "grid_export")}
TWO_SIDED = frozenset({"home"})      # consumption − wallbox: missing wallbox energy can lower it


class CompareError(ValueError):
    """Invalid request (HTTP 422)."""


@dataclass(frozen=True)
class Range:
    first: date
    end: date                        # exclusive

    @property
    def length(self) -> int:
        return (self.end - self.first).days

    def days(self) -> list[date]:
        return [self.first + timedelta(days=i) for i in range(self.length)]

    def contains(self, d: date) -> bool:
        return self.first <= d < self.end

    @property
    def start_utc(self) -> datetime:
        return local_midnight_utc(self.first)

    @property
    def end_utc(self) -> datetime:
        return local_midnight_utc(self.end)


def validate(kind: str, a: Range, b: Range, today: date) -> None:
    """422 only for empty or too long ranges, a kind period that is not whole, a period lying entirely before the data
    start or beginning after today. A current period (end in the future) and one reaching back before the data start
    are allowed – the cut and the coverage handle them."""
    if kind not in KINDS:
        raise CompareError(f"unknown kind {kind!r}")
    for name, r in (("a", a), ("b", b)):
        if r.end <= r.first:
            raise CompareError(f"{name}: empty range")
        if r.length > MAX_DAYS:
            raise CompareError(f"{name}: more than {MAX_DAYS} days")
        if r.end <= DATA_SINCE:
            raise CompareError(f"{name}: before the data start {DATA_SINCE.isoformat()}")
        if r.first > today:
            raise CompareError(f"{name}: begins in the future")
        try:
            whole = period_bounds(kind, r.first) == (r.start_utc, r.end_utc) if kind != "range" else True
        except (ValueError, OverflowError):
            whole = False
        if not whole:
            raise CompareError(f"{name}: not a whole {kind}")


def floor15(t: datetime) -> datetime:
    t = t.astimezone(UTC)
    return t.replace(minute=t.minute - t.minute % 15, second=0, microsecond=0)


def data_day(now: datetime) -> date:
    """Local day whose data the latest quarter hours belong to (the day that ended less than the write latency ago is
    still running)."""
    return (now - WRITE_LATENCY).astimezone(TZ).date()


def cut_time(now: datetime, computed_at: datetime | None) -> datetime:
    """Last quarter-hour boundary covered by the computation of the running day and by the write latency."""
    return floor15((min(now, computed_at) if computed_at else now) - WRITE_LATENCY)


# --- calendar pairing -------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Pair:
    index: int
    a: date | None
    b: date | None
    group: int                       # axis position (non-day kinds)


def pairs(kind: str, a: Range, b: Range) -> list[Pair]:
    """Day pairs: day ↔ day, week by weekday, month by day of month, year by month and day, range by day index. A day
    without a partner (31st, Feb 29, the longer free range) gets None on the other side."""
    if kind == "day":
        return [Pair(0, a.first, b.first, 0)]
    if kind == "year":
        am, bm = {(d.month, d.day): d for d in a.days()}, {(d.month, d.day): d for d in b.days()}
        return [Pair(i, am.get(k), bm.get(k), k[0] - 1) for i, k in enumerate(sorted(am.keys() | bm.keys()))]
    n = max(a.length, b.length)
    blocks = kind == "range" and n > DAILY_POINTS_MAX
    return [Pair(i, a.first + timedelta(days=i) if i < a.length else None,
                 b.first + timedelta(days=i) if i < b.length else None, i // 7 if blocks else i) for i in range(n)]


def group_key(kind: str, group: int, blocks: bool) -> str:
    if kind == "year":
        return str(group + 1)
    return f"B{group + 1}" if blocks else str(group + 1)


@lru_cache(maxsize=1024)
def slot_keys(d: date) -> tuple[str, ...]:
    """Local clock key per 15-min slot; the repeated hour of the 25-h day is 'HH:MM#2'."""
    seen: set[str] = set()
    out = []
    for s in quarter_hours(*period_bounds("day", d)):
        out.append(s.local_label if s.local_label not in seen else f"{s.local_label}#2")
        seen.add(s.local_label)
    return tuple(out)


def key_order(key: str) -> tuple[int, int, int]:
    hh, rest = key.split(":")
    mm, _, occ = rest.partition("#")
    return int(hh), int(occ or 1), int(mm)


@lru_cache(maxsize=4096)
def _nights(d: date) -> tuple[tuple[datetime, datetime], ...]:
    return tuple(night_intervals(*period_bounds("day", d)))


@lru_cache(maxsize=4096)
def night_seconds(d: date) -> float:
    """Seconds of astronomical night of a day (same function as the engine's PV night rule)."""
    return sum((e - a).total_seconds() for a, e in _nights(d))


@lru_cache(maxsize=1024)
def night_by_slot(d: date) -> tuple[float, ...]:
    """Seconds of astronomical night per slot (only for days compared per quarter hour)."""
    nights = _nights(d)
    return tuple(sum(max(0.0, (min(e, s.end) - max(a, s.start)).total_seconds()) for a, e in nights)
                 for s in quarter_hours(*period_bounds("day", d)))


# --- sections -------------------------------------------------------------------------------------------------------

@dataclass
class Section:
    day: date
    start: datetime
    end: datetime
    expected_s: float
    night_s: float
    cats: dict[str, SlotEnergy]
    kpi: dict[str, tuple[float, float, float, float]]
    provisional: bool = False


def day_section(agg: DayAggregate) -> Section:
    start, end = period_bounds("day", agg.local_date)
    return Section(agg.local_date, start, end, (end - start).total_seconds(), night_seconds(agg.local_date),
                   agg.categories, agg.kpi_parts)


def slice_section(day: DayData, idx: Sequence[int], provisional: bool = False) -> Section | None:
    """Section of selected slots: the categories and KPI parts are cut to the slots and aggregated like a day, so the
    derived categories are formed per slot (never a mixed dict of aggregates and slot lists)."""
    if not idx:
        return None
    sub = DayData(day.local_date, day.profile, {c: [v[i] for i in idx] for c, v in day.categories.items()},
                  {k: [v[i] for i in idx] for k, v in day.kpi_parts.items()}, day.high_water_end, day.computed_at,
                  day.final)
    agg = aggregate(sub)
    slots = quarter_hours(*period_bounds("day", day.local_date))
    night = night_by_slot(day.local_date)
    return Section(day.local_date, slots[idx[0]].start, slots[idx[-1]].end, 900.0 * len(idx),
                   sum(night[i] for i in idx), agg.categories, agg.kpi_parts, provisional)


def slot_sections(day: DayData, provisional_from: datetime | None = None) -> list[Section]:
    """One section per slot (day comparison series), derived categories per slot."""
    slots = quarter_hours(*period_bounds("day", day.local_date))
    cats = {**day.categories, **derived_slots(day.categories, len(slots))}
    night = night_by_slot(day.local_date)
    return [Section(day.local_date, s.start, s.end, 900.0, night[i], {c: v[i] for c, v in cats.items()},
                    {k: v[i] for k, v in day.kpi_parts.items()},
                    provisional_from is not None and s.end > provisional_from - timedelta(minutes=15))
            for i, s in enumerate(slots)]


# --- values -----------------------------------------------------------------------------------------------------------

@dataclass
class Measure:
    wh: float | None
    covered_s: float
    expected_s: float
    night_s: float
    unresolved: float
    reconstructed: bool


def measure(sections: Iterable[Section], metric: str) -> Measure:
    wh, known, cov, exp, night, unres, recon = 0.0, False, 0.0, 0.0, 0.0, 0.0, False
    for s in sections:
        exp += s.expected_s
        night += s.night_s
        e = s.cats.get(metric)
        if e is None or e.wh is None:
            continue
        known = True
        wh += e.wh
        cov += e.covered_s
        unres += e.unresolved_wh
        recon = recon or e.confidence == "reconstructed"
    return Measure(wh if known else None, cov, exp, night, unres, recon)


def energy_value(metric: str, m: Measure, divisor: int = 1) -> CompareValue:
    """Null normalisation: no covered time is unknown (never 0). Solar measures count daylight only: the night rule
    covers the night without data, so a day without any PV data would otherwise read 0 kWh with 30–70 % coverage; a
    section without expected daylight is a proven 0 Wh."""
    if m.expected_s <= 0:
        return CompareValue(value=None, reason="no_window")
    if m.wh is None:
        return CompareValue(value=None, coverage=0.0, reason="no_source")
    if metric in SOLAR:
        day_expected = m.expected_s - m.night_s
        if day_expected < NIGHT_TOLERANCE_S:
            cov = 1.0 if m.covered_s >= m.expected_s - NIGHT_TOLERANCE_S else min(1.0, m.covered_s / m.expected_s)
        else:
            day_covered = max(0.0, m.covered_s - m.night_s)
            # without measured energy a few covered seconds are only the night grid (epoch boundary days)
            if day_covered < COVERED_EPS_S or (day_covered < NIGHT_TOLERANCE_S and abs(m.wh) < 0.5):
                return CompareValue(value=None, coverage=0.0, reason="no_daylight_data")
            cov = min(1.0, day_covered / day_expected)
    else:
        if m.covered_s < COVERED_EPS_S:
            return CompareValue(value=None, coverage=0.0, reason="no_data")
        cov = min(1.0, m.covered_s / m.expected_s)
    return CompareValue(value=m.wh / divisor, coverage=cov, unresolved_wh=m.unresolved / divisor,
                        complete=cov >= FULL, reconstructed=m.reconstructed)


def ratio_value(sections: Sequence[Section], metric: str, generation: CompareValue | None = None,
                reconstructed: bool = False) -> CompareValue:
    """Autarky / self-consumption from the KPI parts of the sections (common valid intervals of their inputs), never an
    average of percentages; coverage = joint seconds / expected seconds. Self-consumption is at most as covered as
    the generation's daylight (the PV night rule covers the night, where numerator and denominator are 0/0)."""
    num = den = joint = exp = 0.0
    for s in sections:
        exp += s.expected_s
        r = s.kpi.get(metric)
        if r:
            num, den, joint = num + r[0], den + r[1], joint + r[2]
    if exp <= 0:
        return CompareValue(value=None, reason="no_window")
    cov = min(1.0, joint / exp)
    if joint < COVERED_EPS_S:
        return CompareValue(value=None, coverage=0.0, reason="no_data")
    if metric == "self_consumption" and generation is not None:
        if generation.value is None:
            return CompareValue(value=None, coverage=cov, reason=generation.reason)
        cov = min(cov, generation.coverage if generation.coverage is not None else cov)
    if den <= 0:
        return CompareValue(value=None, coverage=cov, reason="zero_denominator")
    return CompareValue(value=100.0 * (1.0 - num / den), coverage=cov, complete=cov >= FULL,
                        reconstructed=reconstructed)


def values(sections: Sequence[Section], divisor: int = 1, metrics: Sequence[str] = ENERGY_METRICS) -> dict[str, CompareValue]:
    out = {m: energy_value(m, measure(sections, m), divisor) for m in metrics}
    gen = out.get("generation") or energy_value("generation", measure(sections, "generation"))
    for r, inputs in RATIO_INPUTS.items():
        recon = any((out.get(i) or energy_value(i, measure(sections, i))).reconstructed for i in inputs)
        out[r] = ratio_value(sections, r, gen, recon)
    return out


def _bounds(metric: str, v: CompareValue) -> tuple[float, float]:
    u = v.unresolved_wh or 0.0
    assert v.value is not None
    return (v.value - u, v.value + u) if metric in TWO_SIDED else (v.value, v.value + u)


def delta(metric: str, a: CompareValue, b: CompareValue, consistent: bool = True) -> CompareDelta:
    """Tier and change A − B (comparison plan §3.4/§3.5)."""
    if a.value is None or b.value is None:
        return CompareDelta(abs=None, tier=CompareTier.NOT_RELIABLE, reason="missing")
    d = a.value - b.value
    if not consistent:
        return CompareDelta(abs=None, tier=CompareTier.NOT_RELIABLE, reason="refreshing")
    cov = min(a.coverage or 0.0, b.coverage or 0.0)
    if metric in RATIO_METRICS:
        tier = (CompareTier.RELIABLE if cov >= FULL else CompareTier.LIMITED if cov >= MIN_COVERAGE
                else CompareTier.NOT_RELIABLE)
        if tier is CompareTier.NOT_RELIABLE:
            return CompareDelta(abs=None, tier=tier, reason="gaps")
        if tier is CompareTier.RELIABLE and abs(d) < RATIO_TOLERANCE_PP:
            tier = CompareTier.UNCHANGED
        return CompareDelta(abs=d, tier=tier)
    lo_a, hi_a = _bounds(metric, a)
    lo_b, hi_b = _bounds(metric, b)
    lo, hi = lo_a - hi_b, hi_a - lo_b
    stable = lo > 0 or hi < 0
    tol = max(TOLERANCE_WH, TOLERANCE_SHARE * max(abs(a.value), abs(b.value)))
    if cov >= FULL and (hi_a - lo_a) + (hi_b - lo_b) <= 2 * tol:
        tier = CompareTier.RELIABLE if stable else CompareTier.UNCHANGED if abs(d) <= tol else CompareTier.NOT_RELIABLE
    elif cov >= MIN_COVERAGE and stable:
        tier = CompareTier.LIMITED
    else:
        tier = CompareTier.NOT_RELIABLE
    if tier is CompareTier.NOT_RELIABLE:
        return CompareDelta(abs=None, tier=tier, reason="gaps")
    if tier is CompareTier.UNCHANGED:
        return CompareDelta(abs=d, tier=tier)
    if metric in TWO_SIDED and (a.value < 0 or b.value < 0):
        return CompareDelta(abs=d, tier=tier, reason="negative_value")
    if b.value <= 0:
        return CompareDelta(abs=d, tier=tier, reason="reference_zero")
    return CompareDelta(abs=d, rel_pct=100.0 * d / b.value, tier=tier)


# --- composition ------------------------------------------------------------------------------------------------------

def day_length_slots(d: date) -> int:
    return int((local_midnight_utc(d + timedelta(days=1)) - local_midnight_utc(d)).total_seconds() // 900)


@dataclass
class Plan:
    """What the composition needs: aggregates of these days and slot data of those (comparison_api loads them)."""
    aggregate_days: list[date]
    slot_days: set[date]


@dataclass
class _Cut:
    t: datetime | None
    t_day: date | None
    running_a: bool
    running_b: bool
    index: int | None                # cut pair index (−1: nothing elapsed yet); None: no running side


def _cut(kind: str, a: Range, b: Range, ps: list[Pair], t: datetime | None) -> _Cut:
    if t is None:
        return _Cut(None, None, False, False, None)
    t_day = t.astimezone(TZ).date()
    run_a, run_b = a.end_utc > t, b.end_utc > t
    elapsed = []
    for running, r, side in ((run_a, a, "a"), (run_b, b, "b")):
        if not running:
            continue
        if t < r.start_utc:
            elapsed.append(-1)
        else:
            elapsed.append(next(p.index for p in ps if getattr(p, side) == t_day))
    return _Cut(t, t_day, run_a, run_b, min(elapsed) if elapsed else None)


def plan(kind: str, a: Range, b: Range, t: datetime | None) -> Plan:
    ps = pairs(kind, a, b)
    c = _cut(kind, a, b, ps, t)
    agg: set[date] = set()
    for r, running in ((a, c.running_a), (b, c.running_b)):
        agg |= {d for d in r.days() if not running or (c.t is not None and period_bounds("day", d)[0] <= c.t)}
    slots: set[date] = set()
    if c.t_day is not None and (a.contains(c.t_day) or b.contains(c.t_day)):
        slots.add(c.t_day)
    if kind == "day":                                # the series is per quarter hour
        slots |= {d for d in (a.first, b.first) if d in agg}
    for p in ps:
        if c.index is not None and p.index > c.index:
            break
        if p.a is None or p.b is None:
            continue
        if p.index == c.index or day_length_slots(p.a) != day_length_slots(p.b):
            slots |= {p.a, p.b}
    agg |= slots
    return Plan(sorted(agg), slots)


@dataclass
class _Exclusions:
    items: list[tuple[str, datetime, datetime, str]] = field(default_factory=list)

    def add(self, side: str, start: datetime, end: datetime, reason: str) -> None:
        if end > start:
            self.items.append((side, start, end, reason))

    def merged(self) -> list[CompareExclusion]:
        out: list[list] = []
        for side, s, e, reason in sorted(self.items, key=lambda x: (x[0], x[1])):
            if out and out[-1][0] == side and out[-1][3] == reason and out[-1][2] >= s:
                out[-1][2] = max(out[-1][2], e)
            else:
                out.append([side, s, e, reason])
        return [CompareExclusion(side=x[0], start=x[1], end=x[2], reason=x[3]) for x in out]


def _grid_source(sections: Iterable[Section]) -> str:
    kinds = {"iometer" if s.start >= IOMETER_FROM else "e3dc" for s in sections}
    return kinds.pop() if len(kinds) == 1 else ("mixed" if kinds else "none")


def _round(v: CompareValue, ratio: bool) -> CompareValue:
    nd = 2 if ratio else 1
    return v.model_copy(update={
        "value": None if v.value is None else round(v.value, nd),
        "coverage": None if v.coverage is None else round(v.coverage, 4),
        "unresolved_wh": None if v.unresolved_wh is None else round(v.unresolved_wh, 1)})


def _round_delta(d: CompareDelta, ratio: bool) -> CompareDelta:
    return d.model_copy(update={"abs": None if d.abs is None else round(d.abs, 2 if ratio else 1),
                                "rel_pct": None if d.rel_pct is None else round(d.rel_pct, 2)})


def compose(kind: str, a: Range, b: Range, basis: CompareBasis | None, profile: BalanceProfile, now: datetime,
            t: datetime | None, aggs: dict[date, DayAggregate], slot_days: dict[date, DayData],
            refreshing: bool = False, consistent: bool = True) -> EnergyComparison:
    """The comparison from loaded days (``plan`` tells which). Pure: the fixture generator calls it with synthetic
    days and a fixed ``now``."""
    ps = pairs(kind, a, b)
    c = _cut(kind, a, b, ps, t)
    blocks = kind == "range" and max(a.length, b.length) > DAILY_POINTS_MAX
    excl = _Exclusions()

    def running(side: str) -> bool:
        return c.running_a if side == "a" else c.running_b

    def complete_day(side: str, d: date) -> bool:
        return not running(side) or (c.t is not None and period_bounds("day", d)[1] <= c.t)

    def full(d: date) -> Section | None:
        agg = aggs.get(d)
        return day_section(agg) if agg is not None and agg.categories else None

    def until_cut(d: date) -> list[int]:
        assert c.t is not None
        return [i for i, s in enumerate(quarter_hours(*period_bounds("day", d))) if s.end <= c.t]

    # --- totals: actual sums per side (running side up to the cut) and per-day values from the complete days
    totals_secs: dict[str, dict[date, list[Section]]] = {"a": {}, "b": {}}
    for side, r in (("a", a), ("b", b)):
        for d in r.days():
            if complete_day(side, d):
                if kind == "day" and d in slot_days:
                    totals_secs[side][d] = slot_sections(slot_days[d])
                elif (sec := full(d)) is not None:
                    totals_secs[side][d] = [sec]
            elif c.t_day == d and d in slot_days:            # the running day up to the cut
                idx = until_cut(d)
                if kind == "day":
                    totals_secs[side][d] = slot_sections(slot_days[d], c.t)[:len(idx)]
                elif (sec := slice_section(slot_days[d], idx, provisional=True)) is not None:
                    totals_secs[side][d] = [sec]

    # --- aligned: paired, elapsed sections; the end of the last paired section is what "same point" means (from the
    # pair structure, not from data: a missing last day must not move the stated boundary)
    aligned: dict[int, tuple[list[Section], list[Section]]] = {}
    aligned_end: dict[str, datetime | None] = {"a": None, "b": None}
    for p in ps:
        if c.index is not None and p.index > c.index:
            for side, d in (("a", p.a), ("b", p.b)):
                if d is not None:
                    excl.add(side, *period_bounds("day", d), "after_cut")
            continue
        if p.a is None or p.b is None:
            side, d = ("b", p.b) if p.a is None else ("a", p.a)
            assert d is not None
            excl.add(side, *period_bounds("day", d), "beyond_common_length" if kind == "range" else "no_partner")
            continue
        partial = p.index == c.index
        mismatch = day_length_slots(p.a) != day_length_slots(p.b)
        if kind == "day" or partial or mismatch:
            if p.a not in slot_days or p.b not in slot_days:
                continue                                     # not loaded (has not begun) – nothing to pair
            keys_a, keys_b = slot_keys(p.a), slot_keys(p.b)
            inc = set(keys_a) & set(keys_b)
            cut_keys: set[str] | None = None
            if partial:
                cut_day = p.a if p.a == c.t_day else p.b
                cut_keys = {slot_keys(cut_day)[i] for i in until_cut(cut_day)}
                inc &= cut_keys
            secs: dict[str, list[Section]] = {}
            for side, d, keys, other in (("a", p.a, keys_a, keys_b), ("b", p.b, keys_b, keys_a)):
                day = slot_days[d]
                idx = [i for i, k in enumerate(keys) if k in inc]
                slots = quarter_hours(*period_bounds("day", d))
                if idx:
                    aligned_end[side] = slots[idx[-1]].end
                for i, k in enumerate(keys):
                    if k in inc:
                        continue
                    if k not in other:
                        reason = "dst_repeated_hour" if "#" in k else "dst_missing_hour"
                    else:
                        reason = "after_cut"
                    excl.add(side, slots[i].start, slots[i].end, reason)
                prov = partial and d == c.t_day
                if kind == "day":
                    all_secs = slot_sections(day, c.t if prov else None)
                    secs[side] = [all_secs[i] for i in idx]
                else:
                    sec = slice_section(day, idx, provisional=prov)
                    secs[side] = [sec] if sec else []
            aligned[p.index] = (secs["a"], secs["b"])
        else:
            aligned_end = {"a": period_bounds("day", p.a)[1], "b": period_bounds("day", p.b)[1]}
            sa, sb = full(p.a), full(p.b)
            if sa is not None and sb is not None:
                aligned[p.index] = ([sa], [sb])

    aligned_a = [s for pa, _ in aligned.values() for s in pa]
    aligned_b = [s for _, pb in aligned.values() for s in pb]
    no_window = c.index is not None and not aligned_a and not aligned_b

    # --- bases: which make sense (the app only shows what the hub offers)
    exception = any(p.a is None or p.b is None or day_length_slots(p.a) != day_length_slots(p.b) for p in ps)
    is_running = c.running_a or c.running_b
    complete_a = [d for d in a.days() if complete_day("a", d) and d in totals_secs["a"]]
    complete_b = [d for d in b.days() if complete_day("b", d) and d in totals_secs["b"]]
    per_day_ok = kind != "day" and bool(complete_a) and bool(complete_b)
    if is_running:
        default, available = CompareBasis.ALIGNED, [CompareBasis.ALIGNED]
    elif kind == "range" and a.length != b.length:
        default, available = CompareBasis.PER_DAY, [CompareBasis.PER_DAY, CompareBasis.ALIGNED]
    elif exception:
        default, available = CompareBasis.TOTALS, [CompareBasis.TOTALS, CompareBasis.ALIGNED]
    else:
        default, available = CompareBasis.TOTALS, [CompareBasis.TOTALS]
    chosen = basis if basis in available else default
    reasons = {CompareBasis.ALIGNED: "same_as_totals",
               CompareBasis.TOTALS: "running" if is_running else "lengths_differ",
               CompareBasis.PER_DAY: ("single_day" if kind == "day" else "no_complete_day" if not per_day_ok
                                      else "supplementary")}
    # display order: the switchable bases first (default first), then the others with their reason
    options = [CompareBasisOption(basis=x, available=True) for x in available] + \
        [CompareBasisOption(basis=x, available=False, reason=reasons[x]) for x in CompareBasis if x not in available]

    totals_a = [s for d in a.days() for s in totals_secs["a"].get(d, [])]
    totals_b = [s for d in b.days() for s in totals_secs["b"].get(d, [])]
    per_day_a = [s for d in complete_a for s in totals_secs["a"][d]]
    per_day_b = [s for d in complete_b for s in totals_secs["b"][d]]
    if chosen is CompareBasis.ALIGNED:
        head_a, head_b, div_a, div_b = aligned_a, aligned_b, 1, 1
    elif chosen is CompareBasis.TOTALS:
        head_a, head_b, div_a, div_b = totals_a, totals_b, 1, 1
    else:
        head_a, head_b, div_a, div_b = per_day_a, per_day_b, len(complete_a), len(complete_b)

    grid_a, grid_b = _grid_source(head_a), _grid_source(head_b)
    grid_differs = "mixed" in (grid_a, grid_b) or (grid_a != grid_b and "none" not in (grid_a, grid_b))
    metrics = (*ENERGY_METRICS, *(SUPPLEMENTARY if grid_differs else ()))
    va, vb = values(head_a, div_a, metrics), values(head_b, div_b, metrics)
    ta, tb = values(totals_a, 1, metrics), values(totals_b, 1, metrics)
    pa = values(per_day_a, len(complete_a), metrics) if per_day_ok else None
    pb = values(per_day_b, len(complete_b), metrics) if per_day_ok else None

    # --- axis and series
    points: list[tuple[str, list[Section], list[Section], int, int]] = []
    if kind == "day":
        keys = sorted(set(slot_keys(a.first)) | set(slot_keys(b.first)), key=key_order)
        def by_key(secs: list[Section], d: date) -> dict[str, Section]:
            ks = slot_keys(d)
            starts = {s.start: i for i, s in enumerate(quarter_hours(*period_bounds("day", d)))}
            return {ks[starts[s.start]]: s for s in secs}
        src_a = by_key(head_a if chosen is CompareBasis.ALIGNED else totals_a, a.first)
        src_b = by_key(head_b if chosen is CompareBasis.ALIGNED else totals_b, b.first)
        points = [(k, [src_a[k]] if k in src_a else [], [src_b[k]] if k in src_b else [], 0, 0) for k in keys]
    else:
        groups: dict[int, tuple[list[Section], list[Section], set[date], set[date]]] = {}
        for p in ps:
            g = groups.setdefault(p.group, ([], [], set(), set()))
            if chosen is CompareBasis.ALIGNED:
                sa, sb = aligned.get(p.index, ([], []))
            else:
                sa = totals_secs["a"].get(p.a, []) if p.a else []
                sb = totals_secs["b"].get(p.b, []) if p.b else []
            g[0].extend(sa)
            g[1].extend(sb)
            g[2].update(s.day for s in sa)
            g[3].update(s.day for s in sb)
        points = [(group_key(kind, gi, blocks), g[0], g[1], len(g[2]), len(g[3])) for gi, g in sorted(groups.items())]

    axis: list[CompareAxisPoint] = []
    point_values: list[tuple[dict[str, CompareValue], dict[str, CompareValue]]] = []
    for key, sa, sb, da, db in points:
        axis.append(CompareAxisPoint(
            key=key, a_start=min((s.start for s in sa), default=None), a_end=max((s.end for s in sa), default=None),
            b_start=min((s.start for s in sb), default=None), b_end=max((s.end for s in sb), default=None),
            a_days=da, b_days=db, provisional_a=any(s.provisional for s in sa),
            provisional_b=any(s.provisional for s in sb)))
        per_day = chosen is CompareBasis.PER_DAY
        point_values.append((values(sa, max(1, da) if per_day else 1, metrics),
                             values(sb, max(1, db) if per_day else 1, metrics)))

    # --- comparability: plant/data-source events inside the compared span, reconstructed data
    used = head_a + head_b
    events = events_between(min(s.start for s in used), max(s.end for s in used), profile) if used else []
    out_metrics: list[CompareMetric] = []
    for m in (*metrics, *RATIO_METRICS):
        ratio = m in RATIO_METRICS
        reasons = [e.event_id for e in events if m in e.metrics]
        if va[m].reconstructed or vb[m].reconstructed:
            reasons.append("reconstructed")
        dl = delta(m, va[m], vb[m], consistent)
        series_d = [delta(m, x[m], y[m], consistent) for x, y in point_values]
        out_metrics.append(CompareMetric(
            metric_id=m, kind="ratio" if ratio else "energy", unit=Unit.PERCENT if ratio else Unit.WH,
            supplementary=m in SUPPLEMENTARY, a=_round(va[m], ratio), b=_round(vb[m], ratio),
            delta=_round_delta(dl, ratio), comparability="limited" if reasons else "comparable",
            comparability_reasons=reasons, totals_a=_round(ta[m], ratio), totals_b=_round(tb[m], ratio),
            per_day_a=_round(pa[m], ratio) if pa else None, per_day_b=_round(pb[m], ratio) if pb else None,
            series=CompareSeries(
                a=[_round(x[m], ratio).value for x, _ in point_values],
                b=[_round(y[m], ratio).value for _, y in point_values],
                coverage_a=[_round(x[m], ratio).coverage for x, _ in point_values],
                coverage_b=[_round(y[m], ratio).coverage for _, y in point_values],
                tier=[d.tier for d in series_d],
                delta=[_round_delta(d, ratio).abs for d in series_d])))

    # --- notes
    notes: list[CompareNote] = []
    for e in events:
        notes.append(CompareNote(code=f"event:{e.event_id}", at=e.at,
                                 metrics=[m for m in (*metrics, *RATIO_METRICS) if m in e.metrics],
                                 detail=None if e.verified else "unverified"))
    if grid_differs:
        notes.append(CompareNote(code="grid_source", detail=f"{grid_a}/{grid_b}",
                                 metrics=["grid_import", "grid_export", "autarky", "self_consumption"]))
    if any(v.reconstructed for v in (*va.values(), *vb.values())):
        notes.append(CompareNote(code="reconstructed"))
    merged = excl.merged() if chosen is CompareBasis.ALIGNED else []
    for x in merged:                                   # one note per excluded interval (all days of it, not the first)
        if x.reason != "after_cut":
            notes.append(CompareNote(code=f"excluded:{x.reason}", side=x.side, at=x.start, until=x.end,
                                     detail=str(round((x.end - x.start).total_seconds() / 3600))))
    if a.first < b.end and b.first < a.end:
        notes.append(CompareNote(code="overlap"))
    for side, r in (("a", a), ("b", b)):
        if r.first < DATA_SINCE:
            notes.append(CompareNote(code="before_data_start", side=side, detail=DATA_SINCE.isoformat()))
    if no_window:
        notes.append(CompareNote(code="no_window"))
    if not consistent:
        notes.append(CompareNote(code="refreshing"))

    def side_model(side: str, r: Range, secs: list[Section], complete: list[date]) -> CompareSide:
        return CompareSide(first_day=r.first, end_day=r.end, start=r.start_utc, end=r.end_utc, days=r.length,
                           complete_days=len(complete), running=running(side), grid_source=_grid_source(secs))

    return EnergyComparison(
        kind=kind, basis=chosen, default_basis=default, available_bases=options, balance_profile=profile,
        calculation_version=CALCULATION_VERSION, generated_at=now, cut=c.t if is_running else None,
        aligned_end_a=aligned_end["a"] if is_running else None, aligned_end_b=aligned_end["b"] if is_running else None,
        no_window=no_window, refreshing=refreshing or not consistent,
        a=side_model("a", a, head_a, complete_a), b=side_model("b", b, head_b, complete_b),
        axis=axis, metrics=out_metrics, excluded=merged, notes=notes)
