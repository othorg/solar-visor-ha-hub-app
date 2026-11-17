"""Deterministic sample plants and their v2 answers for the contract fixtures (P2 detail plan step 4). Python, Swift
and Kotlin tests read the same files. Four shapes on purpose: the existing plant, the smallest possible plant, one
built differently in every optional part and one with a generator behind its own meter (full feed-in).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .api_v2 import build_capabilities, build_diagnostics
from .config import load_mapping
from .plant import Plant, parse
from .plant_legacy import import_legacy
from .plant_daycalc import PlantDay
from .plant_live import PlantLive

T = datetime(2026, 11, 15, 11, 30, tzinfo=UTC)
TIMEZONE = "Europe/Berlin"


def _point(entity: str, unit: str = "W", role: str = "authoritative", **extra: Any) -> dict[str, Any]:
    return {"entity": entity, "unit": unit, "sign": 1, "role": role, "valid_from": None, **extra}


MINIMAL: dict[str, Any] = {"schema": 1, "revision": 1, "profile": None, "modules": {
    "grid": {"enabled": True, "name": "Netz", "variant": "signed", "points": {"power": _point("sensor.sample_grid_power")}},
    "pv_1": {"enabled": True, "name": "PV", "points": {"power": _point("sensor.sample_pv_power", nonnegative=True)}},
}}

# split grid sensors, three generators, battery, no wallbox, calculated consumption, extra consumers of both kinds
THREE_PV: dict[str, Any] = {"schema": 1, "revision": 4, "profile": None, "pv_peak_w": 14_500, "modules": {
    "grid": {"enabled": True, "name": "Netz", "variant": "split", "points": {
        "import_power": _point("sensor.sample_import_power", nonnegative=True),
        "export_power": _point("sensor.sample_export_power", nonnegative=True),
        "import_energy": _point("sensor.sample_import_energy", "Wh", "cross_check"),
        "export_energy": _point("sensor.sample_export_energy", "Wh", "cross_check")}},
    "pv_1": {"enabled": True, "name": "Dach Süd", "points": {
        "power": _point("sensor.sample_pv_sued_power", nonnegative=True),
        "energy_total": _point("sensor.sample_pv_sued_energy", "Wh", "cross_check")},
        "strings": [{"id": "string_1", "name": "Ost", "points": {
            "power": _point("sensor.sample_pv_sued_s1_power", role="detail"),
            "voltage": _point("sensor.sample_pv_sued_s1_voltage", "V", "detail")}}]},
    "pv_2": {"enabled": True, "name": "Garage", "points": {"power": _point("sensor.sample_pv_garage_power", nonnegative=True)}},
    "pv_3": {"enabled": True, "name": "Balkon", "points": {"power": _point("sensor.sample_pv_balkon_power", nonnegative=True)}},
    "battery": {"enabled": True, "name": "Speicher", "points": {
        "power": _point("sensor.sample_battery_power"), "soc": _point("sensor.sample_battery_soc", "%", "detail")}},
    "wallbox": {"enabled": False, "name": "Wallbox", "trust": "none", "points": {
        "power": _point("sensor.sample_wallbox_power", role="subset")}},
    "generic_1": {"enabled": True, "name": "Wärmepumpe", "balance": "beside_home", "points": {
        "power": _point("sensor.sample_heatpump_power", role="subset", nonnegative=True),
        "energy_total": _point("sensor.sample_heatpump_energy", "Wh", "cross_check")}},
    "generic_2": {"enabled": True, "name": "Gartenhaus", "balance": "part_of_home", "points": {
        "power": _point("sensor.sample_garden_power", role="subset", nonnegative=True),
        "storage_soc": _point("sensor.sample_garden_soc", "%", "detail")}},
}}

_OWN_VALUES = {"pv_1.power": "3200", "pv_2.power": "1800", "pv_2.ac_power": "1790", "battery.power": "-400",
               "battery.soc": "64", "grid.power": "-1200", "consumption.power": "4200", "wallbox.power": "0",
               "wallbox.plugged": "off", "generic_1.power": "210", "generic_1.storage_power": "35",
               "generic_1.storage_soc": "88", "generic_1.status": "not_connected", "pv_1.string_1.power": "1650",
               "pv_2.string_1.power": "700"}
# a home generator with battery and a second generator with full feed-in (own meter, the grid sensor never sees it);
# at noon the feed-in generator delivers more than the home's – counting it into the home would show at once
FULL_FEED_IN: dict[str, Any] = {"schema": 1, "revision": 2, "profile": None, "pv_peak_w": 8_400, "modules": {
    "grid": {"enabled": True, "name": "Netz", "variant": "signed", "points": {"power": _point("sensor.sample_ffi_grid_power")}},
    "pv_1": {"enabled": True, "name": "Hausdach", "points": {
        "power": _point("sensor.sample_ffi_pv_power", nonnegative=True)},
        "strings": [{"id": "string_1", "name": "Ost", "points": {"power": _point("sensor.sample_ffi_pv_s1_power", role="detail")}},
                    {"id": "string_2", "name": "West", "points": {"power": _point("sensor.sample_ffi_pv_s2_power", role="detail")}}]},
    "pv_2": {"enabled": True, "name": "Scheune", "balance": "full_feed_in", "points": {
        "power": _point("sensor.sample_ffi_feed_in_power", nonnegative=True),
        "dc_power": _point("sensor.sample_ffi_feed_in_dc_power", role="detail"),
        "energy_total": _point("sensor.sample_ffi_feed_in_energy", "Wh", "cross_check")},
        "strings": [{"id": "string_1", "name": "Süd 1", "points": {"power": _point("sensor.sample_ffi_feed_in_s1_power", role="detail")}},
                    {"id": "string_2", "name": "Süd 2", "points": {"power": _point("sensor.sample_ffi_feed_in_s2_power", role="detail")}}]},
    "battery": {"enabled": True, "name": "Speicher", "points": {
        "power": _point("sensor.sample_ffi_battery_power", sign=-1),          # the device counts + as discharging
        "soc": _point("sensor.sample_ffi_battery_soc", "%", "detail")}},
}}

_MINIMAL_VALUES = {"grid.power": "-640", "pv_1.power": "1.42"}             # the generator reports in kW
_THREE_PV_VALUES = {"grid.import_power": "0", "grid.export_power": "2150", "pv_1.power": "4100", "pv_2.power": "1900",
                    "pv_3.power": "380", "battery.power": "900", "battery.soc": "71", "generic_1.power": "1150",
                    "generic_2.power": "95", "grid.import_energy": "8123.4", "pv_1.string_1.voltage": "612.5"}
# home: 3100 W generation − 1450 W export − 900 W charging = 750 W consumption; the feed-in generator's 4800 W are
# beside it (counted in, the consumption would read 5550 W)
_FULL_FEED_IN_VALUES = {"grid.power": "-1450", "pv_1.power": "3100", "pv_1.string_1.power": "1600",
                        "pv_1.string_2.power": "1500", "pv_2.power": "4.8", "pv_2.dc_power": "4950",
                        "pv_2.energy_total": "18234.5", "pv_2.string_1.power": "2500", "pv_2.string_2.power": "2450",
                        "battery.power": "-900", "battery.soc": "55"}
_UNITS = {"pv_1.power@minimal": "kW", "grid.import_energy": "kWh", "pv_2.power@full_feed_in": "kW",
          "pv_2.energy_total@full_feed_in": "kWh"}


def own_plant() -> dict[str, Any]:
    """The existing plant as the import produces it; entity names never reach a fixture (v2 does not carry them)."""
    return {**import_legacy(load_mapping()), "revision": 1}


def sample_live(raw: dict[str, Any], values: dict[str, str], shape: str = "") -> PlantLive:
    plant: Plant = parse(raw)
    live = PlantLive(plant)
    live.set_connected(True, T)
    for metric_id, value in values.items():
        point = plant.points()[metric_id]
        unit = _UNITS.get(f"{metric_id}@{shape}") or _UNITS.get(metric_id) or point.unit.value
        attr = None if point.unit.value in ("bool", "text") else unit
        live.apply_state(point.entity, value, attr, T - timedelta(seconds=3), T, bootstrap=True)
    return live


def samples() -> dict[str, dict[str, Any]]:
    """Fixture name → JSON content."""
    out: dict[str, dict[str, Any]] = {}
    shapes = {"own": (own_plant(), _OWN_VALUES), "minimal": (MINIMAL, _MINIMAL_VALUES), "three_pv": (THREE_PV, _THREE_PV_VALUES),
              "full_feed_in": (FULL_FEED_IN, _FULL_FEED_IN_VALUES)}
    for shape, (raw, values) in shapes.items():
        live = sample_live(raw, values, shape)
        snap = live.snapshot(T).model_copy(update={"snapshot_id": f"sample-{shape}"})
        out[f"v2_snapshot_{shape}"] = snap.model_dump(mode="json")
        out[f"v2_capabilities_{shape}"] = build_capabilities(live.plant, TIMEZONE, rscp=shape == "own", history=True,
                                                             consumers=shape == "own",
                                                             compare=shape == "own").model_dump(mode="json")
    own = sample_live(own_plant(), _OWN_VALUES, "own")
    out["v2_diagnostics_own"] = build_diagnostics(own, None, T, TIMEZONE).model_dump(mode="json")
    out |= energy_samples()
    return out


def energy_samples() -> dict[str, dict[str, Any]]:
    """/v2/energy for the existing plant (the v1 fixtures of the reference day, renamed) and for the minimal plant
    (generic calculation on synthetic recorder data: a sunny day, export at noon, an hour-tier day for the week)."""
    import json
    from datetime import date

    from .contract import CONTRACTS_DIR
    from .energy import Point as EPoint
    from .ha_history import StatRow
    from .history_v2 import from_legacy, plant_period
    from .models import EnergyPeriod
    from .periods import period_bounds, quarter_hours, zone
    from .plant_daycalc import TIER_HOUR, TIER_RAW, PointData, compute_day

    out: dict[str, dict[str, Any]] = {}
    from .models import PowerResponse
    from .plant_power import LEGACY_SERIES
    from .plant_power import from_legacy as power_from_legacy
    v1_power = PowerResponse.model_validate(json.loads((CONTRACTS_DIR / "fixtures" / "power_day_2026-10-03.json").read_text()))
    ids = [i for i, (v1, _) in LEGACY_SERIES.items() if v1 in {s.series_id for s in v1_power.series}]
    out["v2_power_day_own"] = power_from_legacy(v1_power, ids).model_dump(mode="json")
    from .models import WidgetSnapshot
    from .widget_v2 import from_legacy as widget_from_legacy
    v1_widget = WidgetSnapshot.model_validate(json.loads((CONTRACTS_DIR / "fixtures" / "widget_2026-10-03.json").read_text()))
    own_live = sample_live(own_plant(), _OWN_VALUES, "own")
    out["v2_widget_own"] = widget_from_legacy(v1_widget, own_live.snapshot(T), own_live.plant).model_dump(mode="json")
    from .consumers_v2 import from_legacy as consumers_from_legacy
    from .models import HomeConsumers
    v1_consumers = HomeConsumers.model_validate(json.loads((CONTRACTS_DIR / "fixtures" / "consumers_sample.json").read_text()))
    out["v2_consumers_own"] = consumers_from_legacy(v1_consumers).model_dump(mode="json")
    from .comparison_v2 import from_legacy as comparison_from_legacy
    from .models import EnergyComparison
    v1_compare = EnergyComparison.model_validate(json.loads((CONTRACTS_DIR / "fixtures" / "compare_month_running.json").read_text()))
    out["v2_compare_month_own"] = comparison_from_legacy(v1_compare).model_dump(mode="json")
    for name in ("energy_day_2026-10-03", "energy_week_2026-10-03"):
        v1 = EnergyPeriod.model_validate(json.loads((CONTRACTS_DIR / "fixtures" / f"{name}.json").read_text()))
        out[f"v2_{name.replace('2026-10-03', 'own')}"] = from_legacy(v1, 1).model_dump(mode="json")

    tz = zone(TIMEZONE)
    plant = parse(MINIMAL)
    now = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)

    def sun(t: datetime) -> float:
        h = t.astimezone(tz).hour + t.astimezone(tz).minute / 60
        return max(0.0, 2400.0 * (1 - abs(h - 12.5) / 4.5))       # 08:00–17:00, peak 2.4 kW

    def house(t: datetime) -> float:
        h = t.astimezone(tz).hour
        return 900.0 if 7 <= h < 22 else 350.0

    def day(d: date, tier: str):
        start, end = period_bounds("day", d, tz)
        step = timedelta(minutes=5) if tier == TIER_RAW else timedelta(hours=1)
        rows, t = [], start
        while t < end:
            rows.append(StatRow(t, t + step, mean=round(sun(t + step / 2), 1)))
            t += step
        grid, t = [EPoint(start - timedelta(minutes=10), house(start))], start
        while t < end:
            grid.append(EPoint(t, round(house(t) - sun(t), 1)))
            t += timedelta(minutes=1)
        grid_rows = [StatRow(r.start, r.end, mean=round(house(r.start) - (r.mean or 0.0), 1)) for r in rows]
        data = {"pv_1.power": PointData(stats=rows),
                "grid.power": PointData(raw=grid) if tier == TIER_RAW else PointData(stats=grid_rows)}
        return compute_day(plant, quarter_hours(start, end, tz), start, end, tier, data)

    d = date(2026, 11, 15)
    out["v2_energy_day_minimal"] = plant_period("day", d, tz, [(d, day(d, TIER_RAW))], now, 1, False).model_dump(mode="json")
    week = [date(2026, 11, 9) + timedelta(days=i) for i in range(7)]
    loaded = [(dd, day(dd, TIER_HOUR if dd < date(2026, 11, 13) else TIER_RAW)) for dd in week]
    out["v2_energy_week_minimal"] = plant_period("week", d, tz, loaded, now, 1, False).model_dump(mode="json")
    return out | full_feed_in_samples()


def without_full_feed_in(raw: dict[str, Any]) -> dict[str, Any]:
    """The same plant before its generator got its own meter: every PV feeds the home (an earlier revision)."""
    import copy
    out = copy.deepcopy(raw)
    for module in out["modules"].values():
        if isinstance(module, dict) and module.get("balance") == "full_feed_in":
            del module["balance"]
    return {**out, "revision": raw["revision"] - 1}


FULL_FEED_IN_SINCE = date(2026, 11, 11)          # first day of the revision with full feed-in (the week before: without)


def _ffi_hour(t: datetime) -> float:
    local = t.astimezone(ZoneInfo(TIMEZONE))
    return local.hour + local.minute / 60


def _ffi_sun(t: datetime, peak: float) -> float:
    return max(0.0, peak * (1 - abs(_ffi_hour(t) - 12.5) / 4.5))      # 08:00–17:00


def _ffi_house(t: datetime) -> float:
    return 750.0 if 7 <= _ffi_hour(t) < 22 else 300.0


def _ffi_home_pv(t: datetime) -> float:
    return _ffi_sun(t, 3200.0)


def _ffi_feed_in(t: datetime) -> float:
    return _ffi_sun(t, 5200.0)                          # more than the home's generator at noon


def _ffi_battery(t: datetime) -> float:
    """+ charging with the home's surplus (at most 1.2 kW), − discharging in the evening."""
    surplus = _ffi_home_pv(t) - _ffi_house(t)
    if surplus > 0:
        return min(surplus, 1200.0)
    return -400.0 if 18 <= _ffi_hour(t) < 23 else 0.0


