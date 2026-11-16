"""Edge cases of the generic history found in the Codex review of P3 (2026-11-15): unknown is never 0, the archive keeps
its day bounds, Home Assistant failures fall back to archived days, the calculation leaves the event loop."""

import asyncio
import copy
import threading
from datetime import UTC, date, datetime, timedelta

import pytest

from solar_visor_hub import plant_history as plant_history_module
from solar_visor_hub.energy import Point as EPoint
from solar_visor_hub.ha_history import StatRow, oldest_statistic
from solar_visor_hub.history import journal_invalid
from solar_visor_hub.journal import Journal
from solar_visor_hub.models import MetricPoint, MetricState
from solar_visor_hub.history_v2 import plant_period
from solar_visor_hub.periods import period_bounds, quarter_hours, zone
from solar_visor_hub.plant import parse
from solar_visor_hub.plant_archive import PlantArchive
from solar_visor_hub.plant_daycalc import TIER_HOUR, TIER_RAW, PointData, compute_day
from solar_visor_hub.plant_history import PlantHistory
from solar_visor_hub.plant_history_source import read_day
from solar_visor_hub.plant_power import plant_power
from solar_visor_hub.plant_samples import MINIMAL
from solar_visor_hub.sun import night_intervals
from solar_visor_hub.widget_v2 import plant_widget

from test_plant_history import GRID, NOW, PV, Recorder, service

TZ = zone("Europe/Berlin")
START, END = period_bounds("day", date(2026, 11, 14), TZ)
SLOTS = quarter_hours(START, END, TZ)


def pt(entity, unit="W", role="authoritative", **extra):
    return {"entity": entity, "unit": unit, "sign": 1, "role": role, "valid_from": None, **extra}


def means(w, step, start=START, end=END):
    rows, t = [], start
    while t < end:
        rows.append(StatRow(t, t + timedelta(seconds=step), mean=w(t) if callable(w) else w))
        t += timedelta(seconds=step)
    return rows


def tot(day, cat):
    known = [e for e in day.categories[cat] if e.wh is not None]
    cov = sum(e.covered_s for e in known) / sum(e.applicable_s for e in known) if known else None
    return (round(sum(e.wh for e in known), 2) if known else None, None if cov is None else round(cov, 4))


def with_pv_counter():
    p = copy.deepcopy(MINIMAL)
    p["modules"]["pv_1"]["points"]["energy_total"] = pt("sensor.pv_energy", "Wh", "cross_check")
    return parse(p)


# --- day calculation ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("tier,step", [(TIER_RAW, 300), (TIER_HOUR, 3600)])
def test_a_counter_without_rows_is_unknown_not_a_measured_zero(tier, step):
    """H2: HA writes a row every period while the sensor is available – none at all means it was not."""
    base = [StatRow(START - timedelta(seconds=step), START, sum=10_000.0)]
    grid = PointData(raw=[EPoint(START, 100.0)]) if tier == TIER_RAW else PointData(stats=means(100.0, 3600))
    day = compute_day(with_pv_counter(), SLOTS, START, END, tier, {"pv_1.energy_total": PointData(stats=base),
                                                                   "grid.power": grid})
    known = [i for i, e in enumerate(day.categories["pv_1"]) if e.wh is not None]
    assert known == ([0] if tier == TIER_RAW else [0, 1, 2, 3])        # one period: the next row may still come
    assert day.categories["pv_1"][0].wh == 0.0 and day.categories["pv_1"][10].reasons == {"no_data"}


def test_a_source_outage_ends_the_counter_hold():
    rows = [StatRow(START - timedelta(minutes=5), START, sum=10_000.0)]
    day = compute_day(with_pv_counter(), SLOTS, START, END, TIER_RAW,
                      {"pv_1.energy_total": PointData(stats=rows), "grid.power": PointData(raw=[EPoint(START, 0.0)])},
                      invalid={"sensor.pv_energy": [(START, END)]})
    assert all(e.wh is None for e in day.categories["pv_1"])


