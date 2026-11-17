"""PV generator with full feed-in (plan full-feed-in §4, §8): a generator behind its own meter keeps its node, values,
energy and strings but is never a term of the home balance – generation, consumption, home, residual, supply,
autarky and self-consumption see only the generators of the home. Plants without the mark compute as before (their
fixtures are compared bit for bit by the contract check). Free of the own plant's data: part of the public tests."""

import asyncio
import copy
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from solar_visor_hub.app import create_public_app
from solar_visor_hub.devices import DeviceStore
from solar_visor_hub.energy import Point as EPoint
from solar_visor_hub.energy_api import JobManager
from solar_visor_hub.ha_history import StatRow
from solar_visor_hub.history_v2 import plant_period
from solar_visor_hub.models import MetricState
from solar_visor_hub.models_v2 import PlantCapabilities, PlantEnergyPeriod, PlantSnapshot
from solar_visor_hub.periods import period_bounds, quarter_hours
from solar_visor_hub.plant import PlantError, dump, parse
from solar_visor_hub.plant_archive import source_key
from solar_visor_hub.plant_daycalc import TIER_HOUR, TIER_RAW, PointData, compute_day, requirements
from solar_visor_hub.plant_power import series_plans
from solar_visor_hub.plant_samples import (FULL_FEED_IN, FULL_FEED_IN_SINCE, MINIMAL, THREE_PV, T, _FULL_FEED_IN_VALUES,
                                           full_feed_in_day, sample_live, without_full_feed_in)
from solar_visor_hub.ui_card import card_sensors
from solar_visor_hub.ui_ha import catalog_view
from solar_visor_hub.widget_v2 import plant_widget

TZ = ZoneInfo("Europe/Berlin")
OK, STALE, IMPLAUSIBLE, UNKNOWN = MetricState.OK, MetricState.STALE, MetricState.IMPLAUSIBLE, MetricState.UNKNOWN


def point(entity, unit="W", role="authoritative", **extra):
    return {"entity": entity, "unit": unit, "sign": 1, "role": role, "valid_from": None, **extra}


def with_changes(raw, change):
    out = copy.deepcopy(raw)
    change(out["modules"])
    return out


def codes(raw):
    with pytest.raises(PlantError) as info:
        parse(raw)
    return {(p.path, p.code) for p in info.value.problems}


# --- plant model ---------------------------------------------------------------------------------------------------

def test_the_mark_is_written_only_when_set_and_survives_the_round_trip():
    plant = parse(FULL_FEED_IN)
    assert plant.modules["pv_2"].balance == "full_feed_in" and plant.modules["pv_2"].full_feed_in
    assert plant.modules["pv_1"].balance is None and not plant.modules["pv_1"].full_feed_in
    assert [m.id for m in plant.generators()] == ["pv_1"] and [m.id for m in plant.feed_in()] == ["pv_2"]
    out = dump(plant)
    assert out["modules"]["pv_2"]["balance"] == "full_feed_in" and "balance" not in out["modules"]["pv_1"]
    assert parse(out) == plant
    for raw in (MINIMAL, THREE_PV):                     # feeding the home is the default: never written
        dumped = dump(parse(raw))["modules"]
        assert not any("balance" in dumped[m] for m in ("pv_1", "pv_2", "pv_3") if m in dumped)
        assert parse(raw).feed_in() == []


def test_a_wrong_value_is_an_unknown_balance():
    raw = with_changes(FULL_FEED_IN, lambda m: m["pv_2"].__setitem__("balance", "beside_home"))
    assert codes(raw) == {("modules.pv_2.balance", "invalid_balance")}


