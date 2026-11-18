"""P4 step 3: what the interface reads from Home Assistant – catalogue, sensor search, check of a draft against the
real states, suggestions from the energy dashboard, pairing code."""

import copy

from fastapi.testclient import TestClient

from solar_visor_hub.devices import DeviceStore
from solar_visor_hub.models import Unit
from solar_visor_hub.plant_samples import MINIMAL
from solar_visor_hub.ui import UiContext, UiSettings, create_ui_app, pairing_payload, Addresses
from solar_visor_hub.ui_ha import EntityIndex, catalog_view, check_points, search, suggestions

W = {"X-Solar-Visor": "1"}


def state(eid, value, unit=None, device_class=None, state_class=None, name=None):
    attrs = {k: v for k, v in (("unit_of_measurement", unit), ("device_class", device_class),
                               ("state_class", state_class), ("friendly_name", name)) if v is not None}
    return {"entity_id": eid, "state": value, "attributes": attrs}


STATES = [
    state("sensor.sample_pv_power", "1.2", "kW", "power", "measurement", "PV Leistung"),
    state("sensor.sample_grid_power", "-300", "W", "power", "measurement", "Netz Leistung"),
    state("sensor.pv_energy_today", "12.5", "kWh", "energy", "total_increasing", "PV heute"),
    state("sensor.grid_import_total", "1500", "kWh", "energy", "total_increasing", "Netzbezug"),
    state("sensor.grid_export_total", "800", "kWh", "energy", "total_increasing", "Einspeisung"),
    state("sensor.inverter_temperature", "45", "°C", "temperature", "measurement", "WR Temperatur"),
    state("sensor.broken_power", "unavailable", "W", "power", "measurement", "Kaputt"),
    state("sensor.old_power", "5", "W", "power", None, "Alt (deaktiviert)"),
    state("binary_sensor.car_plugged", "on", None, "plug", None, "Auto angesteckt"),
]
REGISTRY = {
    "config/entity_registry/list": [
        {"entity_id": "sensor.sample_pv_power", "device_id": "inv", "area_id": None},
        {"entity_id": "sensor.pv_energy_today", "device_id": "inv", "area_id": None},
        {"entity_id": "sensor.inverter_temperature", "device_id": "inv", "entity_category": "diagnostic"},
        {"entity_id": "sensor.old_power", "disabled_by": "user"},
    ],
    "config/device_registry/list": [{"id": "inv", "name": "Wechselrichter", "area_id": "roof"}],
    "config/area_registry/list": [{"area_id": "roof", "name": "Dach"}],
    "recorder/list_statistic_ids": [
        {"statistic_id": "sensor.sample_pv_power", "has_mean": True, "has_sum": False},
        {"statistic_id": "sensor.grid_import_total", "has_mean": False, "has_sum": True},
        {"statistic_id": "sensor.grid_export_total", "has_mean": False, "has_sum": True},
    ],
    "get_states": STATES,
    "energy/get_prefs": {"energy_sources": [
        {"type": "grid", "stat_energy_from": "sensor.grid_import_total", "stat_energy_to": "sensor.grid_export_total"},
        {"type": "solar", "stat_energy_from": "sensor.pv_energy_today"},
        {"type": "gas", "stat_energy_from": "sensor.gas"}]},
}


class HA:
    def __init__(self):
        self.calls = []

    async def read(self, message):
        self.calls.append(message["type"])
        return REGISTRY[message["type"]]


async def index():
    return await EntityIndex(HA().read).all()


def test_the_catalogue_comes_from_the_plant_model():
    c = catalog_view()
    grid = next(m for m in c["modules"] if m["id"] == "grid")
    assert grid["required"] == {"signed": ["power"], "split": ["import_power", "export_power"]} and grid["always_on"]
    pv2 = next(m for m in c["modules"] if m["id"] == "pv_2")
    assert pv2["points"]["energy_total"] == {"unit": "Wh", "roles": ["cross_check"], "counter": True, "nonnegative": False}
    assert not pv2["always_on"] and pv2["strings"] and c["generic_balances"] == ["part_of_home", "beside_home"]