def test_the_counter_base_is_the_highest_reading_before_the_day():
    """M3: a dip just before midnight and the recovery after it are no energy."""
    rows = [StatRow(START - timedelta(minutes=10), START - timedelta(minutes=5), sum=1000.0),
            StatRow(START - timedelta(minutes=5), START, sum=900.0)]
    rows += [StatRow(r.start, r.end, sum=1000.0) for r in means(0.0, 300)]
    day = compute_day(with_pv_counter(), SLOTS, START, END, TIER_RAW,
                      {"pv_1.energy_total": PointData(stats=rows), "grid.power": PointData(raw=[EPoint(START, 0.0)])})
    assert tot(day, "pv_1") == (0.0, 1.0)


def test_autarky_compares_import_and_consumption_on_the_same_time():
    """M1: consumption known for the whole quarter hour, import only for its first five minutes – on those five
    minutes all came from the grid."""
    p = copy.deepcopy(MINIMAL)
    p["modules"]["consumption"] = {"points": {"power": pt("sensor.cons")}}
    plant = parse(p)
    q = START + timedelta(minutes=15)
    raw = [EPoint(START, 1000.0), EPoint(START + timedelta(minutes=5), None), EPoint(q, 1000.0)]
    day = compute_day(plant, SLOTS, START, q, TIER_RAW,
                      {"pv_1.power": PointData(stats=means(0.0, 300, end=q)),
                       "consumption.power": PointData(stats=means(1000.0, 300, end=q)), "grid.power": PointData(raw=raw)})
    n, d, joint, app = day.kpi_parts["autarky"][0]
    assert (joint, app) == (300.0, 900.0) and n == pytest.approx(d) and 1 - n / d == pytest.approx(0.0)


def test_the_hourly_balance_counts_only_from_valid_from():
    """M11: an hourly mean before the point existed is no energy."""
    p = copy.deepcopy(MINIMAL)
    p["modules"]["grid"]["points"]["power"]["valid_from"] = (START + timedelta(minutes=7)).isoformat()
    day = compute_day(parse(p), SLOTS, START, END, TIER_HOUR,
                      {"pv_1.power": PointData(stats=means(0.0, 3600)), "grid.power": PointData(stats=means(-1000.0, 3600))})
    e = day.categories["grid.net"][0]
    assert e.applicable_s == 480 and e.wh == pytest.approx(-1000 * 480 / 3600)


def test_pv_is_zero_at_night_without_rows():
    """N1: an inverter that reports unavailable at night leaves no gap when the location is known."""
    def daylight(t):
        return 8 <= t.astimezone(TZ).hour < 16
    rows = [r for r in means(500.0, 300) if daylight(r.start)]
    data = {"pv_1.power": PointData(stats=rows), "grid.power": PointData(raw=[EPoint(START, 0.0)])}
    without = compute_day(parse(MINIMAL), SLOTS, START, END, TIER_RAW, data)
    nights = night_intervals(START, END, lat=52.52, lon=13.40)
    with_night = compute_day(parse(MINIMAL), SLOTS, START, END, TIER_RAW, data, nights=nights)
    def known(day):
        return sum(1 for e in day.categories["pv_1"] if e.wh is not None)
    assert known(without) == 32                                                 # 08–16 local only
    assert tot(with_night, "pv_1")[0] == tot(without, "pv_1")[0] and known(with_night) > 90
    assert with_night.categories["pv_1"][4].wh == 0.0 and with_night.categories["pv_1"][4].coverage == 1.0   # 01:00


# --- reading --------------------------------------------------------------------------------------------------------

async def test_raw_states_of_a_sensor_no_longer_followed_take_the_statistic_unit():
    """M5: a sensor of an older revision is not in the live units – kW stays kW, an unknown unit stays unknown."""
    async def read(msg):
        if msg["type"] == "history/history_during_period":
            return {GRID: [{"s": "1.5", "lu": (START - timedelta(hours=1)).timestamp()}]}
        return {}
    plant = parse(MINIMAL)
    data = await read_day(plant, START, END, TIER_RAW, read, {GRID: {"display_unit_of_measurement": "kW"}}, {})
    assert data["grid.power"].raw[0].value == 1500.0
    data = await read_day(plant, START, END, TIER_RAW, read, {}, {})
    assert data["grid.power"].raw is None


