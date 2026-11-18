"""P4 step 5: overview, history, devices and diagnostics of the web interface – reusing what port B and the command line
already do (devices, tests, InfluxDB token, E3/DC credentials write-only, archive recomputation)."""

import json
from datetime import UTC, date, datetime, timedelta

from fastapi.testclient import TestClient

from solar_visor_hub.config import SecretFiles
from solar_visor_hub.devices import DeviceStore
from solar_visor_hub.plant_samples import MINIMAL, sample_live
from solar_visor_hub.ui import Addresses, UiContext, UiSettings, create_ui_app
from solar_visor_hub.influx_config import InfluxConfig, InfluxConfigStore, InfluxConnection
from solar_visor_hub.ui_admin import AdminContext
from solar_visor_hub.ui_plant import PlantEditing

W = {"X-Solar-Visor": "1"}
NOW = datetime(2026, 11, 15, 11, 0, tzinfo=UTC)


class Archive:
    def __init__(self):
        self.marked = []

    def summary(self):
        return {"raw_durable": {"days": 13, "first": "2026-11-02", "last": "2026-11-14"}}

    def mark_stale(self, days):
        self.marked += days
        return len(days)


class History:
    window_start = datetime(2026, 11, 1, 3, 15, tzinfo=UTC)

    def __init__(self):
        self.archive = Archive()


def hub(tmp_path, mode="plant", history=None, influx=None, now=None):
    devices = DeviceStore(tmp_path / "devices.sqlite3")
    secrets = SecretFiles(ha_token=tmp_path / "secrets" / "ha", influx_token=tmp_path / "secrets" / "influx")
    live = sample_live(MINIMAL, {"grid.power": "-500", "pv_1.power": "1200"})

    async def ok():
        return "ok", "reachable"

    admin = AdminContext(mode=mode, snapshot=lambda: live.snapshot(now or NOW), ha_connected=lambda: True,
                         module_names=lambda: {"pv_1": "Dach"},
                         timezone=lambda: "Europe/Berlin", plant_history=history, devices=devices, test_ha=ok,
                         secrets=secrets, influx=influx)
    plant_path = tmp_path / "plant.json"
    ed = PlantEditing(plant_path, mode, lambda: None, lambda: None)
    settings = UiSettings(tmp_path / "ui.json")
    ctx = UiContext(mode=mode, running_revision=None, stored_revision=lambda: None, settings=settings,
                    web_dir=tmp_path / "noweb", editing=ed, devices=devices, admin=admin)
    return TestClient(create_ui_app(ctx)), devices, settings, plant_path


def test_overview_shows_connection_live_values_and_the_balance(tmp_path):
    c, *_ = hub(tmp_path)
    o = c.get("/api/overview").json()
    assert o["ha_connected"] and o["timezone"] == "Europe/Berlin"
    live = {m["metric_id"]: m for m in o["live"]}
    assert live["grid.power"]["value"] == -500.0 and live["pv_1.power"]["value"] == 1200.0
    assert live["pv_1.power"]["name"] == "Dach" and live["grid.power"]["name"] is None
    assert all(m.count(".") == 1 for m in live)                                    # no strings, no detail powers
    assert live["consumption.power"]["value"] == 700.0 and live["consumption.power"]["state"] == "ok"
    assert "balance_residual_w" in o and o["not_ok"] == [] and o["problems"] == []


def test_overview_names_the_values_without_a_valid_state_and_since_when(tmp_path):
    """User decision 2026-11-17: the interface says which value has no valid state, why and since when (the sample
    values were reported 3 s before the samples' time; ten minutes later they are too old)."""
    from solar_visor_hub.plant_samples import T
    c, *_ = hub(tmp_path, now=T + timedelta(minutes=10))
    o = c.get("/api/overview").json()
    p = {x["metric_id"]: x for x in o["problems"]}
    assert p["pv_1.power"] == {"metric_id": "pv_1.power", "name": "Dach", "state": "stale", "reasons": ["report_too_old"],
                               "since": (T - timedelta(seconds=3)).isoformat()}
    assert p["consumption.power"]["since"] is None                  # calculated: no source of its own
    assert o["not_ok"] == [x["metric_id"] for x in o["problems"]]


def test_history_window_archive_and_recomputation(tmp_path):
    h = History()
    c, *_ = hub(tmp_path, history=h)
    v = c.get("/api/history").json()
    assert v["source"] == "recorder" and v["archive"]["raw_durable"]["days"] == 13 and v["window_start"].startswith("2026-11-01")
    r = c.post("/api/history/recompute", json={"from": "2026-11-10", "to": "2026-11-12"}, headers=W)
    assert r.json() == {"days": 3} and h.archive.marked == [date(2026, 11, 10), date(2026, 11, 11), date(2026, 11, 12)]
    future = (datetime.now(UTC) + timedelta(days=2)).date().isoformat()
    assert c.post("/api/history/recompute", json={"from": "2026-11-12", "to": future}, headers=W).status_code == 422
    assert c.post("/api/history/recompute", json={"from": "2026-11-12", "to": "2026-11-10"}, headers=W).status_code == 422
    legacy, *_ = hub(tmp_path / "l", mode="plant_legacy")
    assert legacy.get("/api/history").json()["source"] == "existing_calculation"
    assert legacy.post("/api/history/recompute", json={"from": "2026-11-10", "to": "2026-11-10"}, headers=W).status_code == 409


