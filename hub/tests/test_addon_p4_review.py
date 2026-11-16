"""Findings of the Codex review of P4 (2026-11-15): repair of an unreadable plant, retroactive changes against old
revisions, power history across the override day, restore withdraws overrides, no half-written activation, a running
computation cannot undo an invalidation, import needs a plant."""

import copy
import json
from datetime import UTC, date, datetime, timedelta

from fastapi.testclient import TestClient

from solar_visor_hub.periods import period_bounds, zone
from solar_visor_hub.plant import PlantStore, parse
from solar_visor_hub.plant_archive import PlantArchive
from solar_visor_hub.plant_samples import MINIMAL
from solar_visor_hub.ui import UiContext, UiSettings, create_ui_app
from solar_visor_hub.ui_plant import PlantEditing

from test_plant_history import NOW, Recorder, service

W = {"X-Solar-Visor": "1"}
TZ = zone("Europe/Berlin")


def plant(pv="sensor.sample_pv_power", grid="sensor.sample_grid_power", revision=1):
    p = copy.deepcopy(MINIMAL)
    p["revision"] = revision
    p["modules"]["pv_1"]["points"]["power"]["entity"] = pv
    p["modules"]["grid"]["points"]["power"]["entity"] = grid
    return p


def client(tmp_path, mode="plant", restarts=None):
    sink = restarts if restarts is not None else []
    ed = PlantEditing(tmp_path / "plant.json", mode, lambda: None, lambda: sink.append(1),
                      archive_path=tmp_path / "archive.sqlite3", timezone=lambda: "Europe/Berlin")
    ctx = UiContext(mode=mode, running_revision=None, stored_revision=lambda: None, settings=UiSettings(tmp_path / "ui.json"),
                    web_dir=tmp_path / "noweb", editing=ed)
    return TestClient(create_ui_app(ctx))


def activate(c, candidate, revision, retro=()):
    view = c.get("/api/plant").json()
    draft = c.put("/api/plant/draft", json={"plant": candidate, "draft_id": view["draft"]["id"] if view["draft"] else None},
                  headers=W).json()["draft_id"]
    return c.post("/api/plant/activate", json={"active_revision": revision, "draft_id": draft, "retroactive": list(retro)},
                  headers=W)


def test_an_unreadable_plant_is_repaired_from_the_interface(tmp_path):
    """M1: the setup mode it causes must not be a dead end – the broken file is kept, the repair gets a new number."""
    PlantArchive(tmp_path / "archive.sqlite3").record_plant(parse(plant(revision=4)), NOW)
    (tmp_path / "plant.json").write_text('{"schema": 1, "revision": 3, "modules": "broken"')
    c = client(tmp_path, mode="setup")
    view = c.get("/api/plant").json()
    assert view["active"] is None and view["active_revision"] == 4                # above what the archive knows
    r = activate(c, plant(), 4)
    assert r.status_code == 200 and r.json()["revision"] == 5
    assert (tmp_path / "plant.broken.json").read_text().startswith('{"schema": 1, "revision": 3')
    assert PlantStore(tmp_path / "plant.json").current.revision == 5


def test_a_retroactive_change_that_would_break_an_old_revision_is_refused_and_never_breaks_the_history(tmp_path):
    """M2: in revision 1 the grid used the sensor the PV now takes over – counted twice there."""
    a = PlantArchive(tmp_path / "archive.sqlite3")
    a.record_plant(parse(plant(grid="sensor.shared", revision=1)), NOW - timedelta(days=20))
    a.record_plant(parse(plant(revision=2)), NOW - timedelta(days=10))
    (tmp_path / "plant.json").write_text(json.dumps(plant(revision=2)))
    c = client(tmp_path)
    day = (datetime.now(UTC) - timedelta(days=30)).date().isoformat()
    r = activate(c, plant(pv="sensor.shared", revision=2), 2, [{"metric_id": "pv_1.power", "from": day}])
    assert r.status_code == 422 and r.json()["detail"]["problems"][0]["code"] == "retroactive_invalid"
    # an override that is invalid for some revision anyway is skipped there, the history goes on
    a.add_override("pv_1.power", {"entity": "sensor.shared", "unit": "W", "sign": 1, "role": "authoritative"},
                   NOW - timedelta(days=40), NOW)
    old = a.plant_at(NOW - timedelta(days=15), parse(plant(revision=2)))
    assert old.points()["pv_1.power"].entity == "sensor.sample_pv_power"           # skipped, logged