def test_at_least_one_switched_on_generator_feeds_the_home():
    only_feed_in = with_changes(FULL_FEED_IN, lambda m: (m["pv_1"].__setitem__("balance", "full_feed_in"),
                                                         m["pv_2"].pop("balance")))
    assert [m.id for m in parse(only_feed_in).generators()] == ["pv_2"]       # pv_1 behind its own meter: fine
    off = with_changes(only_feed_in, lambda m: m["pv_2"].__setitem__("enabled", False))
    assert codes(off) == {("modules.pv_1.balance", "full_feed_in_needs_house_pv")}
    every = with_changes(FULL_FEED_IN, lambda m: m["pv_1"].__setitem__("balance", "full_feed_in"))
    assert codes(every) == {("modules.pv_1.balance", "full_feed_in_needs_house_pv")}
    # a switched-off generator keeps its mark and changes nothing
    parked = with_changes(FULL_FEED_IN, lambda m: m.__setitem__("pv_3", {
        "enabled": False, "name": "Carport", "balance": "full_feed_in", "points": {"power": point("sensor.sample_ffi_x")}}))
    plant = parse(parked)
    assert plant.feed_in() == [plant.modules["pv_2"]] and dump(plant)["modules"]["pv_3"]["balance"] == "full_feed_in"


def test_the_e3dc_details_never_name_a_generator_with_full_feed_in():
    on_feed_in = with_changes(FULL_FEED_IN, lambda m: m.__setitem__("e3dc_rscp", {"pv": "pv_2"}))
    assert codes(on_feed_in) == {("modules.e3dc_rscp.pv", "invalid_rscp_pv")}
    on_home = with_changes(FULL_FEED_IN, lambda m: m.__setitem__("e3dc_rscp", {"pv": "pv_1"}))
    assert parse(on_home).rscp_pv == "pv_1"


def test_source_keys_of_plants_without_the_mark_are_unchanged():
    """Frozen before the feature (plan F0): a changed key would recompute every archived day of every plant."""
    assert (source_key(parse(MINIMAL), TIER_RAW), source_key(parse(MINIMAL), TIER_HOUR)) == (
        "14e63dedd9849ddaedb7eebd", "ed0fa456f8b417739744919e")
    assert (source_key(parse(THREE_PV), TIER_RAW), source_key(parse(THREE_PV), TIER_HOUR)) == (
        "d472aa0b8aaceb2d4e8886e0", "6e4e2bbd00a404147ea79c43")
    # the mark changes the key of the plant that carries it – its days are computed anew
    assert source_key(parse(FULL_FEED_IN), TIER_RAW) != source_key(parse(without_full_feed_in(FULL_FEED_IN)), TIER_RAW)


def test_the_catalogue_offers_the_mark_for_every_generator():
    c = catalog_view()
    assert c["pv_balances"] == ["full_feed_in"] and c["generic_balances"] == ["part_of_home", "beside_home"]


# --- live ----------------------------------------------------------------------------------------------------------

def measured(raw):
    return with_changes(raw, lambda m: m.__setitem__("consumption", {"points": {
        "power": point("sensor.sample_ffi_consumption_power")}}))


HOME_METRICS = ("generation.power", "consumption.power", "home.power", "grid.power", "battery.power")


def live_of(raw, feed_in: str | None = "4800", reported_ago=timedelta(seconds=3)):
    values = {k: v for k, v in _FULL_FEED_IN_VALUES.items() if k != "pv_2.power"} | {"consumption.power": "750"}
    live = sample_live(raw, {k: v for k, v in values.items() if k in parse(raw).points()}, "full_feed_in")
    if feed_in is not None:
        entity = live.plant.points()["pv_2.power"].entity
        live.apply_state(entity, feed_in, "W", T - reported_ago, T, bootstrap=True)
    return live.snapshot(T)