def _ffi_grid(t: datetime, sees_feed_in: bool) -> float:
    return _ffi_house(t) - _ffi_home_pv(t) + _ffi_battery(t) - (_ffi_feed_in(t) if sees_feed_in else 0.0)


def full_feed_in_day(d: date, until: datetime | None = None) -> PlantDay:
    """A day of the full feed-in plant from synthetic recorder data (raw tier: 5-minute means of the home's generator,
    5-minute sums of the feed-in meter, raw states of grid and battery) – up to ``until`` for the running day. Before
    ``FULL_FEED_IN_SINCE`` the plant is the earlier revision without the mark: the generator fed the home and the grid
    sensor saw it. The home consumes 750 W by day and 300 W at night either way."""
    from .energy import Point as EPoint
    from .ha_history import StatRow
    from .periods import period_bounds, quarter_hours
    from .plant_daycalc import TIER_RAW, PointData, compute_day

    tz = ZoneInfo(TIMEZONE)
    sees = d < FULL_FEED_IN_SINCE
    plant = parse(without_full_feed_in(FULL_FEED_IN) if sees else FULL_FEED_IN)
    start, end = period_bounds("day", d, tz)
    until = min(until or end, end)
    step = timedelta(minutes=5)
    total = 18_000_000.0 + (d - date(2026, 11, 1)).days * 25_000.0     # the meter at the start of the day
    means, sums, t = [], [StatRow(start - step, start, sum=total)], start
    while t < until:
        mid = t + step / 2
        means.append(StatRow(t, t + step, mean=round(_ffi_home_pv(mid), 1)))
        total += _ffi_feed_in(mid) * step.total_seconds() / 3600
        sums.append(StatRow(t, t + step, sum=round(total, 1)))
        t += step
    carry_in = start - timedelta(minutes=10)
    grid, battery, t = [EPoint(carry_in, _ffi_grid(start, sees))], [EPoint(carry_in, _ffi_battery(start))], start
    while t < until:
        grid.append(EPoint(t, round(_ffi_grid(t, sees), 1)))             # canonical: + import
        battery.append(EPoint(t, round(_ffi_battery(t), 1)))             # canonical: + charging
        t += timedelta(minutes=1)
    data = {"pv_1.power": PointData(stats=means), "pv_2.energy_total": PointData(stats=sums),
            "grid.power": PointData(raw=grid), "battery.power": PointData(raw=battery)}
    return compute_day(plant, quarter_hours(start, end, tz), start, until, TIER_RAW, data)


