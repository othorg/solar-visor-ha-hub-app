"""Plant model (plan ha-addon 4.5, P2 detail plan §4.1).

A plant is a fixed set of module slots – grid, up to three PV generators, battery, wallbox, up to three generic extra
consumers and the total consumption. Every data point of a module names its Home Assistant entity, canonical unit,
sign, balance role and validity. ``plant.json`` is the single source of truth once it exists; it carries no secrets.

Rules that the evaluation relies on:
- a module that is switched off is a *known absence*: its term leaves the sums, it has no node, edge or points;
- a module that is switched on but has no value is *unknown* – ``null``, never 0;
- the catalogue below fixes unit and admissible roles of every point, the file still spells the role out so that a
  wrong balance role is rejected instead of silently reinterpreted;
- a PV generator marked ``balance: "full_feed_in"`` feeds the public grid through its own meter: the plant's grid
  sensor never sees it. It keeps its node, values, energy and strings but is never a term of the home balance
  (generation, consumption, home, residual, supply, autarky, self-consumption) – ``Plant.generators()`` and
  ``Plant.feed_in()`` are the one place that tells both apart.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from .models import Unit

log = logging.getLogger("solar_visor_hub.plant")
SCHEMA = 1
LEGACY_PROFILE = "own_plant_legacy"          # set by the import of the existing plant only (plant_legacy.py)
MAX_STRINGS = 6
MAX_NAME = 40
MAX_PV_PEAK_W = 10_000_000          # 10 MW: anything above is a typo (kW entered as W)
ENTITY_ID = re.compile(r"^(?!.+__)(?!_)[\da-z_]+(?<!_)\.(?!_)[\da-z_]+(?<!_)$")     # Home Assistant's own rule


class Role(StrEnum):
    AUTHORITATIVE = "authoritative"      # the value the balance is built from (full feed-in: its node and energy)
    SUBSET = "subset"                    # part of a consumer that is already counted (wallbox, extra consumer)
    CROSS_CHECK = "cross_check"          # second measurement of the same quantity – never added
    DETAIL = "detail"                    # shown, not part of any balance


class Kind(StrEnum):
    GRID = "grid"
    PV = "pv"
    BATTERY = "battery"
    WALLBOX = "wallbox"
    GENERIC = "generic"
    CONSUMPTION = "consumption"


@dataclass(frozen=True)
class PointSpec:
    unit: Unit
    roles: tuple[Role, ...]              # admissible roles, the first one is the default
    counter: bool = False                # ever-increasing meter reading
    nonnegative: bool = False            # by definition never negative – set regardless of the file


def _spec(unit: Unit, *roles: Role, counter: bool = False, nonnegative: bool = False) -> PointSpec:
    return PointSpec(unit, roles, counter, nonnegative)


_A, _S, _C, _D = Role.AUTHORITATIVE, Role.SUBSET, Role.CROSS_CHECK, Role.DETAIL

CATALOG: dict[Kind, dict[str, PointSpec]] = {
    Kind.GRID: {
        "power": _spec(Unit.W, _A),                          # variant "signed": + import, − export
        "import_power": _spec(Unit.W, _A, nonnegative=True),  # variant "split": two sensors, each one direction
        "export_power": _spec(Unit.W, _A, nonnegative=True),
        "import_energy": _spec(Unit.WH, _C, counter=True),
        "export_energy": _spec(Unit.WH, _C, counter=True),
        "phase_1_power": _spec(Unit.W, _D),
        "phase_2_power": _spec(Unit.W, _D),
        "phase_3_power": _spec(Unit.W, _D),
    },
    Kind.PV: {
        "power": _spec(Unit.W, _A),
        "ac_power": _spec(Unit.W, _C, _D),
        "dc_power": _spec(Unit.W, _D),
        "energy_total": _spec(Unit.WH, _C, counter=True),
        "status": _spec(Unit.TEXT, _D),
    },
    Kind.BATTERY: {
        "power": _spec(Unit.W, _A),                          # + charging, − discharging
        "soc": _spec(Unit.PERCENT, _D),
        "capacity": _spec(Unit.WH, _D),
        "max_charge_power": _spec(Unit.W, _D),
        "max_discharge_power": _spec(Unit.W, _D),
        "charge_energy": _spec(Unit.WH, _D),                 # device counters, often reset daily – display only
        "discharge_energy": _spec(Unit.WH, _D),
    },
    Kind.WALLBOX: {
        "power": _spec(Unit.W, _S),
        "energy_total": _spec(Unit.WH, _C, counter=True),
        "plugged": _spec(Unit.BOOL, _D),
        "charging": _spec(Unit.BOOL, _D),
        "mode": _spec(Unit.TEXT, _D),
    },
    Kind.GENERIC: {
        "power": _spec(Unit.W, _S),
        "energy_total": _spec(Unit.WH, _C, counter=True),
        "storage_power": _spec(Unit.W, _D),                  # own storage of the consumer: + charging
        "storage_soc": _spec(Unit.PERCENT, _D),
        "status": _spec(Unit.TEXT, _D),
    },
    Kind.CONSUMPTION: {
        "power": _spec(Unit.W, _A),                          # optional: measured total incl. wallbox and extras
    },
}
STRING_POINTS: dict[str, PointSpec] = {
    "power": _spec(Unit.W, _D),
    "voltage": _spec(Unit.V, _D),
    "current": _spec(Unit.A, _D),
}

MODULES: dict[str, Kind] = {
    "grid": Kind.GRID,
    "pv_1": Kind.PV, "pv_2": Kind.PV, "pv_3": Kind.PV,
    "battery": Kind.BATTERY,
    "wallbox": Kind.WALLBOX,
    "generic_1": Kind.GENERIC, "generic_2": Kind.GENERIC, "generic_3": Kind.GENERIC,
    "consumption": Kind.CONSUMPTION,
}
ALWAYS_ON = frozenset({"grid", "pv_1", "consumption"})
GRID_VARIANTS = ("signed", "split")
GENERIC_BALANCES = ("part_of_home", "beside_home")
FULL_FEED_IN = "full_feed_in"
# PV: absent = feeds the home (the canonical default, never written); "full_feed_in" = own meter straight into the grid
PV_BALANCES = (FULL_FEED_IN,)
# dashboard symbol of a module (user decision 2026-11-16); unset = the default of its kind (DEFAULT_ICONS)
ICONS = ("solar", "house", "battery", "grid", "wallbox", "camper", "plug")
DEFAULT_ICONS = {Kind.PV: "solar", Kind.GRID: "grid", Kind.BATTERY: "battery", Kind.WALLBOX: "wallbox",
                 Kind.GENERIC: "plug"}
WALLBOX_TRUST = ("none", "openwb_retained")
RSCP_KEY = "e3dc_rscp"

_NUMERIC_DOMAINS = ("sensor", "number", "input_number")
_TEXT_DOMAINS = ("sensor", "select", "input_select")


@dataclass(frozen=True)
class Point:
    entity: str
    unit: Unit
    role: Role
    sign: int = 1
    valid_from: datetime | None = None      # null: valid for all the history the entity has
    nonnegative: bool = False
    stale_after_s: int | None = None        # null: default of the unit (plant_live)
    freshness_from: str | None = None       # age taken from another point of the same module
    counter: bool = False                   # from the catalogue, not stored


@dataclass(frozen=True)
class PvString:
    id: str
    name: str
    points: dict[str, Point]


@dataclass(frozen=True)
class Module:
    id: str
    kind: Kind
    enabled: bool
    name: str = ""
    points: dict[str, Point] = field(default_factory=dict)
    strings: tuple[PvString, ...] = ()
    variant: str | None = None              # grid
    balance: str | None = None              # generic: GENERIC_BALANCES; pv: None (feeds the home) or PV_BALANCES
    trust: str | None = None                # wallbox
    icon: str | None = None                 # chosen symbol (ICONS); None = default of the kind

    @property
    def full_feed_in(self) -> bool:
        """A PV generator behind its own meter: shown, never part of the home balance."""
        return self.kind is Kind.PV and self.balance == FULL_FEED_IN


@dataclass(frozen=True)
class Plant:
    revision: int
    modules: dict[str, Module]              # always all slots, in the order of MODULES
    profile: str | None = None
    rscp_pv: str | None = None              # PV slot whose strings name the E3/DC RSCP trackers
    pv_peak_w: float | None = None          # installed PV power of the generators of the home, without full feed-in
                                            # (scale of the apps' gauges)
    schema: int = SCHEMA

    def active(self) -> list[Module]:
        return [m for m in self.modules.values() if m.enabled]

    def generators(self) -> list[Module]:
        """Switched-on PV generators that feed the home – the terms of every balance (generation, supply, …)."""
        return [m for m in self.active() if m.kind is Kind.PV and not m.full_feed_in]

    def feed_in(self) -> list[Module]:
        """Switched-on PV generators with full feed-in: own node and energy, never a term of the home balance."""
        return [m for m in self.active() if m.full_feed_in]

    def points(self) -> dict[str, Point]:
        """Metric id → point of every switched-on module: ``pv_1.power``, ``pv_2.string_1.voltage``."""
        out: dict[str, Point] = {}
        for m in self.active():
            for pid, p in m.points.items():
                out[f"{m.id}.{pid}"] = p
            for s in m.strings:
                for pid, p in s.points.items():
                    out[f"{m.id}.{s.id}.{pid}"] = p
        return out

    def entities(self) -> list[str]:
        return sorted({p.entity for p in self.points().values()})


@dataclass(frozen=True)
class Problem:
    path: str               # "modules.pv_2.points.power.role"
    code: str               # stable, for the interface
    message: str            # German, for the user


class PlantError(ValueError):
    def __init__(self, problems: list[Problem]) -> None:
        super().__init__("; ".join(f"{p.path}: {p.message}" for p in problems[:5]))
        self.problems = problems

    @property
    def codes(self) -> set[str]:
        return {p.code for p in self.problems}


class RevisionConflict(ValueError):
    def __init__(self, expected: int, current: int) -> None:
        super().__init__(f"plant revision is {current}, expected {expected}")
        self.expected, self.current = expected, current


# --- parsing and validation --------------------------------------------------------------------------------------

class _Check:
    def __init__(self) -> None:
        self.problems: list[Problem] = []

    def add(self, path: str, code: str, message: str) -> None:
        self.problems.append(Problem(path, code, message))


def _domain_fits(entity: str, unit: Unit) -> bool:
    domain = entity.split(".", 1)[0]
    if unit is Unit.BOOL:
        return domain == "binary_sensor"
    if unit is Unit.TEXT:
        return domain in _TEXT_DOMAINS
    return domain in _NUMERIC_DOMAINS


def _point(raw: Any, spec: PointSpec, path: str, check: _Check) -> Point | None:
    if not isinstance(raw, dict):
        check.add(path, "not_an_object", "Datenpunkt muss ein Objekt sein")
        return None
    unknown = set(raw) - {"entity", "unit", "sign", "role", "valid_from", "nonnegative", "stale_after_s", "freshness_from"}
    for key in sorted(unknown):
        check.add(f"{path}.{key}", "unknown_field", "unbekanntes Feld")
    ok = True
    entity = raw.get("entity")
    if not isinstance(entity, str) or not ENTITY_ID.fullmatch(entity):       # fullmatch: "$" would accept a final newline
        check.add(f"{path}.entity", "invalid_entity", "keine gültige Home-Assistant-Entität")
        ok = False
    elif not _domain_fits(entity, spec.unit):
        check.add(f"{path}.entity", "domain_mismatch",
                  f"die Art der Entität passt nicht zur Einheit {spec.unit.value}")
        ok = False
    if raw.get("unit") != spec.unit.value:
        check.add(f"{path}.unit", "unit_mismatch", f"Einheit muss {spec.unit.value} sein")
        ok = False
    try:
        role = Role(raw.get("role"))
    except ValueError:
        role = None
    if role is None or role not in spec.roles:
        allowed = ", ".join(r.value for r in spec.roles)
        check.add(f"{path}.role", "invalid_role", f"Bilanzrolle muss eine von {allowed} sein")
        ok = False
    sign = raw.get("sign", 1)
    if isinstance(sign, bool) or sign not in (1, -1):
        check.add(f"{path}.sign", "invalid_sign", "Vorzeichen muss +1 oder −1 sein")
        ok = False
    valid_from = None
    if raw.get("valid_from") is not None:
        try:
            valid_from = datetime.fromisoformat(str(raw["valid_from"]).replace("Z", "+00:00"))
        except ValueError:
            valid_from = None
        if valid_from is None or valid_from.tzinfo is None:
            check.add(f"{path}.valid_from", "invalid_valid_from", "Zeitpunkt mit Zeitzone erwartet (ISO 8601)")
            ok = False
    nonnegative = raw.get("nonnegative", False)
    if not isinstance(nonnegative, bool):
        check.add(f"{path}.nonnegative", "invalid_flag", "wahr oder falsch erwartet")
        ok = False
    stale = raw.get("stale_after_s")
    if stale is not None and (isinstance(stale, bool) or not isinstance(stale, int) or not 5 <= stale <= 86_400):
        check.add(f"{path}.stale_after_s", "invalid_stale_after", "Alterungsgrenze zwischen 5 s und 24 h erwartet")
        ok = False
    fresh = raw.get("freshness_from")
    if fresh is not None and not isinstance(fresh, str):
        check.add(f"{path}.freshness_from", "invalid_freshness_from", "Name eines Datenpunkts erwartet")
        ok = False
    if not ok or role is None:
        return None
    return Point(entity=entity, unit=spec.unit, role=role, sign=sign, valid_from=valid_from,
                 nonnegative=nonnegative or spec.nonnegative, stale_after_s=stale, freshness_from=fresh,
                 counter=spec.counter)


def _points(raw: Any, catalog: dict[str, PointSpec], path: str, check: _Check) -> dict[str, Point]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        check.add(path, "not_an_object", "Datenpunkte müssen ein Objekt sein")
        return {}
    out: dict[str, Point] = {}
    for pid in raw:
        if pid not in catalog:
            check.add(f"{path}.{pid}", "unknown_point", "diesen Datenpunkt gibt es für das Modul nicht")
            continue
        point = _point(raw[pid], catalog[pid], f"{path}.{pid}", check)
        if point is not None:
            out[pid] = point
    # the reference lives in the same group of points: a module point names a module point, a string point a point
    # of the same string
    for pid, point in out.items():
        ref = point.freshness_from
        if ref is not None and (ref == pid or ref not in out):
            check.add(f"{path}.{pid}.freshness_from", "invalid_freshness_from",
                      "muss auf einen anderen belegten Datenpunkt desselben Moduls zeigen")
    return out


def _name(raw: Any, path: str, check: _Check, required: bool) -> str:
    name = raw if isinstance(raw, str) else ""
    if not isinstance(raw, (str, type(None))) or len(name) > MAX_NAME or name != name.strip():
        check.add(path, "invalid_name", f"Name mit höchstens {MAX_NAME} Zeichen, ohne Leerzeichen am Rand")
    elif required and not name:
        check.add(path, "name_missing", "ein eingeschaltetes Modul braucht einen Namen")
    return name


def _strings(raw: Any, path: str, check: _Check) -> tuple[PvString, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or len(raw) > MAX_STRINGS:
        check.add(path, "invalid_strings", f"Liste mit höchstens {MAX_STRINGS} Strings erwartet")
        return ()
    out: list[PvString] = []
    for i, s in enumerate(raw):
        here = f"{path}[{i}]"
        if not isinstance(s, dict):
            check.add(here, "not_an_object", "String muss ein Objekt sein")
            continue
        sid = s.get("id")
        if not isinstance(sid, str) or not re.fullmatch(rf"string_[1-{MAX_STRINGS}]", sid) or any(o.id == sid for o in out):
            check.add(f"{here}.id", "invalid_string_id", f"string_1 bis string_{MAX_STRINGS}, jede nur einmal")
            continue
        out.append(PvString(sid, _name(s.get("name"), f"{here}.name", check, required=False),
                            _points(s.get("points"), STRING_POINTS, f"{here}.points", check)))
    return tuple(out)


def _module(mid: str, raw: Any, check: _Check) -> Module:
    kind, path = MODULES[mid], f"modules.{mid}"
    if raw is None:
        if kind is Kind.CONSUMPTION or mid not in ALWAYS_ON:
            return Module(mid, kind, enabled=mid in ALWAYS_ON)
        raw = {}                    # grid and the first PV generator are part of every plant: report what is missing
    if not isinstance(raw, dict):
        check.add(path, "not_an_object", "Modul muss ein Objekt sein")
        return Module(mid, kind, enabled=False)
    allowed = {"points"} | ({"enabled", "name", "icon"} if kind is not Kind.CONSUMPTION else set())
    allowed |= {Kind.GRID: {"variant"}, Kind.PV: {"strings", "balance"}, Kind.WALLBOX: {"trust"},
                Kind.GENERIC: {"balance"}}.get(kind, set())
    for key in sorted(set(raw) - allowed):
        check.add(f"{path}.{key}", "unknown_field", "unbekanntes Feld")

    enabled = raw.get("enabled", mid in ALWAYS_ON)
    if not isinstance(enabled, bool):
        check.add(f"{path}.enabled", "invalid_flag", "wahr oder falsch erwartet")
        enabled = False
    if mid in ALWAYS_ON and not enabled:
        check.add(f"{path}.enabled", "module_required", "dieses Modul lässt sich nicht abschalten")
        enabled = True
    name = "" if kind is Kind.CONSUMPTION else _name(raw.get("name"), f"{path}.name", check, required=enabled)
    points = _points(raw.get("points"), CATALOG[kind], f"{path}.points", check)
    icon = raw.get("icon")
    if icon is not None and icon not in ICONS:
        check.add(f"{path}.icon", "invalid_icon", "unbekanntes Symbol")
        icon = None
    module = Module(mid, kind, enabled, name, points, icon=icon)
    given = set(raw["points"]) if isinstance(raw.get("points"), dict) else set()     # an invalid point is not "missing"

    if kind is Kind.GRID:
        variant = raw.get("variant", "signed")
        if variant not in GRID_VARIANTS:
            check.add(f"{path}.variant", "invalid_variant", "signed (ein Sensor mit Vorzeichen) oder split (zwei Sensoren)")
            variant = "signed"
        module = replace(module, variant=variant)
        wanted = ("power",) if variant == "signed" else ("import_power", "export_power")
        other = ("import_power", "export_power") if variant == "signed" else ("power",)
        for pid in other:
            if pid in points:
                check.add(f"{path}.points.{pid}", "point_not_in_variant", "passt nicht zur gewählten Netz-Variante")
        _require(module, wanted, given, path, check)
    elif kind is Kind.PV:
        balance = raw.get("balance")
        if balance is not None and balance not in PV_BALANCES:
            check.add(f"{path}.balance", "invalid_balance", "full_feed_in oder weglassen (speist ins Haus)")
            balance = None
        module = replace(module, strings=_strings(raw.get("strings"), f"{path}.strings", check), balance=balance)
        _require(module, ("power",), given, path, check)
    elif kind is Kind.BATTERY:
        _require(module, ("power",), given, path, check)
    elif kind is Kind.WALLBOX:
        trust = raw.get("trust", "none")
        if trust not in WALLBOX_TRUST:
            check.add(f"{path}.trust", "invalid_trust", "none oder openwb_retained")
            trust = "none"
        if trust == "openwb_retained" and enabled and "energy_total" not in points:
            check.add(f"{path}.trust", "trust_needs_counter", "die Prüfung braucht den Energiezähler der Wallbox")
        module = replace(module, trust=trust)
        _require(module, ("power",), given, path, check)
    elif kind is Kind.GENERIC:
        balance = raw.get("balance", "part_of_home")
        if balance not in GENERIC_BALANCES:
            check.add(f"{path}.balance", "invalid_balance", "part_of_home oder beside_home")
            balance = "part_of_home"
        module = replace(module, balance=balance)
        _require(module, ("power",), given, path, check)
    return module


def _require(module: Module, point_ids: tuple[str, ...], given: set[str], path: str, check: _Check) -> None:
    if not module.enabled:
        return                      # a module that is switched off may keep or lack its assignments
    for pid in point_ids:
        if pid not in given:
            check.add(f"{path}.points.{pid}", "point_missing", "dieser Datenpunkt ist für das eingeschaltete Modul Pflicht")


def parse(raw: Any) -> Plant:
    """Validated plant or ``PlantError`` with every problem found (the interface shows them all at once)."""
    check = _Check()
    if not isinstance(raw, dict):
        raise PlantError([Problem("", "not_an_object", "Anlage muss ein Objekt sein")])
    if raw.get("schema") != SCHEMA:
        raise PlantError([Problem("schema", "unsupported_schema", f"nicht unterstütztes Schema {raw.get('schema')!r}")])
    for key in sorted(set(raw) - {"schema", "revision", "profile", "modules", "pv_peak_w"}):
        check.add(key, "unknown_field", "unbekanntes Feld")
    pv_peak_w = raw.get("pv_peak_w")
    if pv_peak_w is not None and (isinstance(pv_peak_w, bool) or not isinstance(pv_peak_w, int | float)
                                  or not 0 < pv_peak_w <= MAX_PV_PEAK_W):
        check.add("pv_peak_w", "invalid_pv_peak", f"Leistung in W über 0 und höchstens {MAX_PV_PEAK_W:,} erwartet")
        pv_peak_w = None
    revision = raw.get("revision", 0)
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        check.add("revision", "invalid_revision", "ganze Zahl ab 0 erwartet")
        revision = 0
    profile = raw.get("profile")
    if profile not in (None, LEGACY_PROFILE):
        check.add("profile", "invalid_profile", "unbekanntes Profil")
        profile = None
    raw_modules = raw.get("modules")
    if not isinstance(raw_modules, dict):
        raise PlantError(check.problems + [Problem("modules", "not_an_object", "Module müssen ein Objekt sein")])
    for key in sorted(set(raw_modules) - set(MODULES) - {RSCP_KEY}):
        check.add(f"modules.{key}", "unknown_module", "diesen Modul-Platz gibt es nicht")
    modules = {mid: _module(mid, raw_modules.get(mid), check) for mid in MODULES}

    rscp_pv = None
    rscp = raw_modules.get(RSCP_KEY)
    if rscp is not None:
        rscp_pv = rscp.get("pv") if isinstance(rscp, dict) and set(rscp) == {"pv"} else None
        if (not isinstance(rscp_pv, str) or rscp_pv not in modules or modules[rscp_pv].kind is not Kind.PV
                or modules[rscp_pv].full_feed_in):
            # the E3/DC trackers belong to the home's bus, never to a generator behind its own meter
            check.add(f"modules.{RSCP_KEY}.pv", "invalid_rscp_pv",
                      "genau ein PV-Platz im Haus erwartet (keine Volleinspeisung), z. B. pv_1")
            rscp_pv = None

    # without a generator of the home every balance would be empty: at least one switched-on PV feeds the home
    feed_in = [m for m in modules.values() if m.enabled and m.full_feed_in]
    if feed_in and not any(m.enabled and m.kind is Kind.PV and not m.full_feed_in for m in modules.values()):
        check.add(f"modules.{feed_in[0].id}.balance", "full_feed_in_needs_house_pv",
                  "mindestens ein eingeschalteter PV-Erzeuger muss ins Haus speisen (ohne Volleinspeisung)")

    # one entity must not be counted twice: at most one authoritative or subset use in the whole plant
    counted: dict[str, str] = {}
    for m in modules.values():
        for pid, p in m.points.items():
            if p.role in (Role.AUTHORITATIVE, Role.SUBSET):
                first = counted.setdefault(p.entity, f"{m.id}.{pid}")
                if first != f"{m.id}.{pid}":
                    check.add(f"modules.{m.id}.points.{pid}.entity", "entity_counted_twice",
                              f"die Entität ist bereits in {first} Teil der Bilanz")
    if check.problems:
        raise PlantError(check.problems)
    return Plant(revision=revision, modules=modules, profile=profile, rscp_pv=rscp_pv,
                 pv_peak_w=None if pv_peak_w is None else float(pv_peak_w))


def _dump_point(p: Point) -> dict[str, Any]:
    out: dict[str, Any] = {"entity": p.entity, "unit": p.unit.value, "sign": p.sign, "role": p.role.value,
                           "valid_from": p.valid_from.isoformat().replace("+00:00", "Z") if p.valid_from else None}
    if p.nonnegative:
        out["nonnegative"] = True
    if p.stale_after_s is not None:
        out["stale_after_s"] = p.stale_after_s
    if p.freshness_from is not None:
        out["freshness_from"] = p.freshness_from
    return out


def dump(plant: Plant) -> dict[str, Any]:
    """Canonical form: every slot that is switched on or still carries assignments; defaults are left out."""
    modules: dict[str, Any] = {}
    for m in plant.modules.values():
        points = {pid: _dump_point(p) for pid, p in m.points.items()}
        if m.kind is Kind.CONSUMPTION:
            if points:
                modules[m.id] = {"points": points}
            continue
        if not (m.enabled or m.name or points or m.strings or m.icon or (m.kind is Kind.PV and m.balance)):
            continue
        out: dict[str, Any] = {"enabled": m.enabled, "name": m.name}
        if m.icon is not None:
            out["icon"] = m.icon
        if m.kind is Kind.GRID:
            out["variant"] = m.variant
        if m.kind is Kind.WALLBOX:
            out["trust"] = m.trust
        if m.kind is Kind.GENERIC or (m.kind is Kind.PV and m.balance is not None):
            out["balance"] = m.balance              # pv: only when set – the default stays out (legacy round trip)
        out["points"] = points
        if m.strings:
            out["strings"] = [{"id": s.id, "name": s.name, "points": {pid: _dump_point(p) for pid, p in s.points.items()}}
                              for s in m.strings]
        modules[m.id] = out
    if plant.rscp_pv is not None:
        modules[RSCP_KEY] = {"pv": plant.rscp_pv}
    out: dict[str, Any] = {"schema": plant.schema, "revision": plant.revision, "profile": plant.profile}
    if plant.pv_peak_w is not None:
        out["pv_peak_w"] = plant.pv_peak_w
    return {**out, "modules": modules}


# --- persistence -------------------------------------------------------------------------------------------------

Rule = Callable[[Plant, "Plant | None"], list[Problem]]


class PlantStore:
    """``plant.json`` with atomic activation. ``current`` is ``None`` until a plant has been activated; an unreadable
    or invalid file is an error at start – the hub must not fall back to another configuration silently.

    ``rules`` are checked on every activation in addition to the plant's own validation (candidate, current plant) –
    the place for what depends on more than the file, e.g. what the legacy profile allows (plant_legacy.py).
    Several processes may hold a store (hub, CLI): activation takes a file lock and compares the expected revision
    with the file, not with what this process loaded earlier."""

    def __init__(self, path: Path, rules: tuple[Rule, ...] = (), tolerant: bool = False) -> None:
        """``tolerant``: an unreadable file is no error here (the interface repairs it, ``activate(repair=True)``)."""
        self.path = path
        self.rules = rules
        self._lock = threading.Lock()
        self._listeners: list[Callable[[Plant], None]] = []
        self.current: Plant | None = self.state()[0] if tolerant else self._load()

    def _load(self) -> Plant | None:
        return parse(json.loads(self.path.read_text(encoding="utf-8"))) if self.path.exists() else None

    def state(self, min_revision: int = 0) -> tuple[Plant | None, int, bool]:
        """(plant, revision, readable). An unreadable file counts with the highest revision it or the kept previous
        plant still names (and ``min_revision``, e.g. the archive's), so a repaired plant gets a new number."""
        try:
            plant = self._load()
            return plant, plant.revision if plant else 0, True
        except (PlantError, ValueError, OSError):
            revision = min_revision
            for path in (self.path, self.path.with_name("plant.prev.json")):
                try:
                    raw = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                r = raw.get("revision") if isinstance(raw, dict) else None
                if isinstance(r, int) and not isinstance(r, bool) and r >= 0:
                    revision = max(revision, r)
            return None, revision, False

    @property
    def revision(self) -> int:
        return self.current.revision if self.current else 0

    def subscribe(self, listener: Callable[[Plant], None]) -> None:
        self._listeners.append(listener)

    def activate(self, candidate: Any, expected_revision: int, keep_previous: Path | None = None,
                 check: Callable[[Plant], None] | None = None, repair: bool = False, min_revision: int = 0) -> Plant:
        """Validates the whole candidate and makes it the plant with the next revision. ``expected_revision`` is the
        revision the editor started from: a change made meanwhile – by this or another process – is never
        overwritten. ``keep_previous``: the plant replaced is kept there (restore); ``check``: a further test of the
        candidate under the same lock (the interface's trial build of the runtime) – raising keeps the old plant.
        ``repair``: an unreadable plant file is replaced (kept as ``plant.broken.json``) instead of stopping everything –
        the way out of the setup mode it caused (Codex P4 review M1)."""
        with self._lock, open(self.path.with_name(f".{self.path.name}.lock"), "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if repair:
                current, revision, readable = self.state(min_revision)
            else:
                current = self._load()                  # what is active now, whoever wrote it
                revision, readable = (current.revision if current else 0), True
            self.current = current
            if expected_revision != revision:
                raise RevisionConflict(expected_revision, revision)
            plant = replace(parse(candidate), revision=revision + 1)
            problems = [p for rule in self.rules for p in rule(plant, self.current)]
            if problems:
                raise PlantError(problems)
            if check is not None:
                check(plant)
            if keep_previous is not None and self.current is not None:
                self._write(dump(self.current), keep_previous)
            if not readable and self.path.exists():
                os.replace(self.path, self.path.with_name("plant.broken.json"))
            self._write(dump(plant))
            self.current = plant
        for listener in self._listeners:
            try:
                listener(plant)
            except Exception:   # noqa: BLE001 – the plant is active; a failing listener must not report otherwise
                log.exception("plant listener failed after activation of revision %s", plant.revision)
        return plant

    def _write(self, data: dict[str, Any], target: Path | None = None) -> None:
        target = target or self.path
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, target)             # atomic: a crash leaves the previous plant in place
            directory = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory)             # the rename itself has to survive a power cut
            finally:
                os.close(directory)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