@pytest.mark.parametrize("raw", [FULL_FEED_IN, measured(FULL_FEED_IN)], ids=["calculated", "measured"])
@pytest.mark.parametrize("feed_in, reported_ago, state", [
    ("0", timedelta(seconds=3), OK), ("5000", timedelta(seconds=3), OK), ("12000", timedelta(seconds=3), OK),
    ("unavailable", timedelta(seconds=3), UNKNOWN), (None, None, UNKNOWN), ("4800", timedelta(minutes=30), STALE),
], ids=["0 W", "5 kW", "12 kW", "unavailable", "never reported", "stale"])
def test_the_home_balance_does_not_see_the_generator_with_full_feed_in(raw, feed_in, reported_ago, state):
    base, snap = live_of(raw), live_of(raw, feed_in, reported_ago)
    for mid in HOME_METRICS:
        a, b = base.metrics[mid], snap.metrics[mid]
        assert (b.value, b.state, b.reasons, b.input_metric_ids) == (a.value, a.state, a.reasons, a.input_metric_ids), mid
    assert snap.metrics["generation.power"].value == 3100.0 and snap.metrics["generation.power"].input_metric_ids == ["pv_1.power"]
    assert snap.metrics["consumption.power"].value == 750.0 and snap.metrics["consumption.power"].state is OK
    assert (snap.balance_residual_w, snap.balance_comparable) == (base.balance_residual_w, base.balance_comparable)
    if "consumption.power" in parse(raw).points():
        assert snap.balance_residual_w == 0.0 and snap.balance_comparable
    # its own value, state and edge stay its own: into the grid, + = feeding in
    edges = {e.edge_id: e for e in snap.edges}
    assert [e.edge_id for e in snap.edges] == ["pv_1", "pv_2", "generation", "battery", "grid", "consumption", "home"]
    assert (edges["pv_2"].from_node, edges["pv_2"].to_node, edges["pv_2"].state) == ("pv_2", "grid", state)
    assert edges["pv_2"].power_w == (float(feed_in) if state is not UNKNOWN else None)
    assert (edges["pv_1"].to_node, edges["generation"].power_w) == ("generation", 3100.0)
    assert snap.metrics["pv_2.power"].state is state and snap.modules["pv_2"].state is state


def test_a_mark_on_a_generator_the_grid_sensor_sees_shows_as_negative_consumption():
    """Misconfiguration: the grid sensor measures the generator after all – the home 'consumes' less than nothing."""
    live = sample_live(FULL_FEED_IN, {**_FULL_FEED_IN_VALUES, "grid.power": "-6250"}, "full_feed_in")
    cons = live.snapshot(T).metrics["consumption.power"]
    assert cons.value == -4050.0 and cons.state is IMPLAUSIBLE and "negative_consumption" in cons.reasons


def test_the_sample_snapshot_and_capabilities_describe_the_generator():
    live = sample_live(FULL_FEED_IN, _FULL_FEED_IN_VALUES, "full_feed_in")
    snap = live.snapshot(T)
    assert snap.metrics["pv_2.power"].value == 4800.0 and snap.metrics["pv_2.energy_total"].value == 18_234_500.0
    assert snap.metrics["pv_2.string_1.power"].value == 2500.0
    assert set(snap.metrics) >= {"pv_2.power", "pv_2.dc_power", "pv_2.string_2.power", "generation.power"}


# --- energy --------------------------------------------------------------------------------------------------------

D = date(2026, 11, 15)


def flat_day(raw, feed_in_meter=True):
    """A day of constant powers: home generator 2 kW, feed-in meter +3 kW, grid −500 W, battery idle."""
    plant = parse(raw)
    start, end = period_bounds("day", D, TZ)
    step = timedelta(minutes=5)
    means = [StatRow(start + step * i, start + step * (i + 1), mean=2000.0) for i in range(int((end - start) / step))]
    sums = [StatRow(start - step, start, sum=1_000_000.0)] + [
        StatRow(r.start, r.end, sum=1_000_000.0 + 250.0 * (i + 1)) for i, r in enumerate(means)]
    held = [start - timedelta(minutes=10) + timedelta(minutes=30) * i for i in range(50)]
    data = {"pv_1.power": PointData(stats=means), "grid.power": PointData(raw=[EPoint(t, -500.0) for t in held]),
            "battery.power": PointData(raw=[EPoint(t, 0.0) for t in held])}
    if feed_in_meter:
        data["pv_2.energy_total"] = PointData(stats=sums)
    return compute_day(plant, quarter_hours(start, end, TZ), start, end, TIER_RAW, data)