async def test_search_filters_by_unit_and_words_and_ranks_the_likely_sensor_first():
    ents = await index()
    assert "sensor.old_power" not in ents                                           # disabled in the registry
    pv = ents["sensor.sample_pv_power"]
    assert (pv.device, pv.area, pv.statistic) == ("Wechselrichter", "Dach", "mean")
    hits, total = search(ents, "", Unit.W)
    assert total == 3 and hits[0].entity_id == "sensor.sample_pv_power"           # power class with a statistic first
    hits, total = search(ents, "dach", None)                                        # words also match device and area
    assert {h.entity_id for h in hits} == {"sensor.sample_pv_power", "sensor.pv_energy_today", "sensor.inverter_temperature"}
    assert search(ents, "netz bezug", Unit.WH)[1] == 1
    assert [h.entity_id for h in search(ents, "", Unit.BOOL)[0]] == ["binary_sensor.car_plugged"]
    assert search(ents, "", Unit.W, limit=1)[1] == 3


async def test_the_check_names_every_misassignment_on_a_draft():
    ents = await index()
    draft = copy.deepcopy(MINIMAL)
    draft["modules"]["pv_1"]["points"]["energy_total"] = {"entity": "sensor.pv_energy_today", "unit": "Wh", "role": "cross_check"}
    draft["modules"]["pv_2"] = {"enabled": True, "name": "Garage", "points": {
        "power": {"entity": "sensor.inverter_temperature", "unit": "W", "role": "authoritative"},
        "status": {"entity": "sensor.nowhere", "unit": "text", "role": "detail"}}}
    draft["modules"]["battery"] = {"enabled": True, "name": "Akku", "points": {
        "power": {"entity": "sensor.broken_power", "unit": "W", "role": "authoritative"}}}
    r = check_points(draft, ents)
    assert r["pv_1.power"]["status"] == "ok" and r["pv_1.power"]["value"] == 1200.0           # kW → W
    assert r["grid.power"]["value"] == -300.0 and r["grid.power"]["codes"] == ["no_statistics", "signed_balance_only"]
    assert r["pv_1.energy_total"]["codes"] == ["no_statistics"] and r["pv_1.energy_total"]["status"] == "warning"
    assert r["pv_2.power"]["status"] == "error" and "unit_mismatch" in r["pv_2.power"]["codes"]
    assert r["pv_2.status"]["codes"] == ["entity_missing"] and r["pv_2.status"]["sensor"] is None
    assert r["battery.power"]["value"] is None and "unavailable" in r["battery.power"]["codes"]
    flipped = copy.deepcopy(MINIMAL)
    flipped["modules"]["grid"]["points"]["power"]["sign"] = -1
    assert check_points(flipped, ents)["grid.power"]["value"] == 300.0                       # preview of the sign
    assert check_points({"schema": 1, "modules": {"pv_1": {"points": "x"}}}, ents) == {}       # broken drafts: no crash


async def test_suggestions_come_from_the_energy_dashboard_and_the_same_device():
    ha = HA()
    idx = EntityIndex(ha.read)
    ents = await idx.all()
    s = suggestions(REGISTRY["energy/get_prefs"], ents, idx.same_device)
    assert s["grid.import_energy"] == [{"entity_id": "sensor.grid_import_total", "source": "energy_dashboard"}]
    assert s["pv_1.energy_total"][0]["entity_id"] == "sensor.pv_energy_today"
    assert s["pv_1.power"] == [{"entity_id": "sensor.sample_pv_power", "source": "same_device"}]
    assert "pv_2.energy_total" not in s and "battery.power" not in s


async def test_registries_are_cached_and_states_refreshed():
    ha, now = HA(), [0.0]
    idx = EntityIndex(ha.read, now=lambda: now[0])
    await idx.all()
    await idx.all()
    assert ha.calls.count("config/entity_registry/list") == 1 and ha.calls.count("get_states") == 1
    now[0] = 20.0
    await idx.all()
    assert ha.calls.count("get_states") == 2 and ha.calls.count("config/entity_registry/list") == 1


