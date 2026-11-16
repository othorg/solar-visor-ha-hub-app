"""Reads what the generic day calculation needs from Home Assistant (plan ha-addon P3 step 3).

``plant_daycalc.requirements`` says what is needed per data point; this module fetches it over any ``read`` coroutine
(the hub's live session or a command-line connection) and converts it to canonical values:
- statistics: the statistic id is the entity id (HA's default); rows come in W/Wh – a statistic without a unit class
  (HA cannot convert it) is scaled here or left out;
- raw states: normalised with the point's unit and sign like the live values; the unit attribute comes from the
  current state (the compact history answer carries none) – for a sensor the hub no longer follows (an older plant
  revision) from the statistic's metadata; without either the states are unknown, never taken as canonical.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .energy import Point as EPoint
from .ha_history import Read, StatRow, raw_states, statistics
from .normalize import _FACTORS, normalize
from .plant import Plant
from .plant_daycalc import COUNTER_LEAD, LEAD_IN, STAT_PERIOD, PointData, requirements


def _scale(meta: dict[str, Any] | None, canonical: str) -> float | None:
    """Factor from the statistic's stored unit to the canonical one; 1 if HA converts it (unit class known)."""
    if meta is None:
        return None
    if meta.get("unit_class") in ("power", "energy"):
        return 1.0                          # requested with units {power: W, energy: Wh}
    unit = (meta.get("statistics_unit_of_measurement") or "").strip()
    for (src, dst), factor in _FACTORS.items():
        if src == unit and dst.value == canonical:
            return factor
    return None


def _scaled(rows: list[StatRow], factor: float) -> list[StatRow]:
    if factor == 1.0:
        return rows

    def f(v: float | None) -> float | None:
        return None if v is None else v * factor

    return [StatRow(r.start, r.end, f(r.mean), f(r.min), f(r.max), f(r.sum), f(r.state), f(r.change)) for r in rows]


def _unit(entity: str, units: dict[str, str | None], meta: dict[str, Any] | None) -> tuple[bool, str | None]:
    """(known, unit attribute): the live state's attribute (None = the sensor has none), else the statistic's."""
    if entity in units:
        return True, units[entity]
    unit = (meta or {}).get("display_unit_of_measurement") or (meta or {}).get("statistics_unit_of_measurement")
    return (True, unit) if isinstance(unit, str) and unit else (False, None)


async def read_day(plant: Plant, start: datetime, end: datetime, tier: str, read: Read, meta: dict[str, dict[str, Any]],
                   units: dict[str, str | None]) -> dict[str, PointData]:
    """Data of [start, end) for every point the tier needs. ``meta``: statistic metadata by id
    (``ha_history.statistic_ids``); ``units``: current unit attribute by entity (live states)."""
    need = requirements(plant, tier)
    points = plant.points()
    period = STAT_PERIOD[tier]

    def ids(kind: str) -> list[str]:
        return sorted({points[m].entity for m, kinds in need.items() if kind in kinds and points[m].entity in meta})

    rows: dict[str, list[StatRow]] = {}
    if mean_ids := ids("mean"):
        rows |= await statistics(read, mean_ids, start, end, period, ("mean",))
    if sum_ids := ids("sum"):                   # counters: a day before as well – its highest reading is the base
        for sid, r in (await statistics(read, sum_ids, start - COUNTER_LEAD, end, period, ("sum",))).items():
            rows[sid] = rows.get(sid, []) + r
    out: dict[str, PointData] = {}
    for metric_id, kinds in need.items():
        point = points[metric_id]
        stats = None
        if kinds & {"mean", "sum"}:
            factor = _scale(meta.get(point.entity), point.unit.value)
            if factor is not None and point.entity in rows:
                stats = _scaled(rows[point.entity], factor)
        raw = None
        if "raw" in kinds:
            known, unit = _unit(point.entity, units, meta.get(point.entity))
            if known:
                states = await raw_states(read, point.entity, start - LEAD_IN, end)
                raw = [EPoint(s.at, v if isinstance(v := normalize(point, s.state, unit).value, float) else None)   # type: ignore[arg-type]
                       for s in states]
        out[metric_id] = PointData(stats=stats, raw=raw)
    return out
