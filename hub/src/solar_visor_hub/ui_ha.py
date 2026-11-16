"""What the web interface needs from Home Assistant to assign sensors (P4 detail plan §3): the point catalogue, a
server-side sensor search, a check of every assigned point against the real HA and suggestions from the energy
dashboard. Read-only, over the hub's session (small requests, ``heavy=False``).

- Search: states, the entity/device/area registries and the statistic metadata, cached briefly; at most 50 hits.
  Whether the recorder excludes an entity is not known to the read API – shown as unknown, never guessed from a missing
  statistic (Codex review of the P4 plan).
- Check: works on drafts, also incomplete ones – every assigned point is looked at on its own.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

from .models import Unit
from .normalize import compatible_unit, normalize
from .plant import (
    ALWAYS_ON,
    CATALOG,
    GENERIC_BALANCES,
    GRID_VARIANTS,
    MAX_NAME,
    MAX_STRINGS,
    MODULES,
    STRING_POINTS,
    WALLBOX_TRUST,
    Kind,
    Point,
    PointSpec,
    Role,
)

log = logging.getLogger("solar_visor_hub.ui")
Read = Callable[[dict[str, Any]], Awaitable[Any]]
STATES_MAX_AGE = 15.0
REGISTRY_MAX_AGE = 300.0
SEARCH_LIMIT = 50
EXPECTED_CLASS = {Unit.W: "power", Unit.WH: "energy", Unit.PERCENT: "battery", Unit.V: "voltage", Unit.A: "current"}
REQUIRED = {Kind.GRID: {"signed": ("power",), "split": ("import_power", "export_power")}}


# --- catalogue ------------------------------------------------------------------------------------------------------

def _spec_view(spec: PointSpec) -> dict[str, Any]:
    return {"unit": spec.unit.value, "roles": [r.value for r in spec.roles], "counter": spec.counter,
            "nonnegative": spec.nonnegative}


def catalog_view() -> dict[str, Any]:
    """Slots, points and rules of the plant model – the forms are built from it, not from rules of their own."""
    modules = []
    for mid, kind in MODULES.items():
        required: dict[str, list[str]]
        if kind is Kind.GRID:
            required = {v: list(p) for v, p in REQUIRED[Kind.GRID].items()}
        elif kind is Kind.CONSUMPTION:
            required = {"": []}
        else:
            required = {"": ["power"]}
        modules.append({"id": mid, "kind": kind.value, "always_on": mid in ALWAYS_ON, "required": required,
                        "points": {pid: _spec_view(s) for pid, s in CATALOG[kind].items()},
                        "strings": kind is Kind.PV})
    return {"modules": modules, "string_points": {pid: _spec_view(s) for pid, s in STRING_POINTS.items()},
            "max_strings": MAX_STRINGS, "max_name": MAX_NAME, "grid_variants": list(GRID_VARIANTS),
            "generic_balances": list(GENERIC_BALANCES), "wallbox_trust": list(WALLBOX_TRUST)}


# --- what Home Assistant has ----------------------------------------------------------------------------------------

@dataclass(frozen=True)
class EntityInfo:
    entity_id: str
    name: str
    state: str | None
    unit: str | None
    device_class: str | None
    state_class: str | None
    device: str | None
    area: str | None
    statistic: str | None        # "mean" | "sum" | None
    hidden: bool
    category: str | None         # "diagnostic" / "config" or None

    @property
    def domain(self) -> str:
        return self.entity_id.split(".", 1)[0]

    def view(self) -> dict[str, Any]:
        return {"entity_id": self.entity_id, "name": self.name, "state": self.state, "unit": self.unit,
                "device_class": self.device_class, "state_class": self.state_class, "device": self.device,
                "area": self.area, "statistic": self.statistic, "recorder_excluded": None}


class EntityIndex:
    def __init__(self, read: Read, now: Callable[[], float] = time.monotonic) -> None:
        self._read, self._now = read, now
        self._states: list[dict[str, Any]] = []
        self._states_at = -1e9
        self._registry: dict[str, Any] = {}
        self._registry_at = -1e9

    async def _refresh(self) -> None:
        now = self._now()
        if now - self._registry_at > REGISTRY_MAX_AGE:
            entities = await self._read({"type": "config/entity_registry/list"})
            devices = await self._read({"type": "config/device_registry/list"})
            areas = await self._read({"type": "config/area_registry/list"})
            stats = await self._read({"type": "recorder/list_statistic_ids"})
            self._registry = {
                "entities": {e["entity_id"]: e for e in entities or [] if isinstance(e, dict) and "entity_id" in e},
                "devices": {d["id"]: d for d in devices or [] if isinstance(d, dict) and "id" in d},
                "areas": {a["area_id"]: a.get("name") for a in areas or [] if isinstance(a, dict) and "area_id" in a},
                "stats": {s["statistic_id"]: s for s in stats or [] if isinstance(s, dict) and "statistic_id" in s},
            }
            self._registry_at = now
        if now - self._states_at > STATES_MAX_AGE:
            self._states = [s for s in await self._read({"type": "get_states"}) or []
                            if isinstance(s, dict) and isinstance(s.get("entity_id"), str)]
            self._states_at = now

    async def all(self) -> dict[str, EntityInfo]:
        await self._refresh()
        reg = self._registry
        out = {}
        for st in self._states:
            eid = st["entity_id"]
            attrs = st.get("attributes") or {}
            entry = reg["entities"].get(eid) or {}
            if entry.get("disabled_by"):
                continue
            device = reg["devices"].get(entry.get("device_id") or "") or {}
            area_id = entry.get("area_id") or device.get("area_id")
            stat = reg["stats"].get(eid) or {}
            out[eid] = EntityInfo(
                entity_id=eid, name=str(attrs.get("friendly_name") or entry.get("name") or entry.get("original_name") or eid),
                state=None if st.get("state") is None else str(st.get("state")), unit=attrs.get("unit_of_measurement"),
                device_class=attrs.get("device_class"), state_class=attrs.get("state_class"),
                device=device.get("name_by_user") or device.get("name"), area=reg["areas"].get(area_id or ""),
                statistic="sum" if stat.get("has_sum") else "mean" if stat.get("has_mean") else None,
                hidden=bool(entry.get("hidden_by")), category=entry.get("entity_category"))
        return out

    def device_of(self, entity_id: str) -> str | None:
        return (self._registry.get("entities", {}).get(entity_id) or {}).get("device_id")

    def same_device(self, entity_id: str) -> list[str]:
        dev = self.device_of(entity_id)
        if not dev:
            return []
        return [eid for eid, e in self._registry["entities"].items() if e.get("device_id") == dev and eid != entity_id]


def _fits(e: EntityInfo, unit: Unit | None) -> bool:
    return unit is None or compatible_unit(e.unit, unit, e.domain)


def _score(e: EntityInfo, q: str, unit: Unit | None) -> tuple[Any, ...]:
    hay = f"{e.entity_id} {e.name}".casefold()
    return (q not in hay if q else False, unit is not None and e.device_class != EXPECTED_CLASS.get(unit),
            e.statistic is None, e.hidden or e.category is not None, e.name.casefold())


def search(entities: dict[str, EntityInfo], q: str = "", unit: Unit | None = None, device_class: str | None = None,
           area: str | None = None, limit: int = SEARCH_LIMIT) -> tuple[list[EntityInfo], int]:
    terms = [t for t in q.casefold().split() if t]
    hits = []
    for e in entities.values():
        if not _fits(e, unit) or (device_class and e.device_class != device_class) or (area and e.area != area):
            continue
        hay = f"{e.entity_id} {e.name} {e.device or ''} {e.area or ''}".casefold()
        if all(t in hay for t in terms):
            hits.append(e)
    hits.sort(key=lambda e: _score(e, q.casefold(), unit))
    return hits[:limit], len(hits)


# --- check of a draft -------------------------------------------------------------------------------------------------

def _raw_points(plant: Any) -> list[tuple[str, dict[str, Any], PointSpec]]:
    """(metric id, raw point, spec) of every assigned point of a draft, valid or not."""
    out: list[tuple[str, dict[str, Any], PointSpec]] = []
    modules = plant.get("modules") if isinstance(plant, dict) else None
    for mid, raw in (modules or {}).items():
        if mid not in MODULES or not isinstance(raw, dict):
            continue
        catalog = CATALOG[MODULES[mid]]
        for pid, p in (raw.get("points") or {}).items() if isinstance(raw.get("points"), dict) else ():
            if pid in catalog and isinstance(p, dict):
                out.append((f"{mid}.{pid}", p, catalog[pid]))
        for s in raw.get("strings") or [] if isinstance(raw.get("strings"), list) else ():
            if not isinstance(s, dict) or not isinstance(s.get("points"), dict):
                continue
            for pid, p in s["points"].items():
                if pid in STRING_POINTS and isinstance(p, dict):
                    out.append((f"{mid}.{s.get('id')}.{pid}", p, STRING_POINTS[pid]))
    return out


SEVERITY = {"entity_missing": "error", "not_numeric": "error", "not_boolean": "error", "non_finite": "error",
            "unit_mismatch": "error", "domain_mismatch": "error", "unavailable": "warning", "negative_value": "warning",
            "no_statistics": "warning", "class_unexpected": "info", "signed_balance_only": "info"}
ORDER = {"error": 3, "warning": 2, "info": 1, "ok": 0}


def check_points(plant: Any, entities: dict[str, EntityInfo]) -> dict[str, dict[str, Any]]:
    """Per assigned point: status (ok/info/warning/error), codes, the normalised live value and the sensor's facts."""
    out: dict[str, dict[str, Any]] = {}
    grid = ((plant.get("modules") or {}).get("grid") or {}) if isinstance(plant, dict) else {}
    for metric_id, raw, spec in _raw_points(plant):
        eid = raw.get("entity")
        codes: list[str] = []
        value: Any = None
        e = entities.get(eid) if isinstance(eid, str) else None
        if e is None:
            codes.append("entity_missing")
        else:
            if not compatible_unit(e.unit, spec.unit, e.domain) and not (spec.unit in (Unit.BOOL, Unit.TEXT)):
                codes.append("unit_mismatch")
            elif spec.unit is Unit.BOOL and e.domain != "binary_sensor":
                codes.append("domain_mismatch")
            sign = raw.get("sign", 1) if raw.get("sign") in (1, -1) else 1
            point = Point(entity=e.entity_id, unit=spec.unit, role=spec.roles[0], sign=sign)
            norm = normalize(point, e.state, e.unit)     # type: ignore[arg-type]
            value = norm.value
            codes += [r.split(":", 1)[0] for r in norm.reasons if r.split(":", 1)[0] not in codes]
            if isinstance(value, float) and value < 0 and (spec.nonnegative or raw.get("nonnegative")):
                codes.append("negative_value")
            wanted = "sum" if spec.counter else "mean" if spec.unit in (Unit.W, Unit.PERCENT) else None
            if wanted and e.statistic != wanted:
                codes.append("no_statistics")
            expected = EXPECTED_CLASS.get(spec.unit)
            if expected and e.device_class and e.device_class != expected and spec.unit is not Unit.PERCENT:
                codes.append("class_unexpected")
        if metric_id == "battery.power" or (metric_id == "grid.power" and grid.get("variant", "signed") == "signed"):
            codes.append("signed_balance_only")       # older days: balance only (user decision 2026-11-15, 2)
        status = max((SEVERITY.get(c, "warning") for c in codes), key=ORDER.__getitem__, default="ok")
        out[metric_id] = {"status": status, "codes": codes, "value": value, "unit": spec.unit.value,
                          "sensor": e.view() if e else None}
    return out


