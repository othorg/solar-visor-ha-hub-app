"""Power history (P5, Codex review findings 1/2/5): the *validated intervals of the energy engine* (hold times,
journal vetoes, wallbox rules, PV night rule, battery idle inference, epoch/mapping history, grid by epoch),
reduced to fixed 15-s columns per local day. Requests aggregate whole columns, so the power chart integrates to
exactly the energy view and never shows a value the energy engine would not count.

A column holds signed energy (W·s), covered / estimated / applicable seconds and the extremes of the values inside
it. A point is a statistic over its column – mean over the covered time, min/max – never a single measurement.
Derived series (generation, home, battery, grid) are combined on segment level over the intersection of their
inputs, never by adding independently averaged columns.
"""

from __future__ import annotations

import math
from array import array
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from .daycalc import HistoryTimeline, _intersect
from .energy import combine, merge_intervals
from .models import PowerPoint, PowerSeries, TimeInterval, Unit

COLUMN_S = 15
MAX_POINTS = 2000
MAX_RANGE = timedelta(days=8)      # elapsed time; ≤ 9 local dates (UI: rolling 24 h, a calendar day, an ISO week of
                                   # 167–169 h – power week, user decision 2026-10-08)
Interval = tuple[datetime, datetime]


@dataclass(frozen=True)
class SeriesDef:
    inputs: tuple[str, ...]           # timeline categories
    sign: int = 1                     # second input: + (sum) or − (difference)
    unit: Unit = Unit.W
    derived: bool = False             # shown as "berechnet"


# battery and grid: + = source for the house (discharge, import), − = charge, export (UI spec §3).
# Grid power = the E3/DC power measurement (like the live dashboard, UI spec §2.5a): the billing meter only provides
# counters stamped with HA reception time (report age up to 78 s) – fine for 15-min energy, but counter deltas over
# reception intervals produce spikes of several kW at chart resolution. The energy view keeps
# the billing meter; the difference is reported as e3dc_balance_residual_wh.
SERIES: dict[str, SeriesDef] = {
    "pv_internal": SeriesDef(("pv_internal",)),
    "external": SeriesDef(("external",)),
    "generation": SeriesDef(("pv_internal", "external"), 1, derived=True),
    "consumption": SeriesDef(("consumption",)),
    "wallbox": SeriesDef(("wallbox",)),
    "home": SeriesDef(("consumption", "wallbox"), -1, derived=True),
    "battery": SeriesDef(("battery_discharge", "battery_charge"), -1),
    "grid": SeriesDef(("grid_import_e3dc", "grid_export_e3dc"), -1),
    "mppt1": SeriesDef(("mppt1",)),
    "mppt2": SeriesDef(("mppt2",)),
    "mppt3": SeriesDef(("mppt3",)),
    "battery_soc": SeriesDef(("battery_soc",), unit=Unit.PERCENT),
    "kostal_ac": SeriesDef(("kostal_ac",)),
    # motorhome detail charts (own computation, EnergyService.extra_columns); battery + = discharge like the house
    "womo_shore": SeriesDef(("womo_shore",)),
    "womo_battery": SeriesDef(("womo_battery_discharge", "womo_battery_charge"), -1),
    "womo_soc": SeriesDef(("womo_soc",), unit=Unit.PERCENT),
    # PV detail charts: E3/DC strings (energy engine, like the KOSTAL MPPTs), KOSTAL measured DC sum (separate)
    "s10_string1": SeriesDef(("s10_string1",)),
    "s10_string2": SeriesDef(("s10_string2",)),
    "kostal_dc_sum": SeriesDef(("kostal_dc_sum",)),
}
# series of the separate computation (EnergyService.extra_columns)
EXTRA_SERIES_IDS = frozenset(("womo_shore", "womo_battery", "womo_soc", "kostal_dc_sum"))


@dataclass
class Columns:
    energy: array = field(default_factory=lambda: array("d"))      # signed W·s
    covered: array = field(default_factory=lambda: array("d"))     # s
    estimated: array = field(default_factory=lambda: array("d"))   # s (subset of covered)
    applicable: array = field(default_factory=lambda: array("d"))  # s
    vmin: array = field(default_factory=lambda: array("d"))        # nan = no value
    vmax: array = field(default_factory=lambda: array("d"))