async def test_the_keep_window_is_the_oldest_row_of_any_source():
    """M4: a counter has sums, no means; a sensor added later must not move the window."""
    old, new = datetime(2026, 11, 1, 3, 15, tzinfo=UTC), datetime(2026, 11, 12, 9, 0, tzinfo=UTC)

    async def read(msg):
        start, end = (datetime.fromisoformat(msg[k]) for k in ("start_time", "end_time"))
        out = {}
        for sid, since, key in (("sensor.counter", old, "sum"), ("sensor.new", new, "mean")):
            if sid in msg["statistic_ids"]:
                t, rows = max(start, since), []
                while t < end:
                    rows.append({"start": int(t.timestamp() * 1000), "end": int((t + timedelta(minutes=5)).timestamp() * 1000),
                                 key: 1.0})
                    t += timedelta(minutes=5)
                out[sid] = rows
        return out
    assert await oldest_statistic(read, ["sensor.counter", "sensor.new"], NOW) == old
    assert await oldest_statistic(read, ["sensor.new"], NOW) == new
    assert await oldest_statistic(read, ["sensor.none"], NOW) is None


# --- history service ------------------------------------------------------------------------------------------------

async def test_a_day_archived_under_another_time_zone_is_computed_anew(tmp_path):
    """M6: other day bounds – and nothing is archived before Home Assistant has told the time zone."""
    rec, now = Recorder(), [NOW]
    d = date(2026, 11, 10)
    hist = service(tmp_path, rec, now)
    await hist.day(d)
    ny = PlantHistory(parse(MINIMAL), hist.archive, rec.read, lambda: True, lambda: {GRID: "W", PV: "W"},
                      lambda: "America/New_York", now=lambda: now[0])
    assert ny.cached(d) is None
    assert len((await ny.day(d)).categories["pv_1"]) == 96
    unknown_tz = PlantHistory(parse(MINIMAL), PlantArchive(tmp_path / "other.sqlite3"), rec.read, lambda: True,
                              lambda: {GRID: "W", PV: "W"}, lambda: None, now=lambda: now[0])
    await unknown_tz.day(d)
    assert unknown_tz.archive.get(d) is None and await unknown_tz.keep_archived() == 0


async def test_without_home_assistant_the_last_archived_state_is_served_as_refreshing(tmp_path):
    """Z2: a period with today keeps its archived days when Home Assistant is away."""
    rec, now = Recorder(), [NOW]
    today = date(2026, 11, 15)
    hist = service(tmp_path, rec, now)
    fresh = await hist.day(today)
    now[0] = NOW + timedelta(minutes=10)                                        # due for a refresh
    offline = service(tmp_path, rec, now, connected=False, archive=hist.archive)
    old = await offline.day(today)
    assert old.stale and old.categories == fresh.categories
    p = plant_period("day", today, TZ, [(today, old)], now[0], 1, False)
    assert p.refreshing and p.totals.energy_wh["pv_1"] is not None
    assert not (await hist.day(today)).stale                                    # with a session: computed anew


async def test_the_calculation_runs_beside_the_event_loop(tmp_path, monkeypatch):
    """H4: a busy day must not hold up the live values."""
    threads = []
    real = plant_history_module.compute_day

    def spy(*a, **kw):
        threads.append(threading.current_thread() is threading.main_thread())
        return real(*a, **kw)
    monkeypatch.setattr(plant_history_module, "compute_day", spy)
    await service(tmp_path, Recorder(), [NOW]).day(date(2026, 11, 10))
    assert threads == [False]


async def test_closing_cancels_running_computations(tmp_path):
    """M8: shutdown does not wait for Home Assistant."""
    gate = asyncio.Event()
    rec = Recorder()

    async def slow(msg):
        if msg["type"] == "history/history_during_period":
            await gate.wait()
        return await rec.read(msg)
    hist = PlantHistory(parse(MINIMAL), PlantArchive(tmp_path / "a.sqlite3"), slow, lambda: True,
                        lambda: {GRID: "W", PV: "W"}, lambda: "Europe/Berlin", now=lambda: NOW)
    task = asyncio.create_task(hist.day(date(2026, 11, 10)))
    await asyncio.sleep(0.05)
    await hist.aclose()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(ConnectionError):
        await hist.day(date(2026, 11, 9))


