"""Widget snapshot for /v2 (plan ha-addon P3 step 7): live values of the plant, the last completed quarter hour, SOC,
today's energy and a 24-h sparkline – all under v2 ids, signs as in the live snapshot (battery + charging,
grid + import). The live part comes from the plant's own evaluation in both modes; the history part from the existing
calculation (imported plant, renamed) or from the plant's history service. A failing history part never hides the
live values."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from .history_v2 import measured
from .kpis import FULL
from .models import MetricState, WidgetLive, WidgetSnapshot, WidgetSoc, WidgetSparkline, WidgetWindow, WidgetWindowValue
from .models_v2 import PlantSnapshot, PlantWidgetSnapshot, PlantWidgetToday
from .plant import Kind, Plant
from .plant_archive import CALCULATION_VERSION as GENERIC_VERSION

log = logging.getLogger(__name__)
QUARTER = timedelta(minutes=15)
SPARK_STEP = timedelta(minutes=30)
WINDOW_FROM_LEGACY = {"generation": "generation.power", "consumption": "consumption.power", "home": "home.power",
                      "grid": "grid.power", "battery": "battery.power", "pv_internal": "pv_1.power",
                      "external": "pv_2.power"}
TODAY_FROM_LEGACY = {"pv_internal": "pv_1", "external": "pv_2", "generation": "generation", "consumption": "consumption",
                     "home": "home", "wallbox": "wallbox", "battery_charge": "battery.charge",
                     "battery_discharge": "battery.discharge", "grid_import": "grid.import", "grid_export": "grid.export"}


def live_ids(plant: Plant) -> list[str]:
    ids = ["generation.power", "consumption.power", "home.power", "grid.power"]
    for m in plant.active():
        if m.kind in (Kind.PV, Kind.WALLBOX, Kind.GENERIC) or m.kind is Kind.BATTERY:
            ids.append(f"{m.id}.power")
    if "battery.soc" in plant.points():
        ids.append("battery.soc")
    return list(dict.fromkeys(ids))


def live_part(snap: PlantSnapshot, plant: Plant) -> tuple[dict[str, WidgetLive], WidgetSoc | None]:
    live: dict[str, WidgetLive] = {}
    soc = None
    for mid in live_ids(plant):
        m = snap.metrics.get(mid)
        if m is None:
            live[mid] = WidgetLive(value=None, state=MetricState.UNKNOWN)
            continue
        value = m.value if isinstance(m.value, (int, float)) and not isinstance(m.value, bool) else None
        usable = m.state in (MetricState.OK, MetricState.STALE)
        live[mid] = WidgetLive(value=value if usable else None, state=m.state, observed_at=m.reported_at or m.observed_at)
        if mid == "battery.soc" and usable and value is not None:
            soc = WidgetSoc(value=value, observed_at=m.reported_at or m.observed_at)
    return live, soc


def from_legacy(w: WidgetSnapshot, snap: PlantSnapshot, plant: Plant) -> PlantWidgetSnapshot:
    """History part of the existing calculation's widget snapshot under v2 ids; the live part from the plant."""
    live, soc = live_part(snap, plant)
    window = None
    if w.window is not None:
        window = WidgetWindow(start=w.window.start, end=w.window.end,
                              values={WINDOW_FROM_LEGACY[k]: v for k, v in w.window.values.items() if k in WINDOW_FROM_LEGACY})
    today = None
    if w.today is not None:
        t = w.today
        energy = {TODAY_FROM_LEGACY[k]: measured(v, t.coverage.get(k)) for k, v in t.energy_wh.items()
                  if k in TODAY_FROM_LEGACY}
        coverage = {TODAY_FROM_LEGACY[k]: v for k, v in t.coverage.items() if k in TODAY_FROM_LEGACY}
        if t.womo_wh is not None or t.womo_coverage is not None:
            energy["generic_1"], coverage["generic_1"] = measured(t.womo_wh, t.womo_coverage), t.womo_coverage
        today = PlantWidgetToday(local_date=t.local_date, energy_wh=energy, coverage=coverage, autarky_pct=t.autarky_pct,
                                 self_consumption_pct=t.self_consumption_pct, autarky_coverage=t.autarky_coverage,
                                 self_consumption_coverage=t.self_consumption_coverage, complete=t.complete)
    return PlantWidgetSnapshot(generated_at=w.generated_at, timezone=w.timezone, plant_revision=plant.revision,
                               calculation_version=w.calculation_version, live=live, window=window, soc=soc, today=today,
                               sparkline=w.sparkline)


