"""Power history for /v2 (plan ha-addon P3 step 6).

- The imported plant: the existing calculation's columns (energy_api.power_history), renamed to the live metric ids;
  series whose sign differs from the live metric (v1 battery: + discharge) are flipped – one id, one meaning.
- Any other plant: Home Assistant's statistics, read on demand – 5-minute rows inside the recorder's keep window,
  hourly rows before it (both time-weighted means with min/max, P0). Derived series (generation, consumption, home,
  grid from two sensors) are formed row by row; a missing input makes the row unknown, never 0. Each day is drawn
  with the plant that was active at its start (as the energy) and a point counts from its ``valid_from`` on.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from .ha_history import StatRow, statistics
from .models import PowerPoint, PowerResponse, Unit
from .models_v2 import PlantPowerResponse, PlantPowerSeries
from .periods import days, period_bounds
from .plant import Kind, Plant
from .plant_archive import CALCULATION_VERSION as GENERIC_VERSION
from .plant_daycalc import TIER_RAW
from .plant_history_source import _scale, _scaled

MAX_RANGE = timedelta(days=8)

# v2 id → (v1 series, sign): v1 battery and motorhome battery count + as discharge (source for the house)
LEGACY_SERIES: dict[str, tuple[str, int]] = {
    "pv_1.power": ("pv_internal", 1), "pv_2.power": ("external", 1), "generation.power": ("generation", 1),
    "consumption.power": ("consumption", 1), "wallbox.power": ("wallbox", 1), "home.power": ("home", 1),
    "battery.power": ("battery", -1), "grid.power": ("grid", 1), "battery.soc": ("battery_soc", 1),
    "pv_2.string_1.power": ("mppt1", 1), "pv_2.string_2.power": ("mppt2", 1), "pv_2.string_3.power": ("mppt3", 1),
    "pv_2.ac_power": ("kostal_ac", 1), "pv_2.dc_power": ("kostal_dc_sum", 1),
    "pv_1.string_1.power": ("s10_string1", 1), "pv_1.string_2.power": ("s10_string2", 1),
    "generic_1.power": ("womo_shore", 1), "generic_1.storage_power": ("womo_battery", -1),
    "generic_1.storage_soc": ("womo_soc", 1),
}


def _flip(p: PowerPoint) -> PowerPoint:
    neg = lambda v: None if v is None else -v           # noqa: E731
    return p.model_copy(update={"mean": neg(p.mean), "min": neg(p.max), "max": neg(p.min)})


def from_legacy(resp: PowerResponse, ids: Sequence[str]) -> PlantPowerResponse:
    by_v1 = {s.series_id: s for s in resp.series}
    out = []
    for sid in ids:
        v1_id, sign = LEGACY_SERIES[sid]
        s = by_v1[v1_id]
        out.append(PlantPowerSeries(series_id=sid, unit=s.unit, derived=s.derived, resolution_s=s.resolution_s,
                                    source="existing_calculation",
                                    points=[_flip(p) for p in s.points] if sign < 0 else s.points, outages=s.outages))
    return PlantPowerResponse(start=resp.start, end=resp.end, timezone=resp.timezone,
                              calculation_version=resp.calculation_version, series=out,
                              monitoring_gaps=resp.monitoring_gaps, journal_since=resp.journal_since)


# --- any other plant -----------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class SeriesPlan:
    unit: Unit
    terms: tuple[tuple[str, int], ...]      # metric ids with their sign; one term = measured
    derived: bool


def series_plans(plant: Plant) -> dict[str, SeriesPlan]:
    """Every power series the plant can show: its power and percentage points plus what the hub calculates."""
    pts = plant.points()
    plans = {mid: SeriesPlan(p.unit, ((mid, 1),), False) for mid, p in pts.items() if p.unit in (Unit.W, Unit.PERCENT)}
    gens = tuple((f"{m.id}.power", 1) for m in plant.active() if m.kind is Kind.PV)
    plans["generation.power"] = SeriesPlan(Unit.W, gens, True)
    if plant.modules["grid"].variant == "split":
        plans["grid.power"] = SeriesPlan(Unit.W, (("grid.import_power", 1), ("grid.export_power", -1)), True)
    grid = plans["grid.power"].terms
    battery = (("battery.power", -1),) if plant.modules["battery"].enabled else ()
    if "consumption.power" not in pts:
        plans["consumption.power"] = SeriesPlan(Unit.W, gens + grid + battery, True)
    beside = tuple((f"{m.id}.power", -1) for m in plant.active()
                   if m.kind is Kind.WALLBOX or (m.kind is Kind.GENERIC and m.balance == "beside_home"))
    plans["home.power"] = SeriesPlan(Unit.W, plans["consumption.power"].terms + beside, True)
    return plans


def _merge(rows: Sequence[tuple[datetime, datetime, float | None, float | None, float | None]],
           max_points: int) -> tuple[list[PowerPoint], int]:
    """Downsamples to at most ``max_points`` columns of equal length: time-weighted mean, extremes, coverage."""
    if not rows:
        return [], 0
    step = rows[0][1] - rows[0][0]
    factor = max(1, -(-len(rows) // max_points))
    out = []
    for i in range(0, len(rows), factor):
        group = rows[i:i + factor]
        known = [r for r in group if r[2] is not None]
        mean = sum(r[2] for r in known) / len(known) if known else None     # type: ignore[misc]
        mins = [r[3] for r in known if r[3] is not None]
        maxs = [r[4] for r in known if r[4] is not None]
        out.append(PowerPoint(t=group[0][0], mean=None if mean is None else round(mean, 1),
                              min=round(min(mins), 1) if mins else None, max=round(max(maxs), 1) if maxs else None,
                              coverage=round(len(known) / factor, 4), applicable=1.0))
    return out, int(step.total_seconds()) * factor


def _signed(r: StatRow, sign: int) -> StatRow:
    """A row in the meaning of the metric: with sign −1 the extremes swap (min of −x is −max x)."""
    def neg(v: float | None) -> float | None:
        return None if v is None else -v
    return StatRow(r.start, r.end, r.mean, r.min, r.max) if sign > 0 else StatRow(r.start, r.end, neg(r.mean), neg(r.max),
                                                                                    neg(r.min))


def allowed_series(plant: Plant, revisions: Sequence[Plant] = ()) -> list[str]:
    """Series of the current plant and of the revisions active in the requested range (a module switched off later)."""
    return sorted({sid for pl in (plant, *revisions) for sid in series_plans(pl)})


async def plant_power(history, plant: Plant, ids: Sequence[str], start: datetime, end: datetime,   # type: ignore[no-untyped-def]
                      max_points: int, revisions: dict[datetime, Plant] | None = None) -> PlantPowerResponse:
    """``revisions``: the plant active at the start of each local day of the range (default: ``plant`` throughout)."""
    tz = history.tz
    meta = await history._statistic_meta()
    day_starts = sorted(revisions) if revisions else []

    def plant_for(t: datetime) -> Plant:
        prev = [s for s in day_starts if s <= t]
        return revisions[prev[-1]] if revisions and prev else plant

    plants = list({id(pl): pl for pl in [plant, *(revisions or {}).values()]}.values())
    plans_of = {id(pl): series_plans(pl) for pl in plants}
    needed = {(id(pl), mid) for pl in plants for sid in ids if sid in plans_of[id(pl)]
              for mid, _ in plans_of[id(pl)][sid].terms}
    # one resolution for the whole request: 5 minutes if it lies in the keep window, else hours
    first_day = min(days(start, end, tz) or [start.astimezone(tz).date()])
    raw = all(history.tier_for(d) == TIER_RAW for d in [first_day])
    period, step = ("5minute", timedelta(minutes=5)) if raw else ("hour", timedelta(hours=1))
    t0 = start - (start - datetime(2000, 1, 1, tzinfo=start.tzinfo)) % step
    by_id = {id(pl): pl for pl in plants}
    entities = sorted({by_id[pid].points()[m].entity for pid, m in needed if by_id[pid].points()[m].entity in meta})
    rows = await statistics(history._read, entities, t0, end, period, ("mean", "min", "max")) if entities else {}
    grid: list[datetime] = []
    t = t0
    while t < end:
        grid.append(t)
        t += step
    values: dict[tuple[int, str], dict[datetime, StatRow]] = {}
    for pid, mid in needed:
        p = by_id[pid].points()[mid]
        factor = _scale(meta.get(p.entity), p.unit.value)
        series = _scaled(rows.get(p.entity, []), factor) if factor is not None else []
        values[(pid, mid)] = {r.start: _signed(r, p.sign) for r in series
                              if p.valid_from is None or r.start >= p.valid_from}     # before it: no source
    out = []
    for sid in ids:
        plan = next(plans_of[id(pl)][sid] for pl in plants if sid in plans_of[id(pl)])
        merged = []
        for t in grid:
            pl = plant_for(t)
            day_plan = plans_of[id(pl)].get(sid)
            if day_plan is None:                           # the series did not exist that day
                merged.append((t, t + step, None, None, None))
                continue
            parts = [(values.get((id(pl), mid), {}).get(t), sign) for mid, sign in day_plan.terms]
            if not parts or any(r is None or r.mean is None for r, _ in parts):
                merged.append((t, t + step, None, None, None))
                continue
            mean = sum(r.mean * sign for r, sign in parts)                     # type: ignore[union-attr,operator]
            if len(parts) == 1:
                r, sign = parts[0]
                lo, hi = (r.min, r.max) if sign > 0 else (None if r.max is None else -r.max, None if r.min is None else -r.min)  # type: ignore[union-attr]
            else:
                lo = hi = None                                                  # extremes of a sum are not known
            merged.append((t, t + step, mean, lo, hi))
        points, res = _merge(merged, max_points)
        out.append(PlantPowerSeries(series_id=sid, unit=plan.unit, derived=plan.derived, resolution_s=res or int(step.total_seconds()),
                                    source="statistic_mean", points=points))
    return PlantPowerResponse(start=t0, end=end, timezone=tz.key, calculation_version=GENERIC_VERSION, series=out)