# --- v2 adapters ----------------------------------------------------------------------------------------------------

def test_a_day_without_any_measurement_is_not_complete():
    """M2: a category with no known value at all still counts – and says how much it may hide."""
    data = {"grid.power": PointData(raw=[EPoint(START, 100.0)])}           # PV: nothing read
    day = compute_day(parse(MINIMAL), SLOTS, START, END, TIER_RAW, data)
    p = plant_period("day", START.astimezone(TZ).date(), TZ, [(START.astimezone(TZ).date(), day)],
                     END + timedelta(days=1), 1, False)
    assert p.totals.energy_wh["pv_1"] is None and p.totals.coverage["pv_1"] == 0.0
    assert not p.complete and p.totals.unresolved_wh_max["pv_1"] > 0
    assert p.totals.sources["pv_1"].method == "statistic_mean"


async def test_the_widget_window_averages_over_the_measured_time():
    """Z1: a quarter hour half computed (the running day is refreshed every few minutes) is not half the power."""
    now = datetime(2026, 11, 15, 11, 16, tzinfo=UTC)
    start, end = period_bounds("day", date(2026, 11, 15), TZ)
    upto = datetime(2026, 11, 15, 11, 7, tzinfo=UTC)
    rows = means(3000.0, 60, start=start, end=upto)                             # computed at 12:07 local

    class History:
        tz = TZ

        async def day(self, d):
            s, e = period_bounds("day", d, TZ)
            if d == date(2026, 11, 15):
                return compute_day(parse(MINIMAL), quarter_hours(s, e, TZ), s, upto, TIER_RAW,
                                   {"pv_1.power": PointData(stats=rows), "grid.power": PointData(raw=[EPoint(s, 0.0)])})
            return compute_day(parse(MINIMAL), quarter_hours(s, e, TZ), s, e, TIER_RAW,
                               {"pv_1.power": PointData(stats=means(3000.0, 300, s, e)),
                                "grid.power": PointData(raw=[EPoint(s, 0.0)])})
    from solar_visor_hub.plant_samples import sample_live
    live = sample_live(MINIMAL, {"grid.power": "0", "pv_1.power": "3000"})
    w = await plant_widget(live.snapshot(now), live.plant, History(), now, (ConnectionError,))
    v = w.window.values["pv_1.power"]
    assert v.mean_w == 3000.0 and v.coverage == pytest.approx(7 / 15, abs=0.001) and v.measured_until is None


class PowerHistory:
    def __init__(self, sign_rows):
        self.tz, self.rows = TZ, sign_rows

    def tier_for(self, d):
        return TIER_RAW

    async def _statistic_meta(self):
        return {e: {"unit_class": "power"} for e in (PV, GRID, "sensor.old_pv")}

    async def _read(self, msg):
        start, end = (datetime.fromisoformat(msg[k]) for k in ("start_time", "end_time"))
        out = {}
        for sid in msg["statistic_ids"]:
            rows, t = [], start
            while t < end:
                rows.append({"start": int(t.timestamp() * 1000), "end": int((t + timedelta(minutes=5)).timestamp() * 1000),
                             "mean": 20.0, "min": 10.0, "max": 30.0})
                t += timedelta(minutes=5)
            out[sid] = rows
        return out


async def test_power_extremes_swap_with_a_negative_source_sign():
    """M9: min of −x is −max x."""
    p = copy.deepcopy(MINIMAL)
    p["modules"]["pv_1"]["points"]["power"]["sign"] = -1
    start = datetime(2026, 11, 14, 10, 0, tzinfo=UTC)
    resp = await plant_power(PowerHistory(None), parse(p), ["pv_1.power"], start, start + timedelta(hours=1), 600)
    pt0 = resp.series[0].points[0]
    assert (pt0.mean, pt0.min, pt0.max) == (-20.0, -30.0, -10.0)