def total(day, cat):
    values = [e.wh for e in day.categories[cat]]
    return None if any(v is None for v in values) else sum(values)


def test_the_day_keeps_the_generator_s_energy_out_of_the_home_balance():
    marked, before = flat_day(FULL_FEED_IN), flat_day(without_full_feed_in(FULL_FEED_IN))
    assert [e.wh for e in marked.categories["pv_2"]] == [e.wh for e in before.categories["pv_2"]]   # same reading
    assert total(marked, "pv_2") == pytest.approx(72_000) and total(marked, "pv_1") == pytest.approx(48_000)
    assert total(marked, "generation") == pytest.approx(48_000) and total(marked, "full_feed_in") == pytest.approx(72_000)
    assert marked.sources["full_feed_in"].method == "derived"
    assert total(marked, "consumption") == pytest.approx(36_000)                 # 2 kW − 0.5 kW export
    supply = sorted(c for c in marked.categories if c.startswith("supply."))
    assert supply == ["supply.battery", "supply.grid", "supply.pv_1"]
    assert sum(total(marked, c) for c in supply) == pytest.approx(total(marked, "consumption"))
    # the earlier revision: the generator fed the home, no derived feed-in category
    assert total(before, "generation") == pytest.approx(120_000) and "full_feed_in" not in before.categories
    assert total(before, "consumption") == pytest.approx(108_000) and "supply.pv_2" in before.categories
    # KPIs of the home: self-consumption = 1 − export / generation of the home
    p = plant_period("day", D, TZ, [(D, marked)], datetime(2026, 11, 16, 12, tzinfo=UTC), 2, False)
    assert p.kpis.self_consumption_pct == pytest.approx(75.0) and p.kpis.autarky_pct == pytest.approx(100.0)
    assert p.totals.energy_wh["full_feed_in"] == pytest.approx(72_000) and "supply.pv_2" not in p.totals.energy_wh
    assert p.kpis.generation_ring.total_wh == pytest.approx(48_000)


def test_an_unknown_feed_in_meter_leaves_the_home_known():
    day = flat_day(FULL_FEED_IN, feed_in_meter=False)
    assert total(day, "pv_2") is None and total(day, "full_feed_in") is None
    assert total(day, "generation") == pytest.approx(48_000) and total(day, "consumption") == pytest.approx(36_000)


@pytest.mark.parametrize("tier", [TIER_RAW, TIER_HOUR])
def test_the_generator_with_full_feed_in_is_still_read(tier):
    need = requirements(parse(FULL_FEED_IN), tier)
    assert need["pv_2.energy_total"] == {"sum"} and need["pv_1.power"] == {"mean"}


def test_power_series_of_the_home_leave_the_generator_out():
    plans = series_plans(parse(FULL_FEED_IN))
    assert plans["generation.power"].terms == (("pv_1.power", 1),)
    assert ("pv_2.power", 1) not in plans["consumption.power"].terms
    assert plans["consumption.power"].terms == (("pv_1.power", 1), ("grid.power", 1), ("battery.power", -1))
    assert plans["pv_2.power"].terms == (("pv_2.power", 1),) and not plans["pv_2.power"].derived


def test_the_card_draws_generation_from_the_home_s_generators_only():
    sensors = {node: [s.entity_id for s in v] for node, v in card_sensors(parse(FULL_FEED_IN)).items()}
    assert sensors["generation"] == ["sensor.sample_ffi_pv_power"]
    assert sensors["pv_2"] == ["sensor.sample_ffi_feed_in_s1_power", "sensor.sample_ffi_feed_in_s2_power",
                               "sensor.sample_ffi_feed_in_power"]


# --- end to end: read API ------------------------------------------------------------------------------------------