# --- suggestions from the energy dashboard ----------------------------------------------------------------------------

POWER_HINTS = {"grid": ("grid", "netz", "bezug", "import"), "pv": ("solar", "pv", "production", "erzeugung"),
               "battery": ("battery", "batterie", "akku")}
# what the point means: grid and battery carry a sign (one sensor for both directions), PV is the generator's output
PREFER = {"grid": ("to_from", "transfer", "saldo", "netto", "net_power"),
          "battery": ("net", "battery_power", "batterieleistung"),
          "pv": ("solar_power", "solar_production", "pv_power", "erzeugung")}
AVOID = {"grid": ("consumption_from", "export_to", "import", "export", "bezug", "einspeis"),
         "battery": ("_charge", "_discharge", "laden", "entladen"),
         "pv": ("home_power_from", "to_battery", "to_grid", "additional", "consumption")}


def suggestions(prefs: Any, entities: dict[str, EntityInfo], same_device: Callable[[str], list[str]]) -> dict[str, list[dict[str, str]]]:
    """Metric id → candidates ``{entity_id, source}``: counters straight from the energy dashboard, power sensors as
    ``stat_rate`` there or as power sensors of the same device as a dashboard counter."""
    out: dict[str, list[dict[str, str]]] = {}

    def add(metric_id: str, eid: Any, source: str) -> None:
        if isinstance(eid, str) and eid in entities and all(c["entity_id"] != eid for c in out.get(metric_id, [])):
            out.setdefault(metric_id, []).append({"entity_id": eid, "source": source})

    def power_near(metric_id: str, counter: Any, kind: str) -> None:
        if not isinstance(counter, str):
            return
        candidates = [entities[e] for e in same_device(counter) if e in entities and _fits(entities[e], Unit.W)
                      and entities[e].device_class == "power"]

        def score(e: EntityInfo) -> tuple[int, int, int, str]:
            hay = f"{e.entity_id} {e.name}".casefold()
            return (-sum(h in hay for h in PREFER[kind]), sum(h in hay for h in AVOID[kind]),
                    -sum(h in hay for h in POWER_HINTS[kind]), e.name)
        candidates.sort(key=score)
        for e in candidates[:3]:
            add(metric_id, e.entity_id, "same_device")

    sources = (prefs or {}).get("energy_sources") or [] if isinstance(prefs, dict) else []
    pv = 0
    for s in sources:
        if not isinstance(s, dict):
            continue
        kind = s.get("type")
        if kind == "grid":
            add("grid.import_energy", s.get("stat_energy_from"), "energy_dashboard")
            add("grid.export_energy", s.get("stat_energy_to"), "energy_dashboard")
            add("grid.power", s.get("stat_rate"), "energy_dashboard")
            power_near("grid.power", s.get("stat_energy_from"), "grid")
            for flow in s.get("flow_from") or []:      # older layout of the grid source
                if isinstance(flow, dict):
                    add("grid.import_energy", flow.get("stat_energy_from"), "energy_dashboard")
            for flow in s.get("flow_to") or []:
                if isinstance(flow, dict):
                    add("grid.export_energy", flow.get("stat_energy_to"), "energy_dashboard")
        elif kind == "solar" and pv < 3:
            pv += 1
            add(f"pv_{pv}.energy_total", s.get("stat_energy_from"), "energy_dashboard")
            add(f"pv_{pv}.power", s.get("stat_rate"), "energy_dashboard")
            power_near(f"pv_{pv}.power", s.get("stat_energy_from"), "pv")
        elif kind == "battery":
            add("battery.charge_energy", s.get("stat_energy_to"), "energy_dashboard")
            add("battery.discharge_energy", s.get("stat_energy_from"), "energy_dashboard")
            add("battery.power", s.get("stat_rate"), "energy_dashboard")
            power_near("battery.power", s.get("stat_energy_from"), "battery")
    return out


