"""Bridge between the plant model and the fixed mapping of the existing plant (P2 detail plan §5, step 2).

Two directions, one table:
- ``import_legacy`` turns the packaged mapping plus overrides into a ``plant.json`` candidate – a repeatable step that
  marks the plant with ``LEGACY_PROFILE`` (existing history calculation, InfluxDB, v1 projection stay available);
- ``legacy_mapping`` projects a plant back onto the old metric ids so that ``/v1`` keeps working from the same single
  source of truth until the apps have moved to ``/v2`` (end of P5 – then this module goes away).

``default_mapping.toml`` is a frozen template from now on: it still supplies units, sources and boundary texts of the
44 old ids, the plant supplies entity, sign and string names – exactly what an override could change.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from . import profile
from .config import MappingConfig, load_mapping
from .plant import CATALOG, LEGACY_PROFILE, MODULES, SCHEMA, STRING_POINTS, Plant, Problem, dump

# old metric id → new metric id (<module>.<point> or <module>.<string>.<point>)
TABLE: dict[str, str] = {
    "pv_internal": "pv_1.power",
    "kostal_balance": "pv_2.power",
    "kostal_ac": "pv_2.ac_power",
    "kostal_dc_sum": "pv_2.dc_power",
    "kostal_state": "pv_2.status",
    "battery_power": "battery.power",
    "soc": "battery.soc",
    "battery_capacity": "battery.capacity",
    "battery_max_charge": "battery.max_charge_power",
    "battery_max_discharge": "battery.max_discharge_power",
    "battery_charge_today": "battery.charge_energy",
    "battery_discharge_today": "battery.discharge_energy",
    "grid_power": "grid.power",
    "grid_import_energy": "grid.import_energy",
    "grid_export_energy": "grid.export_energy",
    "grid_phase1_power": "grid.phase_1_power",
    "grid_phase2_power": "grid.phase_2_power",
    "grid_phase3_power": "grid.phase_3_power",
    "consumption_total": "consumption.power",
    "wallbox_power": "wallbox.power",
    "wallbox_energy_total": "wallbox.energy_total",
    "wallbox_plug": "wallbox.plugged",
    "wallbox_charging": "wallbox.charging",
    "wallbox_mode": "wallbox.mode",
    "womo_shore_power": "generic_1.power",
    "womo_shore_energy_total": "generic_1.energy_total",
    "womo_battery_power": "generic_1.storage_power",
    "womo_battery_soc": "generic_1.storage_soc",
    "womo_ac_input": "generic_1.status",
    **{f"mppt{n}_{q}": f"pv_2.string_{n}.{q}" for n in (1, 2, 3) for q in ("power", "voltage", "current")},
    **{f"e3dc_string{n}_{q}": f"pv_1.string_{n}.{q}" for n in (1, 2) for q in ("power", "voltage", "current")},
}
DERIVED: dict[str, str] = {"generation_total": "generation.power", "home_power": "home.power"}
NEW_TO_OLD: dict[str, str] = {new: old for old, new in {**TABLE, **DERIVED}.items()}

# without these the fixed v1 evaluation cannot run (live.py reads them unguarded)
V1_REQUIRED = frozenset({"pv_internal", "kostal_balance", "kostal_ac", "battery_power", "grid_power",
                         "consumption_total", "wallbox_power"})
NAMES = profile.PLANT_NAMES                 # names and PV power of the own plant (profile, plan hub-publish P4)
OWN_PV_PEAK_W = profile.PV_PEAK_W


def _split(new_id: str) -> tuple[str, str | None, str]:
    module, *rest = new_id.split(".")
    return (module, rest[0], rest[1]) if len(rest) == 2 else (module, None, rest[0])


def import_legacy(mapping: MappingConfig) -> dict[str, Any]:
    """Plant candidate for the effective old mapping. Deterministic: the same mapping gives the same candidate."""
    modules: dict[str, dict[str, Any]] = {}
    for old, new in TABLE.items():
        m = mapping.metrics.get(old)
        if m is None:
            continue                                    # custom mappings without the motorhome
        module, string, pid = _split(new)
        spec = (STRING_POINTS if string else CATALOG[MODULES[module]])[pid]
        if m.unit is not spec.unit:
            raise ValueError(f"{old}: unit {m.unit.value} does not fit {new} ({spec.unit.value})")
        point: dict[str, Any] = {"entity": m.entity, "unit": m.unit.value, "sign": m.sign, "role": spec.roles[0].value,
                                 "valid_from": None}
        if m.nonnegative:
            point["nonnegative"] = True
        point["stale_after_s"] = mapping.sources[m.source].stale_after_s
        if m.freshness_from is not None:
            point["freshness_from"] = _split(TABLE[m.freshness_from])[2]
        target = modules.setdefault(module, {"points": {}})
        if string:
            names = mapping.e3dc_strings if module == "pv_1" else mapping.kostal_strings
            key = f"string{string[-1]}" if module == "pv_1" else f"mppt{string[-1]}"
            entry = next((s for s in target.setdefault("strings", []) if s["id"] == string), None)
            if entry is None:
                entry = {"id": string, "name": str(names.get(key, "")), "points": {}}
                target["strings"].append(entry)
            entry["points"][pid] = point
        else:
            target["points"][pid] = point

    out: dict[str, Any] = {}
    for mid in MODULES:                                  # slot order, so the candidate is stable
        if mid not in modules:
            continue
        body = modules[mid]
        if mid == "consumption":
            out[mid] = {"points": body["points"]}
            continue
        head: dict[str, Any] = {"enabled": True, "name": NAMES[mid]}
        if mid == "grid":
            head["variant"] = "signed"
        if mid == "wallbox":
            head["trust"] = "openwb_retained"
        if mid == "generic_1":
            head["balance"] = "part_of_home"            # the motorhome hangs on a socket in the house
        head["points"] = body["points"]
        if "strings" in body:
            head["strings"] = sorted(body["strings"], key=lambda s: s["id"])
        out[mid] = head
    out["e3dc_rscp"] = {"pv": "pv_1"}
    return {"schema": SCHEMA, "revision": 0, "profile": LEGACY_PROFILE, "pv_peak_w": OWN_PV_PEAK_W, "modules": out}


def legacy_mapping(plant: Plant, template: MappingConfig | None = None) -> MappingConfig | None:
    """The old mapping as seen from the plant, or ``None`` when the fixed v1 evaluation cannot describe the plant:
    no legacy profile, a value v1 needs is missing, or the plant differs from what v1 can express (see
    ``_project``/``representable``) – v1 must never show other values than v2 for the same plant."""
    if plant.profile != LEGACY_PROFILE or not profile.AVAILABLE:     # no own profile: the legacy calculation is shut
        return None
    template = template or load_mapping()
    projection = _project(plant, template)
    return projection if projection is not None and representable(plant, projection) else None


def _comparable(raw: dict[str, Any]) -> dict[str, Any]:
    """A plant without what v1 does not know at all and therefore cannot contradict: module names, validity dates,
    the revision, and slots that are switched off (they deliver nothing)."""
    modules: dict[str, Any] = {}
    for mid, module in raw["modules"].items():
        if module.get("enabled") is False:
            continue
        module = {k: v for k, v in module.items() if k != "name"}
        groups = [module.get("points", {}), *(s["points"] for s in module.get("strings", []))]
        for points in groups:
            for point in points.values():
                point["valid_from"] = None
        modules[mid] = module
    rest = {k: v for k, v in raw.items() if k != "pv_peak_w"}       # a display scale, unknown to v1
    return {**rest, "revision": 0, "modules": modules}


def representable(plant: Plant, projection: MappingConfig) -> bool:
    """True if importing the projection gives this plant again. Anything v1 cannot express – a third generator, a
    consumer beside home, another trust rule, split grid sensors, own age limits – makes the round trip differ."""
    return _comparable(dump(plant)) == _comparable(import_legacy(projection))


def legacy_rules(candidate: Plant, current: Plant | None) -> list[Problem]:
    """Activation rules around the legacy profile (``PlantStore(rules=(legacy_rules,))``): only the import of the
    existing plant sets it, and a plant that carries it stays within what v1 can express – otherwise ``/v1`` and the
    existing history would silently describe another plant."""
    if candidate.profile != LEGACY_PROFILE:
        return []
    if not profile.AVAILABLE:
        return [Problem("profile", "legacy_profile_unavailable",
                        "die Bestandsrechnung dieser Anlage ist in diesem Paket nicht enthalten (kein Anlagenprofil)")]
    if current is None or current.profile != LEGACY_PROFILE:
        return [Problem("profile", "profile_reserved",
                        "dieses Profil setzt nur die Übernahme der bisherigen Zuordnung (plant import-legacy)")]
    if legacy_mapping(candidate) is None:
        return [Problem("profile", "legacy_not_representable",
                        "mit dem Profil der bisherigen Anlage lassen sich nur Sensoren, Vorzeichen und Namen ändern; "
                        "für weitergehende Änderungen das Profil entfernen (die bisherige Historie und /v1 entfallen dann)")]
    return []


def _project(plant: Plant, template: MappingConfig) -> MappingConfig | None:
    points = plant.points()                              # switched-on modules only
    metrics = {}
    for old, base in template.metrics.items():
        point = points.get(TABLE[old]) if old in TABLE else None
        if point is None:
            if old in V1_REQUIRED:
                return None
            continue                                    # optional value the plant does not carry (any more)
        metrics[old] = replace(base, entity=point.entity, sign=point.sign)
    # a freshness reference must not dangle after an optional value went away
    metrics = {mid: (m if m.freshness_from in (None, *metrics) else replace(m, freshness_from=None))
               for mid, m in metrics.items()}

    def names(module: str, prefix: str, base: dict[str, Any]) -> dict[str, Any]:
        out = dict(base)
        for s in plant.modules[module].strings:
            out[f"{prefix}{s.id[-1]}"] = s.name
        return out

    return replace(template, metrics=metrics, revision=template.revision * 1000 + plant.revision,
                   kostal_strings=names("pv_2", "mppt", template.kostal_strings),
                   e3dc_strings=names("pv_1", "string", template.e3dc_strings))
