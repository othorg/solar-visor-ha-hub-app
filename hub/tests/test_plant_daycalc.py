"""Generic day calculation (P3 step 3): tiers, signed power, counters, derived categories, null instead of 0.

The acceptance against the existing calculation runs on real data (plan §10): 8 days of the own plant, every category
within 0.5 % / 25 Wh. These tests pin the rules on synthetic data."""

import copy
from datetime import UTC, date, datetime, timedelta

import pytest

from solar_visor_hub.daycalc import coverage
from solar_visor_hub.energy import Point as EPoint
from solar_visor_hub.ha_history import StatRow
from solar_visor_hub.periods import period_bounds, quarter_hours
from solar_visor_hub.plant import parse
from solar_visor_hub.plant_daycalc import (TIER_HOUR, TIER_RAW, PointData, compute_day, requirements)
from solar_visor_hub.plant_samples import MINIMAL, THREE_PV

START, END = period_bounds("day", date(2026, 11, 14))
SLOTS = quarter_hours(START, END)


def point(entity, unit="W", role="authoritative", **extra):
    return {"entity": entity, "unit": unit, "sign": 1, "role": role, "valid_from": None, **extra}


def with_modules(base, **modules):
    raw = copy.deepcopy(base)
    raw["modules"].update(modules)
    return raw


def means(watts, step_s, start=START, end=END):
    """Statistic rows with a constant mean (watts: number or function of the row start)."""
    rows, t = [], start
    while t < end:
        w = watts(t) if callable(watts) else watts
        rows.append(StatRow(t, t + timedelta(seconds=step_s), mean=w))
        t += timedelta(seconds=step_s)
    return rows


def counter_rows(wh_per_row, step_s, start=START, end=END, first=10_000.0):
    rows, t, total = [StatRow(start - timedelta(seconds=step_s), start, sum=first)], start, first
    while t < end:
        total += wh_per_row(t) if callable(wh_per_row) else wh_per_row
        rows.append(StatRow(t, t + timedelta(seconds=step_s), sum=total))
        t += timedelta(seconds=step_s)
    return rows


def total(day, cat):
    slots = day.categories[cat]
    return round(sum(e.wh for e in slots if e.wh is not None), 3) if any(e.wh is not None for e in slots) else None


def run(raw_plant, tier, data, end=END):
    return compute_day(parse(raw_plant), SLOTS, START, end, tier, data)


def profile(t):
    """Grid: import 1200 W until 06:00 local, export 800 W until 18:00, import 400 W after."""
    hour = (t - START).total_seconds() / 3600
    return 1200.0 if hour < 6 else (-800.0 if hour < 18 else 400.0)


def raw_from(profile_fn, step=timedelta(minutes=5)):
    pts, t = [EPoint(START - timedelta(minutes=30), profile_fn(START))], START
    while t < END:
        pts.append(EPoint(t, profile_fn(t)))
        t += step
    return pts


def test_minimal_plant_raw_tier_splits_the_grid_and_calculates_the_consumption():
    data = {"pv_1.power": PointData(stats=means(lambda t: 1000.0 if 8 <= (t - START).total_seconds() / 3600 < 16 else 0.0, 300)),
            "grid.power": PointData(raw=raw_from(profile))}
    day = run(MINIMAL, TIER_RAW, data)
    assert len(day.categories["pv_1"]) == 96 and day.resolution_s == 900
    assert total(day, "pv_1") == total(day, "generation") == 8000.0
    assert total(day, "grid.import") == 1200 * 6 + 400 * 6 and total(day, "grid.export") == 800 * 12
    assert total(day, "grid.net") == 9600 - 9600
    assert total(day, "consumption") == 8000.0 + 0.0                 # generation + net grid
    assert total(day, "home") == total(day, "consumption")           # nothing beside home
    assert day.sources["grid.import"].method == "sample_hold" and day.sources["pv_1"].method == "statistic_mean"
    assert day.sources["consumption"].method == "derived"
    assert coverage(day.categories["grid.import"]) == 1.0
    assert not any(e.time_allocated for e in day.categories["pv_1"])
    # supply: import first, the rest from the generator – per slot, at most what was consumed in it
    consumed = sum(max(e.wh, 0.0) for e in day.categories["consumption"])
    assert total(day, "supply.grid") == 9600.0
    assert total(day, "supply.pv_1") + total(day, "supply.grid") + total(day, "supply.battery") == pytest.approx(consumed)