class History:
    def __init__(self):
        self.tz = TZ

    async def missing(self, dates):
        return []

    async def day(self, d):
        return full_feed_in_day(d)

    async def days(self, dates, progress=None):
        return [full_feed_in_day(d) for d in dates]


def test_a_plant_with_full_feed_in_through_live_and_energy(tmp_path):
    store = DeviceStore(tmp_path / "d.sqlite3")
    token, _ = store.enroll(store.create_pairing_code(), "t")
    live = sample_live(FULL_FEED_IN, _FULL_FEED_IN_VALUES, "full_feed_in")
    c = TestClient(create_public_app(devices=store, plant_live=live, plant_context={
        "timezone": lambda: "Europe/Berlin", "plant_history": History(), "jobs": JobManager()}))
    auth = {"Authorization": f"Bearer {token}"}

    caps = PlantCapabilities.model_validate(c.get("/v2/capabilities", headers=auth).json())
    by_id = {m.id: m for m in caps.modules}
    assert (by_id["pv_1"].balance, by_id["pv_2"].balance) == (None, "full_feed_in")
    assert [s.name for s in by_id["pv_2"].strings] == ["Süd 1", "Süd 2"] and caps.pv_peak_w == 8_400.0
    snap = PlantSnapshot.model_validate(c.get("/v2/snapshot", headers=auth).json())
    assert sorted(snap.metrics) == caps.metrics
    assert snap.metrics["generation.power"].value == 3100.0 and snap.metrics["consumption.power"].value == 750.0
    assert next(e for e in snap.edges if e.edge_id == "pv_2").to_node == "grid"

    day = PlantEnergyPeriod.model_validate(c.get("/v2/energy?period=day&date=2026-11-15", headers=auth).json())
    e = day.totals.energy_wh
    assert e["generation"] == e["pv_1"] == 14_400.0 and e["full_feed_in"] == e["pv_2"] == 23_400.0
    assert e["consumption"] == 13_950.0                     # 15 h × 750 W + 9 h × 300 W, without the generator
    assert e["supply.grid"] + e["supply.battery"] + e["supply.pv_1"] == pytest.approx(e["consumption"], abs=0.2)
    assert "supply.pv_2" not in e and day.complete
    assert day.kpis.autarky_pct == pytest.approx(round(100 * (1 - e["grid.import"] / e["consumption"]), 2))
    assert day.kpis.self_consumption_pct == pytest.approx(round(100 * (1 - e["grid.export"] / e["generation"]), 2))

    week = PlantEnergyPeriod.model_validate(c.get("/v2/energy?period=week&date=2026-11-15", headers=auth).json())
    w = week.totals.energy_wh
    assert w["generation"] + w["full_feed_in"] == pytest.approx(w["pv_1"] + w["pv_2"], abs=0.5)    # nothing twice
    assert w["full_feed_in"] == pytest.approx(5 * 23_400.0) and w["consumption"] == pytest.approx(7 * 13_950.0, abs=1)
    before = [b for b in week.buckets["full_feed_in"] if b.start < period_bounds("day", FULL_FEED_IN_SINCE, TZ)[0]]
    assert len(before) == 8 and all(b.energy_wh is None and "no_source" in b.quality_reasons for b in before)
    assert week.complete                                    # the derived category does not decide completeness


def test_the_widget_carries_the_feed_in_energy_of_today():
    live = sample_live(FULL_FEED_IN, _FULL_FEED_IN_VALUES, "full_feed_in")

    class Today(History):
        async def day(self, d):
            return full_feed_in_day(d, T)

    w = asyncio.run(plant_widget(live.snapshot(T), live.plant, Today(), T))
    assert w.today.energy_wh["generation"] == w.today.energy_wh["pv_1"] and w.today.energy_wh["full_feed_in"] > 0
    assert w.window.values["generation.power"].mean_w == w.window.values["pv_1.power"].mean_w
    assert w.live["pv_2.power"].value == 4800.0 and w.live["generation.power"].value == 3100.0
