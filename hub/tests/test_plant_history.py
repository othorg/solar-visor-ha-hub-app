"""History service and archive of a plant without the existing calculation (P3 step 4)."""

import asyncio
import copy
from datetime import UTC, date, datetime, timedelta

import pytest

from solar_visor_hub import backup
from solar_visor_hub.periods import period_bounds
from solar_visor_hub.plant import parse
from solar_visor_hub.plant_archive import PlantArchive
from solar_visor_hub.plant_daycalc import TIER_HOUR, TIER_RAW
from solar_visor_hub.plant_history import PlantHistory
from solar_visor_hub.plant_samples import MINIMAL

PV, GRID = "sensor.sample_pv_power", "sensor.sample_grid_power"
NOW = datetime(2026, 11, 15, 11, 0, tzinfo=UTC)          # 12:00 Berlin
WINDOW = datetime(2026, 11, 1, 3, 15, tzinfo=UTC)       # oldest 5-minute statistic, as measured


class Recorder:
    def __init__(self, pv_w=1000.0, grid_w=200.0, other_w=500.0):
        self.pv_w, self.grid_w, self.other_w, self.calls = pv_w, grid_w, other_w, []

    async def read(self, msg):
        self.calls.append(msg["type"])
        if msg["type"] == "recorder/list_statistic_ids":
            return [{"statistic_id": e, "unit_class": "power", "has_mean": True} for e in (PV, GRID, "sensor.other_pv")]
        start, end = (datetime.fromisoformat(msg[k]) for k in ("start_time", "end_time"))
        if msg["type"] == "recorder/statistics_during_period":
            step = timedelta(minutes=5) if msg["period"] == "5minute" else timedelta(hours=1)
            out = {}
            for sid in msg["statistic_ids"]:
                rows, t = [], max(start, WINDOW) if msg["period"] == "5minute" else start
                t = t.replace(minute=t.minute - t.minute % 5, second=0) if step.seconds == 300 else t.replace(minute=0, second=0)
                while t < end:
                    w = {GRID: self.grid_w, PV: self.pv_w}.get(sid, self.other_w)
                    rows.append({"start": int(t.timestamp() * 1000), "end": int((t + step).timestamp() * 1000), "mean": w})
                    t += step
                out[sid] = rows
            return out
        if msg["type"] == "history/history_during_period":
            rows, t = [], start
            while t < end:                         # a busy meter: a new state every minute
                rows.append({"s": str(self.grid_w + (1 if t.minute % 2 else 0)), "lu": t.timestamp()})
                t += timedelta(minutes=1)
            return {GRID: rows}
        raise AssertionError(msg)


def service(tmp_path, recorder, now, plant=MINIMAL, connected=True, archive=None):
    return PlantHistory(parse(plant), archive or PlantArchive(tmp_path / "archive.sqlite3"), recorder.read,
                        lambda: connected, lambda: {GRID: "W", PV: "W"}, lambda: "Europe/Berlin", now=lambda: now[0])


def wh(day, cat):
    return round(sum(e.wh for e in day.categories[cat] if e.wh is not None), 1)


async def test_a_day_is_computed_once_archived_and_then_served_without_home_assistant(tmp_path):
    rec, now = Recorder(), [NOW]
    hist = service(tmp_path, rec, now)
    d = date(2026, 11, 10)
    day = await hist.day(d)
    assert day.tier == TIER_RAW and wh(day, "pv_1") == 24_000.0 and wh(day, "grid.import") == pytest.approx(4812.0, abs=1)
    calls = len(rec.calls)
    assert await hist.day(d) == day and len(rec.calls) == calls                    # from the archive
    offline = service(tmp_path, rec, now, connected=False, archive=hist.archive)
    assert (await offline.day(d)).categories == day.categories                   # no session needed
    with pytest.raises(ConnectionError):
        await offline.day(date(2026, 11, 11))                                   # not archived, no session
    assert hist.archive.summary()["raw_durable"]["days"] == 1


async def test_older_days_come_from_hourly_rows_and_the_window_is_measured(tmp_path):
    rec, now = Recorder(), [NOW]
    hist = service(tmp_path, rec, now)
    assert hist.tier_for(date(2026, 11, 6)) == TIER_RAW                         # default window: 10 days
    assert hist.tier_for(date(2026, 11, 3)) == TIER_HOUR
    old = await hist.day(date(2026, 10, 20))
    assert hist.window_start == WINDOW
    assert old.tier == TIER_HOUR and wh(old, "pv_1") == 24_000.0 and wh(old, "grid.net") == 4800.0
    assert all(e.wh is None for e in old.categories["grid.import"])               # balance only (decision 2)
    assert all(e.time_allocated for e in old.categories["pv_1"])                 # 96 quarter hours, spread (decision 1)
    assert hist.tier_for(date(2026, 11, 1)) == TIER_HOUR                         # begins before 03:15Z
    assert hist.tier_for(date(2026, 11, 2)) == hist.tier_for(date(2026, 11, 3)) == TIER_RAW    # measured window