def test_hour_tier_keeps_the_balance_and_leaves_the_directions_empty():
    data = {"pv_1.power": PointData(stats=means(500.0, 3600)), "grid.power": PointData(stats=means(profile, 3600))}
    day = run(MINIMAL, TIER_HOUR, data)
    assert len(day.categories["grid.net"]) == 96 and day.resolution_s == 3600
    assert total(day, "grid.net") == 0.0 and total(day, "pv_1") == 12_000.0
    for cat in ("grid.import", "grid.export"):
        assert total(day, cat) is None and all(e.reasons == {"signed_needs_raw"} for e in day.categories[cat])
    assert total(day, "consumption") == 12_000.0                     # the balance is enough for the consumption
    assert all(e.time_allocated for e in day.categories["pv_1"])      # an hour spread over its quarter hours
    assert total(day, "supply.grid") is None                          # needs the import direction
    assert all(row[2] == 0.0 for row in day.kpi_parts["autarky"])     # no autarky without the direction


def test_the_same_energy_from_raw_states_five_minute_and_hourly_rows():
    raw = run(MINIMAL, TIER_RAW, {"pv_1.power": PointData(stats=means(300.0, 300)),
                                  "grid.power": PointData(raw=raw_from(profile))})
    five = run(MINIMAL, TIER_RAW, {"pv_1.power": PointData(stats=means(300.0, 300)),
                                   "grid.power": PointData(raw=raw_from(profile, timedelta(minutes=1)))})
    hour = run(MINIMAL, TIER_HOUR, {"pv_1.power": PointData(stats=means(300.0, 3600)),
                                    "grid.power": PointData(stats=means(profile, 3600))})
    for cat in ("pv_1", "generation", "grid.net", "consumption", "home"):
        assert total(raw, cat) == total(five, cat) == total(hour, cat), cat


def test_counters_win_over_power_and_count_only_above_the_high_water_mark():
    raw_plant = with_modules(THREE_PV)                    # split grid with import/export counters, three generators
    data = {f"pv_{i}.power": PointData(stats=means(100.0 * i, 300)) for i in (2, 3)}
    data["pv_1.energy_total"] = PointData(stats=counter_rows(50.0, 300))                 # 600 W
    rows = counter_rows(25.0, 300)
    rows[50] = StatRow(rows[50].start, rows[50].end, sum=rows[50].sum - 400)           # rollback, then recovery
    data["grid.import_energy"] = PointData(stats=rows)
    data["grid.export_energy"] = PointData(stats=counter_rows(0.0, 300))
    data["battery.power"] = PointData(raw=[EPoint(START - timedelta(minutes=5), 0.0)])
    data["generic_1.energy_total"] = PointData(stats=counter_rows(10.0, 300))
    data["generic_2.power"] = PointData(stats=means(60.0, 300))
    day = run(raw_plant, TIER_RAW, data)
    assert set(requirements(parse(raw_plant), TIER_RAW)) == set(data)
    assert day.sources["pv_1"].method == "counter_delta" and total(day, "pv_1") == 50.0 * 288
    assert total(day, "generation") == 50.0 * 288 + 200 * 24 + 300 * 24
    # the reading below the high-water mark is skipped; the next one continues above it – nothing is counted twice
    assert total(day, "grid.import") == pytest.approx(25.0 * 288, abs=1e-6) and day.counter_anomalies["grid.import"] == 1
    assert total(day, "grid.export") == 0.0 and day.sources["grid.export"].method == "counter_delta"


def test_a_generator_before_its_valid_from_is_absent_and_an_unknown_one_makes_the_sum_unknown():
    later = (START + timedelta(hours=12)).isoformat()
    raw_plant = with_modules(MINIMAL, pv_2={"enabled": True, "name": "Neu", "points": {"power": point("sensor.pv2", valid_from=later)}})
    data = {"pv_1.power": PointData(stats=means(1000.0, 300)), "pv_2.power": PointData(stats=means(500.0, 300)),
            "grid.power": PointData(raw=raw_from(lambda t: 0.0))}
    day = run(raw_plant, TIER_RAW, data)
    assert total(day, "pv_2") == 500.0 * 12 and day.categories["pv_2"][0].reasons == {"no_source"}
    assert total(day, "generation") == 1000.0 * 24 + 500.0 * 12
    gap = dict(data, **{"pv_1.power": PointData(stats=[r for r in means(1000.0, 300) if r.start.hour != 3])})
    day = run(raw_plant, TIER_RAW, gap)
    unknown = [e for e in day.categories["generation"] if e.wh is None]
    assert len(unknown) == 4 and all("no_source" not in e.reasons for e in unknown)   # unknown, never 0
    assert sum(1 for e in day.categories["consumption"] if e.wh is None) == 4