def _floor(t: datetime, step: timedelta) -> datetime:
    s = step.total_seconds()
    return datetime.fromtimestamp((t.timestamp() // s) * s, UTC)


async def plant_widget(snap: PlantSnapshot, plant: Plant, history: Any, now: datetime,
                       errors: tuple[type[BaseException], ...] = ()) -> PlantWidgetSnapshot:
    """Any other plant: the history part from the plant's days (archive or computed, refreshed every few minutes)."""
    from .history_v2 import plant_period
    from .periods import period_bounds, quarter_hours

    live, soc = live_part(snap, plant)
    window = today = spark = None
    if history is not None:
        try:
            tz = history.tz
            local = now.astimezone(tz).date()
            yesterday = local - timedelta(days=1)
            days = {d: await history.day(d) for d in (yesterday, local)}
            slots = {d: quarter_hours(*period_bounds("day", d, tz), tz) for d in days}
            flat = [(sl, d, i) for d in (yesterday, local) for i, sl in enumerate(slots[d])]

            def avg_w(cat: str, a: datetime, b: datetime) -> tuple[float | None, float]:
                """Mean power over the covered part of [a, b) – as /v1: the energy of a quarter hour not computed yet
                (the running day is refreshed every few minutes) must not count as 0 W (Codex P3 review Z1)."""
                parts = [days[d].categories.get(cat, [None] * len(slots[d]))[i] for sl, d, i in flat if a <= sl.start < b]
                parts = [p for p in parts if p is not None]
                known = [p for p in parts if p.wh is not None]
                covered = sum(p.covered_s for p in known)
                if not known or covered <= 0:
                    return None, 0.0
                cov = covered / ((b - a).total_seconds() or 1)
                return round(sum(p.wh for p in known) * 3600 / covered, 1), round(min(1.0, cov), 4)    # type: ignore[misc]

            w_end = _floor(now, QUARTER)
            w_start = w_end - QUARTER
            cats = {"generation.power": "generation", "consumption.power": "consumption", "home.power": "home",
                    "grid.power": "grid.net"}
            if plant.modules["battery"].enabled:
                cats["battery.power"] = "battery.net"
            for m in plant.active():
                if m.kind in (Kind.PV, Kind.WALLBOX, Kind.GENERIC):
                    cats[f"{m.id}.power"] = m.id
            values = {}
            for key, cat in cats.items():
                mean, cov = avg_w(cat, w_start, w_end)
                values[key] = WidgetWindowValue(mean_w=mean, coverage=cov, measured_until=w_end if cov >= FULL else None)
            window = WidgetWindow(start=w_start, end=w_end, values=values)
            s_end = _floor(now, SPARK_STEP)
            s_start = s_end - timedelta(hours=24)
            steps = [s_start + SPARK_STEP * i for i in range(48)]
            spark = WidgetSparkline(start=s_start, resolution_s=int(SPARK_STEP.total_seconds()),
                                    generation_w=[avg_w("generation", t, t + SPARK_STEP)[0] for t in steps],
                                    consumption_w=[avg_w("consumption", t, t + SPARK_STEP)[0] for t in steps])
            p = plant_period("day", local, tz, [(local, days[local])], now, plant.revision, "consumption.power" in plant.points())
            k = p.kpis
            today = PlantWidgetToday(local_date=local.isoformat(), energy_wh=p.totals.energy_wh, coverage=p.totals.coverage,
                                     autarky_pct=k.autarky_pct, self_consumption_pct=k.self_consumption_pct,
                                     autarky_coverage=k.autarky_coverage, self_consumption_coverage=k.self_consumption_coverage,
                                     complete=p.complete)
        except errors:
            log.warning("widget snapshot: history unavailable – live part only")
    return PlantWidgetSnapshot(generated_at=now, timezone=history.tz.key if history is not None else "UTC",
                               plant_revision=plant.revision, calculation_version=GENERIC_VERSION, live=live,
                               window=window, soc=soc, today=today, sparkline=spark)