async def test_the_running_day_is_refreshed_and_becomes_final_six_hours_after_its_end(tmp_path):
    rec, now = Recorder(), [NOW]
    hist = service(tmp_path, rec, now)
    today = date(2026, 11, 15)
    first = await hist.day(today)
    assert wh(first, "pv_1") == 12_000.0 and first.categories["pv_1"][-1].reasons == {"future"}    # 00:00–12:00
    now[0] = NOW + timedelta(minutes=3)
    assert await hist.day(today) is not None and hist.archive.get(today).computed_at == NOW            # still fresh
    now[0] = NOW + timedelta(minutes=6)
    assert wh(await hist.day(today), "pv_1") == 12_000.0 + 100.0 and hist.archive.get(today).computed_at == now[0]
    end = period_bounds("day", today)[1]
    now[0] = end + timedelta(hours=1)
    await hist.day(today)
    assert not hist.archive.get(today).final                                     # late rows may still come
    now[0] = end + timedelta(hours=7)
    await hist.day(today)
    a = hist.archive.get(today)
    assert a.final and a.durable and wh(a.day, "pv_1") == 24_000.0


async def test_archived_raw_days_stay_while_hour_days_follow_a_changed_sensor(tmp_path):
    rec, now = Recorder(), [NOW]
    archive = PlantArchive(tmp_path / "archive.sqlite3")
    hist = service(tmp_path, rec, now, archive=archive)
    raw_day, hour_day = date(2026, 11, 10), date(2026, 10, 20)
    await hist.day(raw_day)
    await hist.day(hour_day)
    changed = copy.deepcopy(MINIMAL)
    changed["revision"] = 2
    changed["modules"]["pv_1"]["points"]["power"]["entity"] = "sensor.other_pv"
    later = service(tmp_path, rec, now, plant=changed, archive=archive)       # sensor.other_pv reads 500 W
    assert wh(await later.day(raw_day), "pv_1") == 24_000.0                       # frozen (decision 3)
    assert wh(await later.day(hour_day), "pv_1") == 24_000.0                      # computed with the plant of that day
    # explicit recomputation: the raw day is in the window still, the hour day uses the plant active then
    assert archive.mark_stale([raw_day]) == 1
    assert wh(await later.day(raw_day), "pv_1") == 24_000.0                      # revision 1 was active on that day
    newer = service(tmp_path, rec, [NOW + timedelta(days=2)], plant=changed, archive=archive)
    assert wh(await newer.day(date(2026, 11, 16)), "pv_1") == 12_000.0           # revision 2 from the day it was seen


async def test_a_module_switched_off_later_stays_in_the_days_it_was_active(tmp_path):
    rec, now = Recorder(), [NOW]
    archive = PlantArchive(tmp_path / "archive.sqlite3")
    with_second = copy.deepcopy(MINIMAL)
    with_second["modules"]["pv_2"] = {"enabled": True, "name": "B", "points": {"power": {
        "entity": "sensor.other_pv", "unit": "W", "sign": 1, "role": "authoritative", "valid_from": None}}}
    first = service(tmp_path, rec, [NOW - timedelta(days=30)], plant=with_second, archive=archive)
    assert first.plant.revision == 1
    without = copy.deepcopy(with_second)
    without["revision"] = 2
    without["modules"]["pv_2"]["enabled"] = False
    hist = service(tmp_path, rec, now, plant=without, archive=archive)
    old = await hist.day(date(2026, 10, 20))                                    # before revision 2 was seen
    assert "pv_2" in old.categories and wh(old, "generation") == 24_000.0 + 12_000.0
    assert "pv_2" in (await hist.day(date(2026, 11, 15))).categories             # began before revision 2 was seen
    later = service(tmp_path, rec, [NOW + timedelta(days=1)], plant=without, archive=archive)
    assert "pv_2" not in (await later.day(date(2026, 11, 16))).categories


async def test_one_computation_per_day_however_many_ask(tmp_path):
    rec, now = Recorder(), [NOW]
    hist = service(tmp_path, rec, now)
    days = await asyncio.gather(*(hist.day(date(2026, 11, 12)) for _ in range(5)))
    assert all(x is days[0] for x in days)
    assert rec.calls.count("history/history_during_period") == 5                 # one day of grid raw: 25 h in 6-h slices
    # window probe once (a binary search over one-hour probes: about eleven) + the day once
    probes = rec.calls.count("recorder/statistics_during_period") - 1
    assert 8 <= probes <= 12
    await asyncio.gather(*(hist.day(date(2026, 11, 11)) for _ in range(3)))
    assert rec.calls.count("recorder/statistics_during_period") == probes + 2     # measured once a day


async def test_the_background_run_archives_the_keep_window_oldest_first(tmp_path):
    rec, now = Recorder(), [NOW]
    hist = service(tmp_path, rec, now)
    assert await hist.keep_archived() == 14                                       # 02.11. … 15.11.
    s = hist.archive.summary()
    assert s["raw_durable"]["first"] == "2026-11-02" and s["raw_durable"]["last"] == "2026-11-14"
    assert s["raw"]["days"] == 1                                                  # today: not final yet
    assert await hist.keep_archived() == 0
    now[0] = NOW + timedelta(hours=1)
    assert await hist.keep_archived() == 1                                        # today again, the rest stays
    assert await service(tmp_path, rec, now, connected=False, archive=hist.archive).keep_archived() == 0


async def test_future_days_need_no_request_and_are_not_archived(tmp_path):
    rec, now = Recorder(), [NOW]
    hist = service(tmp_path, rec, now)
    day = await hist.day(date(2026, 11, 20))
    assert all(e.wh is None and e.reasons == {"future"} for e in day.categories["pv_1"])
    assert rec.calls == [] and hist.archive.get(date(2026, 11, 20)) is None


def test_the_archive_is_part_of_the_backup(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    PlantArchive(data / "archive.sqlite3").record_plant(parse(MINIMAL), NOW)
    archive = backup.create(data, tmp_path / "backups")
    restored = tmp_path / "restored"
    restored.mkdir()
    assert "archive.sqlite3" in backup.restore(archive, restored)
