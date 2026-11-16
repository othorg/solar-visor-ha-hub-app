"""Dashboard card routes over Ingress (plan lovelace-card P1): every active Home Assistant user reads them, GET only;
everything else stays for administrators; the routes exist on the Ingress listener only."""

from fastapi.testclient import TestClient

from solar_visor_hub.plant import parse
from solar_visor_hub.plant_samples import MINIMAL, THREE_PV, sample_live
from solar_visor_hub.ui import INGRESS_PEER, IngressGuard, UiContext, UiSettings, UserList, create_ui_app
from solar_visor_hub.ui_card import CardContext
from solar_visor_hub.ui_card import card_sensors as _card_sensors

from test_ui import ADMIN, USER, Peer, Recorder, h


def card_client(tmp_path, raw=THREE_PV, values=None, ha=None, live=True, built=True):
    web = tmp_path / "web"
    if built:
        (web / "card").mkdir(parents=True)
        (web / "card" / "card.js").write_text("export function create() {}")
    plant_live = sample_live(raw, values or {"pv_1.power": "4100", "battery.power": "900", "battery.soc": "71"})
    card = CardContext(plant=lambda: plant_live.plant if live else None,
                       snapshot=plant_live.snapshot if live else None,
                       timezone=lambda: "Europe/Berlin", build=lambda: "0.1.0.190 (abc1234)")
    ctx = UiContext(mode="plant", running_revision=4, stored_revision=lambda: 4, settings=UiSettings(tmp_path / "ui.json"),
                    web_dir=web, card=card)
    return TestClient(Peer(IngressGuard(create_ui_app(ctx), UserList((ha or Recorder()).read)), INGRESS_PEER)), plant_live


def test_every_active_user_reads_the_card_and_only_the_card(tmp_path):
    c, _ = card_client(tmp_path)
    for user in (ADMIN, USER, "own"):
        for path in ("/card/v1/plant", "/card/v1/snapshot", "/card/v1/card.js"):
            assert c.get(path, headers=h(user)).status_code == 200, (user, path)
    for user in ("gone", "sys", "stranger"):                               # deactivated, system, unknown
        assert c.get("/card/v1/plant", headers=h(user)).status_code == 403, user
    assert c.get("/card/v1/plant").status_code == 401
    # the rest of the interface stays for administrators
    assert c.get("/api/session", headers=h(USER)).status_code == 403
    assert c.get("/api/plant", headers=h(USER)).status_code == 403
    assert c.get("/assets/app.js", headers=h(USER)).status_code == 403
    # a path that only starts like the card's reaches nothing else
    for sneaky in ("/card/v1/../api/session", "/card/v1/%2e%2e/api/session", "/card/v1/..%2fapi/session"):
        assert c.get(sneaky, headers=h(USER)).status_code in (403, 404), sneaky
    # the card only reads – not even an administrator writes through it
    for method in ("post", "put", "delete", "patch"):
        r = getattr(c, method)("/card/v1/plant", headers=h(ADMIN, write=True))
        assert r.status_code == 405, method


def test_the_panel_explains_itself_to_users_who_are_no_administrators(tmp_path):
    c, _ = card_client(tmp_path)
    r = c.get("/", headers=h(USER))
    assert r.status_code == 403 and r.headers["content-type"].startswith("text/html")
    assert "custom:solar-visor-card" in r.text and r.headers["cache-control"] == "no-store"
    assert c.get("/api/session", headers=h(USER)).headers["content-type"] == "application/json"


def test_without_the_user_list_nobody_reads_the_card(tmp_path):
    c, _ = card_client(tmp_path, ha=Recorder(fail=True))
    assert c.get("/card/v1/snapshot", headers=h(USER)).status_code == 503


def test_the_plant_names_the_sensors_of_each_node(tmp_path):
    c, live = card_client(tmp_path)
    r = c.get("/card/v1/plant", headers=h(USER))
    body = r.json()
    assert body["build"] == "0.1.0.190 (abc1234)" and r.headers["cache-control"] == "no-store"
    assert body["capabilities"]["plant_revision"] == 4 and body["capabilities"]["timezone"] == "Europe/Berlin"
    entities = {node: [s["entity_id"] for s in sensors] for node, sensors in body["sensors"].items()}
    assert entities == {
        "grid": ["sensor.sample_import_power", "sensor.sample_export_power"],                 # no counters
        "pv_1": ["sensor.sample_pv_sued_s1_power", "sensor.sample_pv_sued_power"],            # strings first
        "pv_2": ["sensor.sample_pv_garage_power"],
        "pv_3": ["sensor.sample_pv_balkon_power"],
        "battery": ["sensor.sample_battery_soc", "sensor.sample_battery_power"],
        "generic_1": ["sensor.sample_heatpump_power"],
        "generic_2": ["sensor.sample_garden_soc", "sensor.sample_garden_power"],
        "generation": ["sensor.sample_pv_sued_power", "sensor.sample_pv_garage_power", "sensor.sample_pv_balkon_power"],
    }                                                       # switched-off wallbox absent, home calculated: none
    assert body["sensors"]["pv_1"][0]["metric_id"] == "pv_1.string_1.power"


def test_the_snapshot_is_the_read_apis_with_304(tmp_path):
    c, _ = card_client(tmp_path)
    first = c.get("/card/v1/snapshot", headers=h(USER))
    assert first.status_code == 200 and first.json()["metrics"]["battery.soc"]["value"] == 71.0
    assert first.headers["cache-control"] == "no-store"
    again = c.get("/card/v1/snapshot", headers={**h(USER), "If-None-Match": first.headers["ETag"]})
    assert again.status_code == 304


def test_the_renderer_is_javascript_and_revalidated(tmp_path):
    c, _ = card_client(tmp_path)
    r = c.get("/card/v1/card.js?v=0.1.0.190%20(abc1234)", headers=h(USER))
    assert r.headers["content-type"].startswith("text/javascript") and r.headers["cache-control"] == "no-cache"
    c2, _ = card_client(tmp_path / "unbuilt", built=False)
    assert c2.get("/card/v1/card.js", headers=h(USER)).status_code == 404


def test_without_a_plant_the_card_hears_not_configured(tmp_path):
    c, _ = card_client(tmp_path, live=False)
    for path in ("/card/v1/plant", "/card/v1/snapshot"):
        r = c.get(path, headers=h(USER))
        assert r.status_code == 503 and r.json() == {"detail": "plant_not_configured"}


def card_sensors(plant):
    return {node: [s.model_dump() for s in sensors] for node, sensors in _card_sensors(plant).items()}


def test_a_minimal_plant_and_a_measured_consumption():
    assert card_sensors(parse(MINIMAL)) == {
        "grid": [{"entity_id": "sensor.sample_grid_power", "metric_id": "grid.power"}],
        "pv_1": [{"entity_id": "sensor.sample_pv_power", "metric_id": "pv_1.power"}],
        "generation": [{"entity_id": "sensor.sample_pv_power", "metric_id": "pv_1.power"}],
    }
    raw = {**MINIMAL, "modules": {**MINIMAL["modules"], "consumption": {"points": {
        "power": {"entity": "sensor.sample_house_total", "unit": "W", "sign": 1, "role": "authoritative",
                  "valid_from": None}}}}}
    assert card_sensors(parse(raw))["consumption"] == [{"entity_id": "sensor.sample_house_total",
                                                        "metric_id": "consumption.power"}]


def test_no_card_route_on_port_a_or_b():
    from solar_visor_hub.app import create_admin_app, create_public_app
    for app in (create_public_app(), create_admin_app()):
        assert not any(getattr(r, "path", "").startswith("/card") for r in app.routes)