async def test_the_power_history_follows_the_override_day_within_a_revision(tmp_path):
    """M3: one object per distinct plant, not per revision."""
    rec, now = Recorder(), [NOW]
    hist = service(tmp_path, rec, now)
    x = date(2026, 11, 11)
    hist.archive.add_override("pv_1.power", {"entity": "sensor.other_pv", "unit": "W", "sign": 1, "role": "authoritative"},
                              period_bounds("day", x, TZ)[0], NOW + timedelta(minutes=1))
    revs = await hist.revisions(period_bounds("day", date(2026, 11, 10), TZ)[0], period_bounds("day", date(2026, 11, 12), TZ)[0],
                                hist.plant)
    entities = [p.points()["pv_1.power"].entity for _, p in sorted(revs.items())]
    assert entities == ["sensor.sample_pv_power", "sensor.other_pv"]


def test_restoring_withdraws_the_retroactive_changes_of_the_plant_replaced(tmp_path):
    """M6: back to how it was – also for the days from X on."""
    restarts = []
    a = PlantArchive(tmp_path / "archive.sqlite3")
    a.record_plant(parse(plant(revision=1)), NOW - timedelta(days=30))
    (tmp_path / "plant.json").write_text(json.dumps(plant(revision=1)))
    c = client(tmp_path, restarts=restarts)
    x = (datetime.now(TZ) - timedelta(days=3)).date()
    assert activate(c, plant(pv="sensor.other_pv"), 1, [{"metric_id": "pv_1.power", "from": x.isoformat()}]).status_code == 200
    t = period_bounds("day", x, TZ)[0] + timedelta(hours=12)
    assert a.plant_at(t, parse(plant(revision=1))).points()["pv_1.power"].entity == "sensor.other_pv"
    assert c.post("/api/plant/restore", json={"active_revision": 2}, headers=W).json()["revision"] == 3
    assert a.plant_at(t, parse(plant(revision=1))).points()["pv_1.power"].entity == "sensor.sample_pv_power"
    assert restarts == [1, 1]


def test_a_plant_that_cannot_be_written_leaves_no_overrides_behind(tmp_path, monkeypatch):
    """N3: overrides are written under the plant lock before the plant; a failing plant write withdraws them."""
    a = PlantArchive(tmp_path / "archive.sqlite3")
    a.record_plant(parse(plant(revision=1)), NOW - timedelta(days=30))
    (tmp_path / "plant.json").write_text(json.dumps(plant(revision=1)))
    real = PlantStore._write

    def failing(self, data, target=None):
        if target is None:
            raise OSError("disk full")
        return real(self, data, target)
    monkeypatch.setattr(PlantStore, "_write", failing)
    c = client(tmp_path)
    x = (datetime.now(TZ) - timedelta(days=3)).date().isoformat()
    try:
        activate(c, plant(pv="sensor.other_pv"), 1, [{"metric_id": "pv_1.power", "from": x}])
    except OSError:
        pass
    t = period_bounds("day", date.fromisoformat(x), TZ)[0] + timedelta(hours=12)
    assert a.plant_at(t, parse(plant(revision=1))).points()["pv_1.power"].entity == "sensor.sample_pv_power"
    assert json.loads((tmp_path / "plant.json").read_text())["revision"] == 1


async def test_a_computation_begun_before_an_override_stores_its_day_as_stale(tmp_path):
    """N4: the day it wrote must not keep the old sensor for good."""
    rec, now = Recorder(), [NOW]
    hist = service(tmp_path, rec, now)
    gen = hist.archive.override_gen()
    day = await hist.day(date(2026, 11, 9))
    hist.archive.add_override("pv_1.power", {"entity": "sensor.other_pv", "unit": "W", "sign": 1, "role": "authoritative"},
                              period_bounds("day", date(2026, 11, 1), TZ)[0], NOW + timedelta(minutes=1))
    hist.archive.put(date(2026, 11, 9), "Europe/Berlin", day, NOW, True, hist.plant, gen)     # the late writer
    assert hist.archive.get(date(2026, 11, 9)) is None                              # stale: computed anew next time


def test_an_import_has_to_be_a_plant(tmp_path):
    """M4: a draft the interface could not show is refused."""
    c = client(tmp_path, mode="setup")
    r = c.post("/api/plant/import", json={"plant": {"schema": 1}, "draft_id": None}, headers=W)
    assert r.status_code == 422 and c.get("/api/plant").json()["draft"] is None