def test_devices_are_listed_and_revoked(tmp_path):
    c, devices, *_ = hub(tmp_path)
    token, _ = devices.enroll(devices.create_pairing_code("read"), "iPhone")
    listed = c.get("/api/devices").json()
    assert [d["name"] for d in listed] == ["iPhone"] and "token" not in json.dumps(listed)
    assert c.delete(f"/api/devices/{listed[0]['id']}", headers=W).status_code == 204
    assert c.get("/api/devices").json() == [] and devices.authenticate(token) is None
    assert c.delete("/api/devices/999", headers=W).status_code == 404


def test_pairing_carries_a_qr_code_of_the_text(tmp_path):
    c, devices, settings, _ = hub(tmp_path)
    settings.set_addresses(Addresses(read_url="http://192.168.1.1:8080", admin_url="http://192.168.1.1:8081"))
    p = c.post("/api/pairing", json={"scope": "admin"}, headers=W).json()
    assert p["qr_svg"].startswith("<svg") and p["code"] not in p["qr_svg"]        # an image, not the text
    assert devices.enroll(p["code"], "Mac")[0]


def test_tests_secrets_and_rscp(tmp_path):
    c, *_ = hub(tmp_path)
    assert c.post("/api/test/ha", headers=W).json() == {"status": "ok", "detail": "reachable"}
    assert c.post("/api/test/influx", headers=W).status_code == 503                 # not configured here
    assert c.get("/api/secrets").json() == {"influx_token": False}
    assert c.put("/api/secrets/influx", json={"value": "abc def"}, headers=W).status_code == 422   # no whitespace
    assert c.put("/api/secrets/influx", json={"value": "s3cr3t"}, headers=W).status_code == 204
    assert c.get("/api/secrets").json() == {"influx_token": True}
    stored = tmp_path / "secrets" / "influx"
    assert stored.read_text().strip() == "s3cr3t" and oct(stored.stat().st_mode & 0o777) == "0o600"
    assert c.get("/api/rscp").status_code == 503                                     # no E3/DC set up in this hub


def test_influx_connection_is_set_in_the_interface(tmp_path):
    running = InfluxConfig("http://192.168.1.5:8086", "solar", "autogen")
    store = InfluxConfigStore(tmp_path / "influx.json")
    files = SecretFiles(ha_token=tmp_path / "secrets" / "ha", influx_token=tmp_path / "secrets" / "influx")
    conn = InfluxConnection(store, running, files.influx_token, files.read_influx_token)
    c, *_ = hub(tmp_path, mode="plant_legacy", influx=conn)
    view = c.get("/api/influx").json()
    assert view == {"url": "http://192.168.1.5:8086", "database": "solar", "retention_policy": "autogen",
                    "token_set": False}
    bad = c.put("/api/influx", json={"url": "influx:8086", "database": "x"}, headers=W)
    assert bad.status_code == 422 and bad.json()["detail"]["code"] == "invalid_influx" and store.current is None
    ok = c.put("/api/influx", json={"url": "http://192.168.1.5:8086", "database": "ha", "retention_policy": ""}, headers=W)
    assert ok.status_code == 200 and ok.json()["retention_policy"] is None and ok.json()["database"] == "ha"
    assert store.current == InfluxConfig("http://192.168.1.5:8086", "ha", None)
    # a stored token never goes to another address unless it is entered again (as for the E3/DC credentials)
    (tmp_path / "secrets").mkdir(exist_ok=True)
    (tmp_path / "secrets" / "influx").write_text("t0k")
    moved = c.put("/api/influx", json={"url": "http://elsewhere:8086", "database": "solar"}, headers=W)
    assert moved.status_code == 422 and moved.json()["detail"]["code"] == "token_required"
    assert store.current.url == "http://192.168.1.5:8086"                           # nothing written
    assert c.put("/api/influx", json={"url": "http://192.168.1.5:8086/", "database": "other"},
                 headers=W).status_code == 200                                      # same address: no token needed
    ok2 = c.put("/api/influx", json={"url": "http://elsewhere:8086", "database": "x", "token": "n3w"}, headers=W)
    assert ok2.status_code == 200 and (tmp_path / "secrets" / "influx").read_text().strip() == "n3w"
    assert c.put("/api/influx", json={"url": "http://elsewhere:8086", "database": "x", "token": "a b"},
                 headers=W).json()["detail"]["code"] == "invalid_secret"
    standalone, *_ = hub(tmp_path / "s")
    assert standalone.get("/api/influx").status_code == 503


def test_export_and_import_go_through_the_draft(tmp_path):
    c, _, _, plant_path = hub(tmp_path)
    assert c.get("/api/plant/export").status_code == 404
    plant_path.write_text(json.dumps({**MINIMAL, "revision": 3}))
    r = c.get("/api/plant/export")
    assert r.json()["revision"] == 3 and "attachment" in r.headers["content-disposition"]
    imported = c.post("/api/plant/import", json={"plant": r.json(), "draft_id": None}, headers=W).json()
    assert imported["problems"] == []
    draft = c.get("/api/plant").json()["draft"]
    assert draft["id"] == imported["draft_id"] and "revision" not in draft["plant"]
    assert json.loads(plant_path.read_text())["revision"] == 3                       # nothing activated
