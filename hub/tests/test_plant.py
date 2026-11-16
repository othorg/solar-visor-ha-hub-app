"""Plant model (plan ha-addon 4.5, P2 detail plan step 1): catalogue, validation, atomic activation."""

import copy
import json
import os

import pytest

from solar_visor_hub import plant as pl
from solar_visor_hub.models import Unit
from solar_visor_hub.plant import PlantError, PlantStore, Role, RevisionConflict, dump, parse


def point(entity, unit="W", role="authoritative", **extra):
    return {"entity": entity, "unit": unit, "sign": 1, "role": role, "valid_from": None, **extra}


MINIMAL = {
    "schema": 1, "revision": 1, "profile": None,
    "modules": {
        "grid": {"enabled": True, "name": "Netz", "variant": "signed", "points": {"power": point("sensor.netz_leistung")}},
        "pv_1": {"enabled": True, "name": "PV", "points": {
            "power": point("sensor.wechselrichter_leistung", nonnegative=True, valid_from="2026-11-15T09:00:00Z")}},
    },
}

FULL = {
    "schema": 1, "revision": 7, "profile": None,
    "modules": {
        "grid": {"enabled": True, "name": "Netz", "variant": "split", "points": {
            "import_power": point("sensor.bezug", nonnegative=True),
            "export_power": point("sensor.einspeisung", nonnegative=True),
            "import_energy": point("sensor.bezug_zaehler", "Wh", "cross_check", stale_after_s=900)}},
        "pv_1": {"enabled": True, "name": "Dach Süd", "points": {"power": point("sensor.pv_sued", nonnegative=True)},
                 "strings": [{"id": "string_1", "name": "Ost", "points": {
                     "power": point("sensor.s1_p", role="detail"), "voltage": point("sensor.s1_u", "V", "detail"),
                     "current": point("sensor.s1_i", "A", "detail")}}]},
        "pv_2": {"enabled": True, "name": "Garage", "points": {
            "power": point("sensor.pv_garage"), "ac_power": point("sensor.pv_garage_ac", role="cross_check"),
            "status": point("sensor.pv_garage_status", "text", "detail")}},
        "pv_3": {"enabled": False, "name": "Carport", "points": {"power": point("sensor.pv_carport")}},
        "battery": {"enabled": True, "name": "Speicher", "points": {
            "power": point("sensor.speicher_leistung", sign=-1), "soc": point("sensor.speicher_soc", "%", "detail")}},
        "wallbox": {"enabled": True, "name": "Wallbox", "trust": "openwb_retained", "points": {
            "power": point("sensor.wb_leistung", role="subset"),
            "energy_total": point("sensor.wb_zaehler", "Wh", "cross_check"),
            "plugged": point("binary_sensor.wb_stecker", "bool", "detail"),
            "mode": point("select.wb_modus", "text", "detail")}},
        "generic_1": {"enabled": True, "name": "Wohnmobil", "balance": "part_of_home", "points": {
            "power": point("sensor.womo_leistung", role="subset"),
            "storage_power": point("sensor.womo_akku_leistung", role="detail"),
            "storage_soc": point("sensor.womo_akku_soc", "%", "detail", freshness_from="storage_power")}},
        "generic_2": {"enabled": True, "name": "Sauna", "balance": "beside_home", "points": {
            "power": point("sensor.sauna_leistung", role="subset")}},
        "consumption": {"points": {"power": point("sensor.verbrauch_gesamt")}},
        "e3dc_rscp": {"pv": "pv_1"},
    },
}


def problems(raw):
    with pytest.raises(PlantError) as info:
        parse(raw)
    return {(p.path, p.code) for p in info.value.problems}


def changed(base, path, value):
    raw = copy.deepcopy(base)
    node = raw
    *head, last = path
    for key in head:
        node = node[key]
    if value is ...:
        del node[last]
    else:
        node[last] = value
    return raw


def test_examples_survive_the_round_trip_unchanged():
    for raw in (MINIMAL, FULL):
        assert dump(parse(raw)) == raw
        assert dump(parse(dump(parse(raw)))) == raw


def test_minimal_plant_has_every_slot_and_only_the_two_active_ones_carry_points():
    p = parse(MINIMAL)
    assert list(p.modules) == list(pl.MODULES)
    assert [m.id for m in p.active()] == ["grid", "pv_1", "consumption"]
    assert sorted(p.points()) == ["grid.power", "pv_1.power"]
    assert p.modules["battery"].enabled is False and p.modules["battery"].points == {}
    assert p.modules["consumption"].points == {}                  # optional sensor: the total is calculated then
    assert p.points()["pv_1.power"].valid_from.isoformat() == "2026-11-15T09:00:00+00:00"