@dataclass
class DayColumns:
    start: datetime                   # local midnight in UTC (multiple of 15 s)
    n: int
    series: dict[str, Columns]
    outages: dict[str, list[Interval]]
    computed_at: datetime
    final: bool


def _ts(t: datetime) -> float:
    return t.timestamp()


def _accumulate(n: int, t0: float, spans: Sequence[tuple[float, float]], into: array) -> None:
    for a, b in spans:
        i = max(0, int((a - t0) // COLUMN_S))
        while i < n:
            c0 = t0 + i * COLUMN_S
            if c0 >= b:
                break
            dt = min(b, c0 + COLUMN_S) - max(a, c0)
            if dt > 0:
                into[i] += dt
            i += 1


def columnize(tl: HistoryTimeline, start: datetime, n: int) -> Columns:
    t0 = _ts(start)
    nan = math.nan
    c = Columns(array("d", [0.0]) * n, array("d", [0.0]) * n, array("d", [0.0]) * n, array("d", [0.0]) * n,
                array("d", [nan]) * n, array("d", [nan]) * n)
    for sg in tl.segs:
        a, b = _ts(sg.start), _ts(sg.end)
        i = max(0, int((a - t0) // COLUMN_S))
        while i < n:
            c0 = t0 + i * COLUMN_S
            if c0 >= b:
                break
            dt = min(b, c0 + COLUMN_S) - max(a, c0)
            if dt > 0:
                c.energy[i] += sg.value * dt
                c.covered[i] += dt
                if not sg.value >= c.vmin[i]:          # nan-safe
                    c.vmin[i] = sg.value
                if not sg.value <= c.vmax[i]:
                    c.vmax[i] = sg.value
            i += 1
    _accumulate(n, t0, [(_ts(a), _ts(b)) for a, b in tl.applicable], c.applicable)
    covered = merge_intervals([(s.start, s.end) for s in tl.segs])
    _accumulate(n, t0, [(_ts(a), _ts(b)) for a, b in _intersect(covered, tl.estimated)], c.estimated)
    return c


def derive(timelines: dict[str, HistoryTimeline], series_id: str) -> HistoryTimeline | None:
    d = SERIES[series_id]
    parts = [timelines.get(i) for i in d.inputs]
    if parts[0] is None:
        return None
    if len(parts) == 1:
        return parts[0]
    second = parts[1] or HistoryTimeline([], [], [])
    segs = combine(parts[0].segs, second.segs, d.sign) if parts[1] is not None else []
    return HistoryTimeline(segs, merge_intervals(parts[0].applicable + second.applicable),
                           merge_intervals(parts[0].estimated + second.estimated))


def day_columns(timelines: dict[str, HistoryTimeline], start: datetime, end: datetime,
                outages: dict[str, list[Interval]], computed_at: datetime, final: bool) -> DayColumns:
    n = round((end - start).total_seconds() / COLUMN_S)
    cols = {}
    for sid in SERIES:
        tl = derive(timelines, sid)
        if tl is not None:
            cols[sid] = columnize(tl, start, n)
    return DayColumns(start, n, cols, outages, computed_at, final)


def snap(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    """Request bounds on the 15-s column grid (start down, end up) – the bounds the response reports."""
    return (datetime.fromtimestamp(math.floor(_ts(start) / COLUMN_S) * COLUMN_S, UTC),
            datetime.fromtimestamp(math.ceil(_ts(end) / COLUMN_S) * COLUMN_S, UTC))


def resolution(start: datetime, end: datetime, max_points: int) -> int:
    s, e = snap(start, end)
    n = round((e - s).total_seconds() / COLUMN_S)
    return COLUMN_S * max(1, math.ceil(n / max(1, min(max_points, MAX_POINTS))))


def points(days_: Sequence[DayColumns], series_id: str, start: datetime, end: datetime, res_s: int) -> list[PowerPoint]:
    """Columns of the snapped [start, end) grouped by ``res_s``. Every point stands for [t, t + res_s): a last,
    shorter group reports ``applicable`` relative to the full width, so mean · coverage · applicable · res_s is
    always the energy of the point."""
    k = res_s // COLUMN_S
    g0, g1 = math.floor(_ts(start) / COLUMN_S), math.ceil(_ts(end) / COLUMN_S)
    lookup: list[tuple[int, int, DayColumns]] = [(round(_ts(d.start) / COLUMN_S), d.n, d) for d in days_]
    out = []
    g = g0
    while g < g1:
        e = min(g1, g + k)
        energy = cov = est = app = 0.0
        lo, hi = math.inf, -math.inf
        for ds, dn, d in lookup:
            cols = d.series.get(series_id)
            a, b = max(g, ds), min(e, ds + dn)
            if cols is None or b <= a:
                continue
            for i in range(a - ds, b - ds):
                energy += cols.energy[i]
                cov += cols.covered[i]
                est += cols.estimated[i]
                app += cols.applicable[i]
                if cols.vmin[i] < lo:
                    lo = cols.vmin[i]
                if cols.vmax[i] > hi:
                    hi = cols.vmax[i]
        width = res_s
        out.append(PowerPoint(
            t=datetime.fromtimestamp(g * COLUMN_S, UTC),
            mean=round(energy / cov, 1) if cov > 0.5 else None,
            min=round(lo, 1) if cov > 0.5 and lo != math.inf else None,
            max=round(hi, 1) if cov > 0.5 and hi != -math.inf else None,
            coverage=round(min(1.0, cov / app), 4) if app > 0 else 0.0,
            estimated=round(min(1.0, est / cov), 4) if cov > 0 else 0.0,
            applicable=round(min(1.0, app / width), 4)))
        g = e
    return out


def series(days_: Sequence[DayColumns], ids: Sequence[str], start: datetime, end: datetime,
           max_points: int) -> list[PowerSeries]:
    start, end = snap(start, end)
    res_s = resolution(start, end, max_points)
    out = []
    for sid in ids:
        d = SERIES[sid]
        outages = merge_intervals([(max(a, start), min(b, end)) for day in days_ for a, b in day.outages.get(sid, [])
                                   if min(b, end) > max(a, start)])
        out.append(PowerSeries(series_id=sid, unit=d.unit, derived=d.derived, resolution_s=res_s,
                               points=points(days_, sid, start, end, res_s),
                               outages=[TimeInterval(start=a, end=b) for a, b in outages]))
    return out


Dependency = tuple[str, str, datetime, datetime]     # (category, measurement, active from, active to)


def series_outages(deps: Sequence[Dependency], invalid: dict[str, list[Interval]]) -> dict[str, list[Interval]]:
    """Journal outages per history series: the vetoes of the measurements feeding its inputs, each only while
    that measurement is the active source (epochs; grid: E3/DC before, meter after the IOmeter epoch)."""
    by_cat: dict[str, list[Interval]] = {}
    for cat, m, a, b in deps:
        by_cat.setdefault(cat, []).extend(
            (max(x, a), min(y, b)) for x, y in invalid.get(m, []) if min(y, b) > max(x, a))
    out = {}
    for sid, d in SERIES.items():
        iv = merge_intervals([x for c in d.inputs for x in by_cat.get(c, [])])
        if iv:
            out[sid] = iv
    return out



def window_stats(days_: Sequence[DayColumns], series_id: str, start: datetime, end: datetime) -> tuple[float | None, float, datetime | None]:
    """(mean W over the covered time, coverage, end of the last covered column) for one window."""
    g0, g1 = math.floor(_ts(start) / COLUMN_S), math.ceil(_ts(end) / COLUMN_S)
    energy = cov = app = 0.0
    last: int | None = None
    for d in days_:
        cols = d.series.get(series_id)
        ds = round(_ts(d.start) / COLUMN_S)
        a, b = max(g0, ds), min(g1, ds + d.n)
        if cols is None or b <= a:
            continue
        for i in range(a - ds, b - ds):
            energy += cols.energy[i]
            cov += cols.covered[i]
            app += cols.applicable[i]
            if cols.covered[i] > 0.5:
                last = ds + i
    mean = round(energy / cov, 1) if cov > 0.5 else None
    until = datetime.fromtimestamp((last + 1) * COLUMN_S, UTC) if last is not None else None
    return mean, (round(min(1.0, cov / app), 4) if app > 0 else 0.0), until
