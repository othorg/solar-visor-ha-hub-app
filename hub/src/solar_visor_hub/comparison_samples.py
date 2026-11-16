"""Synthetic days for comparison tests and the shared contract fixtures (deterministic, no device data).

Constant power per category; PV-like categories are zero and covered at night (as the engine's night rule), daylight
energy scales with the daylight seconds of the slot. Slots after ``until`` have not been computed yet ('future').
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, timedelta

from .comparison import Range, compose, cut_time, data_day, night_by_slot, plan
from .daycalc import SlotEnergy
from .history import DayData, aggregate
from .models import BalanceProfile, CompareBasis, EnergyComparison
from .periods import period_bounds, quarter_hours

CATEGORIES = ("pv_internal", "external", "consumption", "wallbox", "battery_charge", "battery_discharge",
              "grid_import", "grid_export", "grid_import_e3dc", "grid_export_e3dc")
SOLAR_CATEGORIES = ("pv_internal", "external")


def synthetic_day(d: date, watts: Mapping[str, float] | None = None, coverage: Mapping[str, float] | None = None,
                  until: datetime | None = None, unresolved_wh: float = 0.0,
                  profile: BalanceProfile = BalanceProfile.E3DC_BALANCE) -> DayData:
    """``watts``: constant power per category (default consumption 1 kW, rest 0); ``coverage``: covered share of the
    (daylight) time per category; ``until``: computation time of a running day."""
    w = {c: 0.0 for c in CATEGORIES} | {"consumption": 1000.0} | dict(watts or {})
    cov = dict(coverage or {})
    start, end = period_bounds("day", d)
    slots = quarter_hours(start, end)
    night = night_by_slot(d)
    cats: dict[str, list[SlotEnergy]] = {}
    for c in CATEGORIES:
        rows = []
        share = cov.get(c, 1.0)
        for i, s in enumerate(slots):
            if until is not None and s.start >= until:
                rows.append(SlotEnergy(None, 0.0, reasons={"future"}))
                continue
            if c in SOLAR_CATEGORIES:
                day_s = 900.0 - night[i]
                covered = night[i] + day_s * share
                wh = w[c] * day_s * share / 3600.0
            else:
                covered = 900.0 * share
                wh = w[c] * 900.0 * share / 3600.0
            reasons = {"gap"} if covered < 900.0 else set()
            rows.append(SlotEnergy(wh, covered / 900.0, unresolved_wh if covered < 900.0 else 0.0,
                                   reasons=reasons, applicable_s=900.0))
        cats[c] = rows

    def parts(num: str, den: tuple[str, ...]) -> list[tuple[float, float, float, float]]:
        out = []
        for i in range(len(slots)):
            inputs = [cats[num][i], *(cats[x][i] for x in den)]
            if any(e.wh is None for e in inputs):
                out.append((0.0, 0.0, 0.0, 0.0))
                continue
            joint = max(0.0, sum(e.covered_s for e in inputs) - (len(inputs) - 1) * 900.0)
            f = joint / 900.0
            out.append((cats[num][i].wh * f, sum(cats[x][i].wh or 0.0 for x in den) * f, joint, 900.0))
        return out

    kpi = {"autarky": parts("grid_import", ("consumption",)),
           "self_consumption": parts("grid_export", ("pv_internal", "external"))}
    computed = until if until is not None else end + timedelta(hours=3)
    return DayData(d, profile, cats, kpi, {}, computed, until is None)


def run(kind: str, a: Range, b: Range, now: datetime, day: Callable[[date], DayData],
        basis: CompareBasis | None = None, consistent: bool = True,
        profile: BalanceProfile = BalanceProfile.E3DC_BALANCE) -> EnergyComparison:
    """compose() with the loads of comparison_api, from synthetic days (``day(d)`` builds day d)."""
    running_day = data_day(now)
    computed_at = None
    if a.contains(running_day) or b.contains(running_day):
        computed_at = day(running_day).computed_at
    t = cut_time(now, computed_at)
    need = plan(kind, a, b, t)
    slots = {d: day(d) for d in need.slot_days}
    aggs = {d: aggregate(slots[d]) if d in slots else aggregate(day(d)) for d in need.aggregate_days}
    return compose(kind, a, b, basis, profile, now, t, aggs, slots, False, consistent)


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)