async def test_power_follows_valid_from_and_the_plant_of_each_day():
    """M10: no curve before a point existed; a module switched off later is still drawn on its old days."""
    p = copy.deepcopy(MINIMAL)
    p["modules"]["pv_1"]["points"]["power"]["valid_from"] = "2026-11-14T10:30:00+00:00"
    older = copy.deepcopy(p)
    older["modules"]["pv_2"] = {"enabled": True, "name": "Alt", "points": {"power": pt("sensor.old_pv")}}
    old = parse(older)
    p["revision"] = 2
    current = parse(p)
    start, mid = datetime(2026, 11, 13, 23, 0, tzinfo=UTC), datetime(2026, 11, 14, 23, 0, tzinfo=UTC)
    revisions = {start: old, mid: current}
    resp = await plant_power(PowerHistory(None), current, ["pv_1.power", "pv_2.power"], start + timedelta(hours=11),
                             mid + timedelta(hours=2), 2000, revisions)
    pv1 = {x.t: x.mean for x in resp.series[0].points}
    pv2 = {x.t: x.mean for x in resp.series[1].points}
    assert pv1[datetime(2026, 11, 14, 10, 25, tzinfo=UTC)] is None and pv1[datetime(2026, 11, 14, 10, 30, tzinfo=UTC)] == 20.0
    assert pv2[datetime(2026, 11, 14, 10, 30, tzinfo=UTC)] == 20.0 and pv2[mid + timedelta(hours=1)] is None


# --- a value nobody confirms (user decision 2026-11-15) -------------------------------------------------------------

def test_a_source_that_stopped_reporting_cuts_the_generic_history_only(tmp_path):
    """H3: HA hands the state at the request start with the request time, so an unchanged 0 W looks fresh every day.
    The live check of last_reported (report_too_old) is the evidence; a hub-side disconnect is not."""
    t0 = datetime(2026, 11, 14, 0, 0, tzinfo=UTC)

    def point(state, reasons):
        return MetricPoint(metric_id="grid.power", value=None, unit="W", state=state, observed_at=t0, source_id="grid",
                           source_entity=GRID, measurement_boundary="grid", reasons=reasons)
    j = Journal(tmp_path / "j.sqlite3")
    j.record_start(t0)
    j.record_quality({"grid.power": point(MetricState.OK, [])}, t0)
    j.record_quality({"grid.power": point(MetricState.STALE, ["report_too_old"])}, t0 + timedelta(hours=2))
    j.record_quality({"grid.power": point(MetricState.OK, [])}, t0 + timedelta(hours=5))
    j.record_quality({"grid.power": point(MetricState.STALE, ["ha_disconnected"])}, t0 + timedelta(hours=7))
    j.record_quality({"grid.power": point(MetricState.OK, [])}, t0 + timedelta(hours=8))
    j.heartbeat(t0 + timedelta(hours=9))
    span = (t0, t0 + timedelta(hours=9))
    assert journal_invalid(tmp_path / "j.sqlite3", stale=True)(GRID, *span) == [(t0 + timedelta(hours=2),
                                                                                t0 + timedelta(hours=5))]
    assert journal_invalid(tmp_path / "j.sqlite3")(GRID, *span) == []          # the existing calculation: unchanged
    raw = [EPoint(t0 - timedelta(hours=1), 0.0)]                                # HA's start state, days old in truth
    day = compute_day(parse(MINIMAL), quarter_hours(t0, t0 + timedelta(hours=9), TZ), t0, t0 + timedelta(hours=9),
                      TIER_RAW, {"pv_1.power": PointData(stats=means(0.0, 300, t0, t0 + timedelta(hours=9))),
                                 "grid.power": PointData(raw=raw)},
                      invalid={GRID: journal_invalid(tmp_path / "j.sqlite3", stale=True)(GRID, *span)})
    unknown = [i for i, e in enumerate(day.categories["grid.import"]) if e.wh is None]
    assert unknown == list(range(8, 20))                                        # 02:00–05:00 UTC