def test_metric_ids_units_and_catalogue_flags():
    p = parse(FULL)
    pts = p.points()
    assert "pv_1.string_1.voltage" in pts and pts["pv_1.string_1.voltage"].unit is Unit.V
    assert pts["wallbox.energy_total"].counter and not pts["wallbox.power"].counter
    assert pts["battery.power"].sign == -1 and pts["generic_1.storage_soc"].freshness_from == "storage_power"
    assert pts["grid.import_energy"].stale_after_s == 900 and pts["grid.import_power"].stale_after_s is None
    assert "pv_3.power" not in pts                                # switched off: keeps the assignment, delivers nothing
    assert p.modules["pv_3"].points["power"].entity == "sensor.pv_carport"
    assert "sensor.pv_carport" not in p.entities() and "sensor.bezug" in p.entities()
    assert p.rscp_pv == "pv_1" and p.modules["generic_2"].balance == "beside_home"


def test_renaming_changes_no_id():
    renamed = changed(FULL, ("modules", "generic_1", "name"), "Camper")
    assert sorted(parse(renamed).points()) == sorted(parse(FULL).points())


def test_grid_and_first_pv_cannot_be_missing_or_switched_off():
    assert ("modules.grid.points.power", "point_missing") in problems(changed(MINIMAL, ("modules", "grid"), ...))
    assert ("modules.pv_1.points.power", "point_missing") in problems(changed(MINIMAL, ("modules", "pv_1"), ...))
    assert ("modules.pv_1.enabled", "module_required") in problems(changed(MINIMAL, ("modules", "pv_1", "enabled"), False))


def test_a_switched_on_module_needs_its_authoritative_point_and_a_name():
    raw = changed(MINIMAL, ("modules", "battery"), {"enabled": True, "name": "", "points": {
        "soc": point("sensor.soc", "%", "detail")}})
    assert problems(raw) == {("modules.battery.points.power", "point_missing"), ("modules.battery.name", "name_missing")}
    off = changed(MINIMAL, ("modules", "battery"), {"enabled": False, "name": "", "points": {}})
    assert parse(off).modules["battery"].enabled is False         # switched off: nothing is required


def test_invalid_roles_units_signs_and_entities_are_rejected():
    assert ("modules.pv_1.points.power.role", "invalid_role") in problems(
        changed(MINIMAL, ("modules", "pv_1", "points", "power", "role"), "subset"))
    assert ("modules.pv_1.points.power.role", "invalid_role") in problems(
        changed(MINIMAL, ("modules", "pv_1", "points", "power", "role"), "derived"))
    assert ("modules.pv_1.points.power.unit", "unit_mismatch") in problems(
        changed(MINIMAL, ("modules", "pv_1", "points", "power", "unit"), "Wh"))
    for bad in (0, 2, True, "1"):
        assert ("modules.grid.points.power.sign", "invalid_sign") in problems(
            changed(MINIMAL, ("modules", "grid", "points", "power", "sign"), bad))
    for bad in ("Sensor.X", "sensor.", "netz", "sensor.a__b", 5):
        assert ("modules.grid.points.power.entity", "invalid_entity") in problems(
            changed(MINIMAL, ("modules", "grid", "points", "power", "entity"), bad))
    assert ("modules.grid.points.power.entity", "domain_mismatch") in problems(
        changed(MINIMAL, ("modules", "grid", "points", "power", "entity"), "binary_sensor.netz"))
    assert ("modules.wallbox.points.plugged.entity", "domain_mismatch") in problems(
        changed(FULL, ("modules", "wallbox", "points", "plugged", "entity"), "sensor.wb_stecker"))
    assert ("modules.grid.points.power.valid_from", "invalid_valid_from") in problems(
        changed(MINIMAL, ("modules", "grid", "points", "power", "valid_from"), "2026-11-15T09:00:00"))   # no zone
    assert ("modules.grid.points.power.stale_after_s", "invalid_stale_after") in problems(
        changed(MINIMAL, ("modules", "grid", "points", "power", "stale_after_s"), 1))


def test_one_entity_is_counted_once_but_may_be_shown_twice():
    twice = changed(FULL, ("modules", "pv_2", "points", "power", "entity"), "sensor.pv_sued")
    assert problems(twice) == {("modules.pv_2.points.power.entity", "entity_counted_twice")}
    as_subset = changed(FULL, ("modules", "generic_2", "points", "power", "entity"), "sensor.wb_leistung")
    assert ("modules.generic_2.points.power.entity", "entity_counted_twice") in problems(as_subset)
    shown = changed(FULL, ("modules", "pv_2", "points", "ac_power", "entity"), "sensor.pv_garage")
    assert parse(shown).points()["pv_2.ac_power"].role is Role.CROSS_CHECK     # a cross-check is never added


