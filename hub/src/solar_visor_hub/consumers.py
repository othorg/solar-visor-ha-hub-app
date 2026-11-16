"""Individual consumers of the HA energy dashboard for the home detail sheet (user request 2026-10-06).

- Catalog: HA ``energy/get_prefs`` → ``device_consumption`` (energy statistic + optional power sensor), as configured in
  the HA energy dashboard, including the wallbox (user decision: list exactly like HA).
- Now: the subscribed power sensors, normalised to W; unavailable/unknown/non-numeric is null, never 0.
- Today: HA long-term statistics (hourly "change" since local midnight) – the numbers of the HA energy dashboard, up to
  the last compiled hour (``stats_until``).
- Not tracked: total consumption − Σ consumers, like HA's untracked consumption; unknown if a part is unknown, null
  with a reason if the consumers exceed the total beyond the measurement tolerance.
Pure functions plus a small store; ``ha_client`` feeds the store, ``app`` builds the view.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

POWER_SCALE = {"W": 1.0, "kW": 1_000.0, "MW": 1_000_000.0}
# HA's entity id rule (core.VALID_ENTITY_ID): statistics of the energy dashboard may be external ("acme:x") – they are
# never subscribed or rendered (an invalid id fails the whole subscription / renders no template)
VALID_ENTITY_ID = re.compile(r"^(?!.+__)(?!_)[\da-z_]+(?<!_)\.(?!_)[\da-z_]+(?<!_)$")
NAME_SUFFIXES = (" Ladeleistung", " Leistung", " Power", " Energiebezug", " Energie", " Energy")
NAME_PREFIXES = ("Shelly ",)
ID_SUFFIXES = ("_energiebezug", "_energie", "_energy", "_leistung", "_power")
UNTRACKED_TOLERANCE_W = 50.0
UNTRACKED_TOLERANCE_WH = 100.0
UNTRACKED_TOLERANCE_SHARE = 0.03
# HA re-reports the consumers' power sensors every few seconds even when unchanged: no report
# for this long means the value is not current
STALE_AFTER = timedelta(minutes=15)


@dataclass(frozen=True)
class ConsumerSpec:
    consumer_id: str                  # the energy statistic (stat_consumption) – stable identity in HA
    power_entity: str | None          # stat_rate (optional in HA; only a valid entity id)
    pref_name: str | None             # name set in the HA energy dashboard (optional)
    parent: str | None = None         # included_in_stat: measured inside another consumer (not counted twice)


def parse_prefs(result: Any) -> list[ConsumerSpec]:
    """``energy/get_prefs`` result → consumers in HA order; malformed entries are skipped."""
    out: list[ConsumerSpec] = []
    for d in (result or {}).get("device_consumption") or []:
        if not isinstance(d, dict) or not isinstance(d.get("stat_consumption"), str):
            continue
        rate = d.get("stat_rate")
        name = d.get("name")
        parent = d.get("included_in_stat")
        out.append(ConsumerSpec(d["stat_consumption"],
                                rate if isinstance(rate, str) and VALID_ENTITY_ID.match(rate) else None,
                                name.strip() if isinstance(name, str) and name.strip() else None,
                                parent if isinstance(parent, str) and parent and parent != d["stat_consumption"] else None))
    return out


def parse_power(state: Any, unit: str | None) -> float | None:
    """W from a power sensor state; unknown stays unknown (None)."""
    try:
        v = float(state)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    scale = POWER_SCALE.get(unit or "W")
    return None if scale is None else v * scale


def clean_name(friendly: str) -> str:
    """„Shelly Küche Thermomix Leistung“ → „Küche Thermomix“, „USV Power“ → „USV“, „X Ladeleistung (LP1)“ → „X (LP1)“."""
    name = friendly.strip()
    for p in NAME_PREFIXES:
        if name.startswith(p):
            name = name[len(p):]
    for s in NAME_SUFFIXES:
        if name.endswith(s):
            name = name[: -len(s)]
            break
        if s + " (" in name:
            name = name.replace(s + " (", " (", 1)
            break
    return name.strip() or friendly.strip()


def name_from_id(entity_id: str) -> str:
    base = entity_id.split(".", 1)[-1]
    for s in ID_SUFFIXES:
        if base.endswith(s):
            base = base[: -len(s)]
    if base.startswith("shelly_"):
        base = base[len("shelly_"):]
    return " ".join(w.capitalize() for w in base.split("_") if w) or entity_id


def display_name(spec: ConsumerSpec, names: dict[str, str], wallbox_entity: str | None,
                 womo_entity: str | None) -> tuple[str, str]:
    """(name, kind): the HA dashboard name, the app's names for mapped entities, else the cleaned friendly name of the
    power (or energy) sensor, else one derived from the entity id."""
    kind = ("wallbox" if spec.power_entity and spec.power_entity == wallbox_entity
            else "womo" if spec.power_entity and spec.power_entity == womo_entity else "device")
    if spec.pref_name:
        return spec.pref_name, kind
    if kind == "wallbox":
        return "Wallbox", kind
    if kind == "womo":
        return "Wohnmobil (Landstrom)", kind
    for entity in (spec.power_entity, spec.consumer_id):
        if entity and names.get(entity):
            return clean_name(names[entity]), kind
    return name_from_id(spec.consumer_id), kind


def stat_time(value: Any) -> datetime | None:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000 if value > 1e11 else value, UTC)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
        except ValueError:
            return None
    return None


def sum_statistics(result: Any, ids: list[str]) -> tuple[dict[str, float | None], datetime | None]:
    """Hourly ``change`` rows (kWh) per statistic → Wh since the start; ``until`` = end of the last delivered hour. A
    statistic without rows is unknown (None) – HA omits statistics it has no data for."""
    rows_by_id = result if isinstance(result, dict) else {}
    out: dict[str, float | None] = {}
    until: datetime | None = None
    for sid in ids:
        rows = rows_by_id.get(sid) or []
        changes = [r.get("change") for r in rows if isinstance(r, dict)]
        known = [c for c in changes if isinstance(c, (int, float)) and math.isfinite(c)]
        out[sid] = sum(known) * 1000.0 if known else None
        for r in rows:
            end = stat_time(r.get("end")) if isinstance(r, dict) else None
            if end and (until is None or end > until):
                until = end
    return out, until


@dataclass(frozen=True)
class Rest:
    value: float | None
    reason: str | None                # total_unknown | sum_exceeds_total | within_tolerance | None
    unmeasured: int                   # consumers without a value: their consumption is part of the rest


def counted(specs: list[ConsumerSpec]) -> list[bool]:
    """Whether a consumer counts for the rest: one inside another listed consumer (included_in_stat) is already part
    of that one's value – like HA's untracked consumption."""
    ids = {s.consumer_id for s in specs}
    return [s.parent is None or s.parent not in ids for s in specs]


def untracked(total: float | None, parts: list[float | None], tolerance: float, extra_unmeasured: int = 0) -> Rest:
    """Total − Σ measured consumers (HA's untracked consumption). A consumer without a value is not counted as 0 for
    itself – what nothing measures is untracked, so it stays in the rest (counted in ``unmeasured``). A small negative
    rest is 0 within the measurement tolerance, a larger one is not determinable (the consumers exceed the total)."""
    unmeasured = sum(1 for p in parts if p is None) + extra_unmeasured
    if total is None:
        return Rest(None, "total_unknown", unmeasured)
    rest = total - sum(p for p in parts if p is not None)
    tol = max(tolerance, UNTRACKED_TOLERANCE_SHARE * abs(total))
    if rest < -tol:
        return Rest(None, "sum_exceeds_total", unmeasured)
    if rest < 0:
        return Rest(0.0, "within_tolerance", unmeasured)
    return Rest(rest, None, unmeasured)


@dataclass
class PowerReading:
    value_w: float | None
    changed_at: datetime | None
    reported_at: datetime | None = None

    def state(self, now: datetime) -> str:
        """ok | stale | unknown – unknown without a numeric value, stale without a report for STALE_AFTER."""
        if self.value_w is None:
            return "unknown"
        seen = self.reported_at or self.changed_at
        return "stale" if seen is None or now - seen > STALE_AFTER else "ok"


@dataclass
class ConsumerStore:
    """Catalog and live readings, fed by the HA client (event loop only)."""
    catalog: list[ConsumerSpec] = field(default_factory=list)
    loaded_at: datetime | None = None
    readings: dict[str, PowerReading] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)
    units: dict[str, str | None] = field(default_factory=dict)

    def set_catalog(self, specs: list[ConsumerSpec], now: datetime) -> bool:
        """True if the catalog changed (the power subscription must follow)."""
        changed = specs != self.catalog
        self.catalog, self.loaded_at = specs, now
        if changed:
            keep = set(self.entities())
            self.readings = {k: v for k, v in self.readings.items() if k in keep}
        return changed

    def entities(self) -> list[str]:
        return sorted({e for s in self.catalog for e in (s.power_entity, s.consumer_id) if e and VALID_ENTITY_ID.match(e)})

    def power_entities(self) -> set[str]:
        return {s.power_entity for s in self.catalog if s.power_entity}

    def reset_readings(self) -> None:
        self.readings = {}

    def apply(self, entity_id: str, state: Any, unit: str | None, changed_at: datetime | None,
              friendly_name: str | None = None) -> None:
        if friendly_name:
            self.names[entity_id] = friendly_name
        if entity_id not in self.power_entities():
            return
        if unit is not None:
            self.units[entity_id] = unit
        self.readings[entity_id] = PowerReading(parse_power(state, self.units.get(entity_id, unit)), changed_at,
                                                changed_at)

    def apply_reported(self, entity_id: str, reported: datetime | None) -> None:
        """``last_reported`` from the freshness template (also moves when the value stays the same)."""
        r = self.readings.get(entity_id)
        if r is not None and reported is not None and (r.reported_at is None or reported > r.reported_at):
            r.reported_at = reported

    def update_attributes(self, entity_id: str, unit: str | None, friendly_name: str | None) -> None:
        """Attribute-only change: the next state is read with the new unit (the current reading stays)."""
        if friendly_name:
            self.names[entity_id] = friendly_name
        if unit is not None and entity_id in self.power_entities():
            self.units[entity_id] = unit

    def disconnect(self) -> None:
        """Session lost: live values are no longer known (catalog and names stay for the next session)."""
        self.readings = {}