def test_routes_and_pairing(tmp_path):
    devices = DeviceStore(tmp_path / "devices.sqlite3")
    settings = UiSettings(tmp_path / "ui.json")
    ctx = UiContext(mode="setup", running_revision=None, stored_revision=lambda: None, settings=settings,
                    web_dir=tmp_path / "noweb", read=HA().read, devices=devices)
    c = TestClient(create_ui_app(ctx))
    assert c.get("/api/catalog").json()["max_strings"] == 6
    found = c.get("/api/entities", params={"q": "netz", "unit": "Wh"}).json()
    assert found["total"] == 1 and found["entities"][0]["recorder_excluded"] is None        # unknown, never guessed
    assert c.get("/api/entities", params={"unit": "parsec"}).status_code == 422
    checked = c.post("/api/check", json={"plant": MINIMAL}, headers=W).json()["points"]
    assert checked["pv_1.power"]["value"] == 1200.0
    assert c.get("/api/suggestions").json()["points"]["grid.export_energy"][0]["entity_id"] == "sensor.grid_export_total"
    p = c.post("/api/pairing", json={"scope": "read"}, headers=W).json()
    assert len(p["code"]) == 8 and p["payload"] is None                             # no addresses yet
    settings.set_addresses(Addresses(read_url="https://solar.example.org", admin_url="http://192.168.1.1:8081"))
    p = c.post("/api/pairing", json={}, headers=W).json()
    assert p["scope"] == "admin" and p["payload"].startswith(
        "solarvisor://pair?v=1&read=https%3A%2F%2Fsolar.example.org&admin=http%3A%2F%2F192.168.1.1%3A8081&code="
        + p["code"] + "&invite=")                                                    # invitation: remote pairing
    token, _ = devices.enroll(p["code"], "iPhone")
    assert token and token not in p["payload"]
    assert pairing_payload(Addresses(read_url="https://a"), "1") is None


def test_every_problem_code_of_the_sources_is_listed_for_translation():
    import re
    from pathlib import Path

    from solar_visor_hub import plant, plant_legacy, ui_plant
    found = set()
    for module in (plant, plant_legacy, ui_plant):
        found |= set(re.findall(r'(?:check\.add\([^,]+, |Problem\([^,]*, )"([a-z_]+)"', Path(module.__file__).read_text()))
    assert found == set(ui_plant.PROBLEM_CODES)


def test_power_suggestions_prefer_the_sensor_with_the_meaning_of_the_point():
    """Grid and battery carry a sign: the transfer/net sensor of the device, not its import or charge sensor."""
    from solar_visor_hub.ui_ha import EntityInfo

    def e(eid, name):
        return EntityInfo(eid, name, "1", "W", "power", "measurement", "S10", None, "mean", False, None)
    ents = {x.entity_id: x for x in (
        e("sensor.s10_consumption_from_grid", "Consumption from grid"), e("sensor.s10_export_to_grid", "Export to grid"),
        e("sensor.s10_transfer_to_from_grid", "Transfer to/from grid"), e("sensor.s10_battery_charge", "Battery charge"),
        e("sensor.s10_battery_net_change", "Battery net change"), e("sensor.s10_solar_production", "Solar production"),
        e("sensor.s10_additional_production", "Additional production"))}
    ents["sensor.s10_grid_today"] = EntityInfo("sensor.s10_grid_today", "Grid today", "1", "kWh", "energy", "total_increasing",
                                               "S10", None, "sum", False, None)
    prefs = {"energy_sources": [{"type": "grid", "stat_energy_from": "sensor.s10_grid_today"},
                                {"type": "solar", "stat_energy_from": "sensor.s10_grid_today"},
                                {"type": "battery", "stat_energy_from": "sensor.s10_grid_today"}]}
    s = suggestions(prefs, ents, lambda counter: [x for x in ents if x != counter])
    assert s["grid.power"][0]["entity_id"] == "sensor.s10_transfer_to_from_grid"
    assert s["battery.power"][0]["entity_id"] == "sensor.s10_battery_net_change"
    assert s["pv_1.power"][0]["entity_id"] == "sensor.s10_solar_production"