def test_unknown_things_are_named_instead_of_ignored():
    assert problems(changed(MINIMAL, ("schema",), 2)) == {("schema", "unsupported_schema")}
    assert ("modules.pv_4", "unknown_module") in problems(changed(MINIMAL, ("modules", "pv_4"), {"enabled": False}))
    assert ("modules.pv_1.points.soc", "unknown_point") in problems(
        changed(MINIMAL, ("modules", "pv_1", "points", "soc"), point("sensor.soc", "%", "detail")))
    assert ("modules.pv_1.points.power.formula", "unknown_field") in problems(
        changed(MINIMAL, ("modules", "pv_1", "points", "power", "formula"), "{{ 1 }}"))
    assert ("modules.grid.balance", "unknown_field") in problems(changed(MINIMAL, ("modules", "grid", "balance"), "x"))
    assert ("profile", "invalid_profile") in problems(changed(MINIMAL, ("profile",), "other"))


def test_module_options_come_from_fixed_lists():
    assert ("modules.grid.variant", "invalid_variant") in problems(changed(MINIMAL, ("modules", "grid", "variant"), "net"))
    assert ("modules.grid.points.power", "point_not_in_variant") in problems(
        changed(FULL, ("modules", "grid", "points", "power"), point("sensor.netz")))
    assert ("modules.wallbox.trust", "invalid_trust") in problems(changed(FULL, ("modules", "wallbox", "trust"), "always"))
    assert ("modules.wallbox.trust", "trust_needs_counter") in problems(
        changed(FULL, ("modules", "wallbox", "points", "energy_total"), ...))
    assert ("modules.generic_1.balance", "invalid_balance") in problems(
        changed(FULL, ("modules", "generic_1", "balance"), "part_of_grid"))
    assert ("modules.generic_1.points.storage_soc.freshness_from", "invalid_freshness_from") in problems(
        changed(FULL, ("modules", "generic_1", "points", "storage_soc", "freshness_from"), "energy_total"))
    assert ("modules.e3dc_rscp.pv", "invalid_rscp_pv") in problems(changed(FULL, ("modules", "e3dc_rscp"), {"pv": "battery"}))


def test_strings_are_limited_and_unique():
    seven = changed(FULL, ("modules", "pv_1", "strings"), [{"id": f"string_{i}", "name": "", "points": {}} for i in range(1, 8)])
    assert ("modules.pv_1.strings", "invalid_strings") in problems(seven)
    double = changed(FULL, ("modules", "pv_1", "strings"), [{"id": "string_1", "name": "a", "points": {}}] * 2)
    assert ("modules.pv_1.strings[1].id", "invalid_string_id") in problems(double)
    assert ("modules.pv_1.strings[0].points.soc", "unknown_point") in problems(
        changed(FULL, ("modules", "pv_1", "strings", 0, "points", "soc"), point("sensor.x", "%", "detail")))


def test_every_problem_is_reported_at_once():
    raw = changed(changed(MINIMAL, ("modules", "grid", "points", "power", "sign"), 3),
                  ("modules", "pv_1", "points", "power", "role"), "detail")
    assert problems(raw) == {("modules.grid.points.power.sign", "invalid_sign"),
                             ("modules.pv_1.points.power.role", "invalid_role")}      # invalid, not also "missing"


def test_store_starts_empty_and_activates_with_the_next_revision(tmp_path):
    store = PlantStore(tmp_path / "plant.json")
    assert store.current is None and store.revision == 0
    seen = []
    store.subscribe(seen.append)
    first = store.activate(MINIMAL, expected_revision=0)          # the revision in the candidate does not count
    assert first.revision == 1 and seen == [first]
    second = store.activate(FULL, expected_revision=1)
    assert second.revision == 2 and json.loads((tmp_path / "plant.json").read_text())["revision"] == 2
    reloaded = PlantStore(tmp_path / "plant.json")
    assert reloaded.revision == 2 and dump(reloaded.current) == {**FULL, "revision": 2}
    assert sorted(f.name for f in tmp_path.iterdir()) == [".plant.json.lock", "plant.json"]   # no temporary file left


def test_a_change_made_meanwhile_is_never_overwritten(tmp_path):
    store = PlantStore(tmp_path / "plant.json")
    store.activate(MINIMAL, expected_revision=0)
    before = (tmp_path / "plant.json").read_text()
    with pytest.raises(RevisionConflict) as info:
        store.activate(FULL, expected_revision=0)                 # editor started before the first activation
    assert (info.value.expected, info.value.current) == (0, 1)
    assert (tmp_path / "plant.json").read_text() == before and store.revision == 1


