"""Sensor changed "retroactively from day X" (user decision 2026-11-15, P4): a source override in the archive – old
days before X keep the old sensor, days from X read the new one, a later change "from now on" stays untouched."""

import copy
import json
from datetime import UTC, date, datetime, timedelta

from fastapi.testclient import TestClient

from solar_visor_hub.periods import period_bounds, zone
from solar_visor_hub.plant import parse
from solar_visor_hub.plant_archive import PlantArchive
from solar_visor_hub.plant_samples import MINIMAL
from solar_visor_hub.ui import UiContext, UiSettings, create_ui_app
from solar_visor_hub.ui_plant import PlantEditing

from test_plant_history import NOW, PV, Recorder, service, wh

TZ = zone("Europe/Berlin")
OTHER = "sensor.other_pv"


def pv_entity(plant):
    return plant.points()["pv_1.power"].entity


def with_pv(entity, revision):
    p = copy.deepcopy(MINIMAL)
    p["revision"] = revision
    p["modules"]["pv_1"]["points"]["power"]["entity"] = entity
    return parse(p)


def t(day, hour=0):
    return datetime(2026, 11, day, hour, tzinfo=UTC)


def test_an_override_reaches_back_only_to_revisions_seen_before_it(tmp_path):
    a = PlantArchive(tmp_path / "archive.sqlite3")
    a.record_plant(with_pv(PV, 1), t(5))
    point = {"entity": OTHER, "unit": "W", "sign": 1, "role": "authoritative", "valid_from": None}
    a.add_override("pv_1.power", point, effective_from=t(3), created_at=t(10))
    a.record_plant(with_pv(OTHER, 2), t(10, 1))
    a.record_plant(with_pv("sensor.third_pv", 3), t(12))
    current = with_pv("sensor.third_pv", 3)
    assert pv_entity(a.plant_at(t(2), current)) == PV            # before X: the old sensor
    assert pv_entity(a.plant_at(t(4), current)) == OTHER         # before the first revision, from X on: the new one
    assert pv_entity(a.plant_at(t(7), current)) == OTHER         # revision 1 seen before the override
    assert pv_entity(a.plant_at(t(11), current)) == OTHER        # revision 2 has it of its own
    assert pv_entity(a.plant_at(t(13), current)) == "sensor.third_pv"   # a later change "from now on": untouched


async def test_days_from_x_are_computed_anew_with_the_new_sensor(tmp_path):
    rec, now = Recorder(pv_w=1000.0, other_w=500.0), [NOW]
    hist = service(tmp_path, rec, now)
    before, from_x = date(2026, 11, 9), date(2026, 11, 10)
    assert wh(await hist.day(before), "pv_1") == 24_000.0 and wh(await hist.day(from_x), "pv_1") == 24_000.0
    assert hist.archive.get(from_x).durable                       # frozen – only an explicit change recomputes it
    hist.archive.add_override("pv_1.power", {"entity": OTHER, "unit": "W", "sign": 1, "role": "authoritative"},
                              period_bounds("day", from_x, TZ)[0], NOW + timedelta(minutes=1))   # after the start
    assert hist.archive.mark_stale_from(from_x) >= 1
    assert wh(await hist.day(before), "pv_1") == 24_000.0                       # untouched
    assert wh(await hist.day(from_x), "pv_1") == 12_000.0                       # read from the new sensor


def test_activation_records_the_override_and_refuses_what_cannot_be_retroactive(tmp_path):
    plant_path, archive_path = tmp_path / "plant.json", tmp_path / "archive.sqlite3"
    PlantArchive(archive_path)
    plant_path.write_text(json.dumps({**MINIMAL, "revision": 1}))
    restarts = []

    def client(mode):
        ed = PlantEditing(plant_path, mode, lambda: 1, lambda: restarts.append(1),
                          archive_path=archive_path if mode == "plant" else None, timezone=lambda: "Europe/Berlin")
        ctx = UiContext(mode=mode, running_revision=1, stored_revision=lambda: 1, settings=UiSettings(tmp_path / "ui.json"),
                        web_dir=tmp_path / "noweb", editing=ed)
        return TestClient(create_ui_app(ctx))

    changed = copy.deepcopy(MINIMAL)
    changed["modules"]["pv_1"]["points"]["power"]["entity"] = OTHER
    yesterday = (datetime.now(UTC) - timedelta(days=1)).date()
    W = {"X-Solar-Visor": "1"}

    def activate(c, plant, retro, revision=1):
        draft = c.put("/api/plant/draft", json={"plant": plant, "draft_id": None}, headers=W).json()["draft_id"]
        r = c.post("/api/plant/activate", json={"active_revision": revision, "draft_id": draft, "retroactive": retro},
                   headers=W)
        if r.status_code != 200:
            c.delete("/api/plant/draft", params={"draft_id": draft}, headers=W)
        return r

    c = client("plant")
    unchanged = activate(c, MINIMAL, [{"metric_id": "grid.power", "from": "2026-11-01"}])
    assert unchanged.status_code == 422 and unchanged.json()["detail"]["problems"][0]["code"] == "retroactive_invalid"
    future = activate(c, changed, [{"metric_id": "pv_1.power", "from": (datetime.now(UTC) + timedelta(days=2)).date().isoformat()}])
    assert future.status_code == 422
    legacy = activate(client("plant_legacy"), changed, [{"metric_id": "pv_1.power", "from": "2026-11-01"}])
    assert legacy.status_code == 422 and json.loads(plant_path.read_text())["revision"] == 1
    ok = activate(c, changed, [{"metric_id": "pv_1.power", "from": yesterday.isoformat()}])
    assert ok.status_code == 200 and ok.json()["revision"] == 2 and restarts == [1]
    a = PlantArchive(archive_path)
    a.record_plant(parse({**MINIMAL, "revision": 1}), datetime.now(UTC) - timedelta(days=30))
    start_x = period_bounds("day", yesterday, zone("Europe/Berlin"))[0]
    assert pv_entity(a.plant_at(start_x, parse(changed))) == OTHER
    assert pv_entity(a.plant_at(start_x - timedelta(hours=1), parse(changed))) == "sensor.sample_pv_power"