# --- routes -------------------------------------------------------------------------------------------------------------

class CheckIn(BaseModel):
    plant: dict[str, Any]


def register(app: FastAPI, read: Read | None) -> None:
    index = EntityIndex(read) if read is not None else None

    async def entities() -> dict[str, EntityInfo]:
        if index is None:
            raise HTTPException(status_code=503, detail="home assistant not available")
        try:
            return await index.all()
        except (ConnectionError, TimeoutError, RuntimeError) as exc:
            log.warning("Home Assistant not readable for the interface: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail="home assistant not available") from None

    @app.get("/api/catalog")
    def catalog() -> dict[str, Any]:
        return catalog_view()

    @app.get("/api/entities")
    async def find(q: str = Query("", max_length=100), unit: str | None = None, device_class: str | None = None,
                   area: str | None = None, limit: int = Query(SEARCH_LIMIT, ge=1, le=SEARCH_LIMIT)) -> dict[str, Any]:
        try:
            wanted = Unit(unit) if unit else None
        except ValueError:
            raise HTTPException(status_code=422, detail="unknown unit") from None
        hits, total = search(await entities(), q, wanted, device_class, area, limit)
        return {"entities": [e.view() for e in hits], "total": total}

    @app.post("/api/check")
    async def check(body: CheckIn) -> dict[str, Any]:
        return {"points": check_points(body.plant, await entities())}

    @app.get("/api/suggestions")
    async def suggest() -> dict[str, Any]:
        found = await entities()
        try:
            prefs = await read({"type": "energy/get_prefs"}) if read is not None else None
        except (ConnectionError, TimeoutError, RuntimeError):
            prefs = None                                  # no dashboard (or not configured): no suggestions
        assert index is not None
        return {"points": suggestions(prefs, found, index.same_device)}


__all__ = ["Role", "catalog_view", "check_points", "register", "search", "suggestions"]
