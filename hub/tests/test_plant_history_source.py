"""Reading what the generic day calculation needs (P3 step 3): units, statistic scaling, raw states."""

from datetime import UTC, date, datetime, timedelta

from solar_visor_hub.periods import period_bounds
from solar_visor_hub.plant import parse
from solar_visor_hub.plant_daycalc import TIER_HOUR, TIER_RAW
from solar_visor_hub.plant_history_source import read_day
from solar_visor_hub.plant_samples import MINIMAL

START, END = period_bounds("day", date(2026, 11, 14))
PV, GRID = "sensor.sample_pv_power", "sensor.sample_grid_power"


class FakeRead:
    def __init__(self):
        self.calls = []

    async def __call__(self, msg):
        self.calls.append(msg)
        if msg["type"] == "recorder/statistics_during_period":
            ms = int(START.timestamp() * 1000)
            return {sid: [{"start": ms, "end": ms + 3_600_000, "mean": 2.0, "sum": None}] for sid in msg["statistic_ids"]}
        if msg["type"] == "history/history_during_period":
            a, b = (datetime.fromisoformat(msg[k]).timestamp() for k in ("start_time", "end_time"))
            rows = [{"s": "1.5", "lu": START.timestamp() - 600}, {"s": "unavailable", "lu": START.timestamp() + 60}]
            return {msg["entity_ids"][0]: [r for r in rows if a <= r["lu"] < b]}
        raise AssertionError(msg)


async def test_raw_tier_reads_statistics_for_the_generator_and_raw_states_for_the_signed_grid():
    read = FakeRead()
    meta = {PV: {"unit_class": "power"}, GRID: {"unit_class": "power"}}
    data = await read_day(parse(MINIMAL), START, END, TIER_RAW, read, meta, {GRID: "kW"})
    assert data["pv_1.power"].stats[0].mean == 2.0 and data["pv_1.power"].raw is None
    assert [p.value for p in data["grid.power"].raw] == [1500.0, None]          # kW → W like the live value, unavailable → None
    assert data["grid.power"].stats is None
    stat_calls = [c for c in read.calls if c["type"] == "recorder/statistics_during_period"]
    assert len(stat_calls) == 1 and stat_calls[0]["statistic_ids"] == [PV] and stat_calls[0]["period"] == "5minute"
    raw_calls = [c for c in read.calls if c["type"] == "history/history_during_period"]
    assert raw_calls[0]["start_time"] == (START - timedelta(hours=1)).isoformat()   # carry-in


async def test_hour_tier_reads_only_statistics_and_scales_what_ha_cannot_convert():
    read = FakeRead()
    meta = {PV: {"unit_class": None, "statistics_unit_of_measurement": "kW"}, GRID: {"unit_class": "power"}}
    data = await read_day(parse(MINIMAL), START, END, TIER_HOUR, read, meta, {})
    assert data["pv_1.power"].stats[0].mean == 2000.0                            # kW without a unit class: scaled here
    assert data["grid.power"].stats[0].mean == 2.0                               # HA converted it already
    assert all(c["type"] == "recorder/statistics_during_period" and c["period"] == "hour" for c in read.calls)


async def test_a_sensor_without_statistics_or_with_an_unknown_unit_gives_no_rows():
    read = FakeRead()
    meta = {PV: {"unit_class": None, "statistics_unit_of_measurement": "°C"}}
    data = await read_day(parse(MINIMAL), START, END, TIER_HOUR, read, meta, {})
    assert data["pv_1.power"].stats is None and data["grid.power"].stats is None