def full_feed_in_samples() -> dict[str, dict[str, Any]]:
    """/v2/energy (day, week) and /v2/widget of the full feed-in plant. The week starts with two days of the earlier
    revision without the mark: the week where the plant was switched, as the apps have to show it. Every day lies in
    the recorder's keep window (raw tier), so directions and supply are known on both sides of the switch."""
    import asyncio

    from .history_v2 import plant_period
    from .widget_v2 import plant_widget

    tz = ZoneInfo(TIMEZONE)
    revision = parse(FULL_FEED_IN).revision
    now = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
    d = date(2026, 11, 15)
    week = [date(2026, 11, 9) + timedelta(days=i) for i in range(7)]
    out = {
        "v2_energy_day_full_feed_in": plant_period("day", d, tz, [(d, full_feed_in_day(d))], now, revision, False),
        "v2_energy_week_full_feed_in": plant_period("week", d, tz, [(dd, full_feed_in_day(dd)) for dd in week], now,
                                                    revision, False),
    }

    class History:                                      # the plant's history service as plant_widget reads it
        def __init__(self) -> None:
            self.tz = tz

        async def day(self, dd: date) -> PlantDay:
            return full_feed_in_day(dd, T)

    live = sample_live(FULL_FEED_IN, _FULL_FEED_IN_VALUES, "full_feed_in")
    out["v2_widget_full_feed_in"] = asyncio.run(plant_widget(live.snapshot(T), live.plant, History(), T))
    return {name: value.model_dump(mode="json") for name, value in out.items()}