def test_an_invalid_candidate_or_a_failed_write_leaves_the_plant_in_place(tmp_path, monkeypatch):
    store = PlantStore(tmp_path / "plant.json")
    store.activate(MINIMAL, expected_revision=0)
    before = (tmp_path / "plant.json").read_text()
    with pytest.raises(PlantError):
        store.activate(changed(FULL, ("modules", "grid", "variant"), "net"), expected_revision=1)

    def broken(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", broken)
    with pytest.raises(OSError):
        store.activate(FULL, expected_revision=1)
    assert (tmp_path / "plant.json").read_text() == before and store.revision == 1
    assert sorted(f.name for f in tmp_path.iterdir()) == [".plant.json.lock", "plant.json"]


def test_two_processes_cannot_overwrite_each_other(tmp_path):
    """Hub and CLI each hold a store: the expected revision is compared with the file, not with what was loaded."""
    first, second = PlantStore(tmp_path / "plant.json"), PlantStore(tmp_path / "plant.json")
    first.activate(MINIMAL, expected_revision=0)
    with pytest.raises(RevisionConflict) as info:
        second.activate(FULL, expected_revision=0)                # second still believes there is no plant
    assert info.value.current == 1 and second.revision == 1       # it has caught up with the file
    assert dump(PlantStore(tmp_path / "plant.json").current)["modules"].keys() == MINIMAL["modules"].keys()
    assert second.activate(FULL, expected_revision=1).revision == 2


def test_activation_rules_see_candidate_and_current_and_can_refuse(tmp_path):
    seen = []

    def rule(candidate, current):
        seen.append((candidate.revision, current.revision if current else None))
        return [pl.Problem("modules.battery", "not_now", "abgelehnt")] if candidate.modules["battery"].enabled else []

    store = PlantStore(tmp_path / "plant.json", rules=(rule,))
    store.activate(MINIMAL, expected_revision=0)
    with pytest.raises(PlantError) as info:
        store.activate(FULL, expected_revision=1)
    assert info.value.codes == {"not_now"} and store.revision == 1 and seen == [(1, None), (2, 1)]


def test_a_failing_listener_does_not_report_a_failed_activation(tmp_path):
    store = PlantStore(tmp_path / "plant.json")
    called = []

    def broken(plant):
        raise RuntimeError("listener bug")

    store.subscribe(broken)
    store.subscribe(called.append)
    assert store.activate(MINIMAL, expected_revision=0).revision == 1
    assert len(called) == 1 and PlantStore(tmp_path / "plant.json").revision == 1


def test_values_that_only_hand_editing_produces_are_rejected_cleanly():
    assert ("modules.grid.points.power.entity", "invalid_entity") in problems(
        changed(MINIMAL, ("modules", "grid", "points", "power", "entity"), "sensor.netz_leistung\n"))
    for bad in ([], {}, 5, None):
        assert ("modules.e3dc_rscp.pv", "invalid_rscp_pv") in problems(changed(FULL, ("modules", "e3dc_rscp"), {"pv": bad}))


def test_split_grid_sensors_are_never_negative_by_definition():
    raw = changed(changed(FULL, ("modules", "grid", "points", "import_power", "nonnegative"), ...),
                  ("modules", "grid", "points", "export_power", "nonnegative"), ...)
    pts = parse(raw).points()
    assert pts["grid.import_power"].nonnegative and pts["grid.export_power"].nonnegative
    assert dump(parse(raw)) == FULL                               # the canonical form spells it out
    assert not parse(MINIMAL).points()["grid.power"].nonnegative  # one signed sensor: export is negative


def test_freshness_reference_of_a_string_point_stays_within_its_string():
    inside = changed(FULL, ("modules", "pv_1", "strings", 0, "points", "voltage", "freshness_from"), "power")
    assert parse(inside).points()["pv_1.string_1.voltage"].freshness_from == "power"
    outside = changed(FULL, ("modules", "pv_1", "strings", 0, "points", "voltage", "freshness_from"), "energy_total")
    assert ("modules.pv_1.strings[0].points.voltage.freshness_from", "invalid_freshness_from") in problems(outside)


def test_an_invalid_file_stops_the_start(tmp_path):
    (tmp_path / "plant.json").write_text(json.dumps(changed(MINIMAL, ("schema",), 99)))
    with pytest.raises(PlantError):
        PlantStore(tmp_path / "plant.json")


def test_installed_pv_power_is_optional_and_checked():
    """P5: the apps' gauges need a scale; the plant carries it (no constant of one plant in the apps)."""
    assert parse(MINIMAL).pv_peak_w is None and "pv_peak_w" not in dump(parse(MINIMAL))
    p = parse({**MINIMAL, "pv_peak_w": 9_800})
    assert p.pv_peak_w == 9800.0 and dump(p)["pv_peak_w"] == 9800.0 and parse(dump(p)) == p
    for bad in (0, -1, True, "9800", 20_000_000):
        with pytest.raises(PlantError) as e:
            parse({**MINIMAL, "pv_peak_w": bad})
        assert [(x.path, x.code) for x in e.value.problems] == [("pv_peak_w", "invalid_pv_peak")]