def test_home_subtracts_wallbox_and_consumers_beside_home_and_an_unknown_wallbox_leaves_it_empty():
    raw_plant = with_modules(MINIMAL,
                             wallbox={"enabled": True, "name": "WB", "trust": "none", "points": {"power": point("sensor.wb", role="subset")}},
                             generic_1={"enabled": True, "name": "Sauna", "balance": "beside_home", "points": {"power": point("sensor.sauna", role="subset")}},
                             generic_2={"enabled": True, "name": "Womo", "balance": "part_of_home", "points": {"power": point("sensor.womo", role="subset")}},
                             consumption={"points": {"power": point("sensor.cons")}})
    data = {"pv_1.power": PointData(stats=means(0.0, 300)), "grid.power": PointData(raw=raw_from(lambda t: 3000.0)),
            "consumption.power": PointData(stats=means(3000.0, 300)), "wallbox.power": PointData(stats=means(1000.0, 300)),
            "generic_1.power": PointData(stats=means(500.0, 300)), "generic_2.power": PointData(stats=means(200.0, 300))}
    day = run(raw_plant, TIER_RAW, data)
    assert total(day, "consumption") == 72_000.0 and total(day, "home") == (3000 - 1000 - 500) * 24
    assert day.sources["consumption"].method == "statistic_mean"
    no_wallbox = dict(data, **{"wallbox.power": PointData(stats=None)})
    day = run(raw_plant, TIER_RAW, no_wallbox)
    assert total(day, "wallbox") is None and total(day, "home") is None


def test_supply_shares_the_direct_part_among_three_generators():
    raw_plant = with_modules(MINIMAL, pv_2={"enabled": True, "name": "B", "points": {"power": point("sensor.pv2")}},
                             pv_3={"enabled": True, "name": "C", "points": {"power": point("sensor.pv3")}},
                             battery={"enabled": True, "name": "Akku", "points": {"power": point("sensor.bat")}})
    data = {"pv_1.power": PointData(stats=means(600.0, 300)), "pv_2.power": PointData(stats=means(300.0, 300)),
            "pv_3.power": PointData(stats=means(100.0, 300)), "grid.power": PointData(raw=raw_from(lambda t: 500.0)),
            "battery.power": PointData(raw=raw_from(lambda t: -200.0))}
    day = run(raw_plant, TIER_RAW, data)
    assert total(day, "consumption") == (1000 + 500 + 200) * 24          # generation + grid − battery (discharging)
    assert total(day, "supply.grid") == 500 * 24 and total(day, "supply.battery") == 200 * 24
    assert (total(day, "supply.pv_1"), total(day, "supply.pv_2"), total(day, "supply.pv_3")) == (600 * 24, 300 * 24, 100 * 24)
    assert total(day, "battery.discharge") == 200 * 24 and total(day, "battery.charge") == 0.0


def test_the_hour_tier_says_future_and_not_determinable_apart():
    now = START + timedelta(hours=10)
    day = run(MINIMAL, TIER_HOUR, {"pv_1.power": PointData(stats=means(400.0, 3600, end=now)),
                                   "grid.power": PointData(stats=means(100.0, 3600, end=now))}, end=now)
    imp = day.categories["grid.import"]
    assert imp[0].reasons == {"signed_needs_raw"} and imp[39].reasons == {"signed_needs_raw"}
    assert imp[40].reasons == {"future"} and imp[95].reasons == {"future"}
    nothing = run(MINIMAL, TIER_RAW, {"pv_1.power": PointData(stats=None), "grid.power": PointData(raw=None)}, end=now)
    assert nothing.categories["grid.import"][0].reasons == {"no_data"} and nothing.categories["grid.import"][40].reasons == {"future"}


def test_the_running_day_ends_now_and_later_slots_are_future():
    now = START + timedelta(hours=10)
    day = run(MINIMAL, TIER_RAW, {"pv_1.power": PointData(stats=means(400.0, 300, end=now)),
                                  "grid.power": PointData(raw=raw_from(lambda t: 0.0))}, end=now)
    assert total(day, "pv_1") == 4000.0
    assert day.categories["pv_1"][40].wh is None and day.categories["pv_1"][40].reasons == {"future"}


@pytest.mark.parametrize("tier", [TIER_RAW, TIER_HOUR])
def test_requirements_name_exactly_what_the_calculation_reads(tier):
    for raw_plant in (MINIMAL, THREE_PV):
        plant = parse(raw_plant)
        need = requirements(plant, tier)
        read: list[str] = []

        class Spy(dict):
            def get(self, key, default=None):
                read.append(key)
                return super().get(key, default)

        compute_day(plant, SLOTS, START, END, tier, Spy())
        assert set(read) == set(need)
        if tier == TIER_HOUR:
            assert all("raw" not in kinds for kinds in need.values())
