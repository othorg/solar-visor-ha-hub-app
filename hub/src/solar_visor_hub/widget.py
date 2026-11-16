"""Compact snapshot for complications and widgets (P6, plan §4.1): live values with their measurement time, the
last completed quarter hour from the energy engine's power columns, SOC with its time, today's energy and a 24-h
sparkline. A failing history part never hides the live values; a fresh window end never makes old data look fresh
(``measured_until`` per series)."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, date, datetime, timedelta

from .energy_api import energy_period, overlapping_days
from .epochs import CALCULATION_VERSION
from .history import EnergyService
from .models import (
    BalanceProfile,
    MetricState,
    Snapshot,
    WidgetLive,
    WidgetSnapshot,
    WidgetSoc,
    WidgetSparkline,
    WidgetToday,
    WidgetWindow,
    WidgetWindowValue,
)
from .periods import TZ
from .power_history import DayColumns, points, window_stats

log = logging.getLogger(__name__)
LIVE_METRICS = ("generation_total", "consumption_total", "home_power", "grid_power", "battery_power", "pv_internal",
                "kostal_balance", "wallbox_power", "soc")
# history series → widget key and sign (history battery + = discharge, live/widget battery + = charging)
WINDOW_SERIES = {"generation": ("generation", 1), "consumption": ("consumption", 1), "home": ("home", 1),
                 "grid": ("grid", 1), "battery": ("battery", -1), "pv_internal": ("pv_internal", 1),
                 "external": ("external", 1)}
QUARTER = timedelta(minutes=15)
SPARK_STEP = timedelta(minutes=30)


def _floor(t: datetime, step: timedelta) -> datetime:
    s = step.total_seconds()
    return datetime.fromtimestamp((t.timestamp() // s) * s, UTC)


# today's motorhome energy at most every 5 min: the extra columns (strings, KOSTAL DC, motorhome) are recomputed after
# 60 s, and widget snapshots are asked for every minute while an app is open
WOMO_REFRESH = timedelta(minutes=5)
# own short deadline: the optional motorhome line never delays the snapshot (clients give up after 8 s); the extra
# columns keep computing in the background (shielded in EnergyService) for the next request
WOMO_DEADLINE_S = 1.5
_womo_cache: dict[str, tuple[datetime, tuple[float | None, float | None]]] = {}


async def _womo(service: EnergyService, local: date, now: datetime) -> tuple[float | None, float | None]:
    key = local.isoformat()
    hit = _womo_cache.get(key)
    if hit is not None and now - hit[0] < WOMO_REFRESH:
        return hit[1]
    try:
        cols = await asyncio.wait_for(service.extra_columns(local), WOMO_DEADLINE_S)
    except TimeoutError:
        log.info("widget snapshot: motorhome energy not ready within %.1f s", WOMO_DEADLINE_S)
        return hit[1] if hit is not None else (None, None)        # the last value of today, else unknown
    value = womo_today(cols)
    _womo_cache.clear()                                   # one day only – yesterday's entry never lingers
    _womo_cache[key] = (now, value)
    return value


def womo_today(cols: DayColumns | None) -> tuple[float | None, float | None]:
    """Motorhome shore power of the day so far: (Wh over the covered time, covered share of the applicable time);
    (None, None) without the series or anything applicable, Wh None while nothing is covered (unknown, never 0)."""
    s = cols.series.get("womo_shore") if cols is not None else None
    if s is None:
        return None, None
    energy, covered, applicable = sum(s.energy), sum(s.covered), sum(s.applicable)
    if applicable <= 0:
        return None, None
    return (round(energy / 3600.0, 1) if covered > 0.5 else None), round(min(1.0, covered / applicable), 4)


def live_part(snap: Snapshot | None) -> tuple[dict[str, WidgetLive], WidgetSoc | None]:
    live: dict[str, WidgetLive] = {}
    soc = None
    for mid in LIVE_METRICS:
        m = snap.metrics.get(mid) if snap else None
        if m is None:
            live[mid] = WidgetLive(value=None, state=MetricState.UNKNOWN)
            continue
        value = m.value if isinstance(m.value, (int, float)) and not isinstance(m.value, bool) else None
        usable = m.state in (MetricState.OK, MetricState.STALE)
        live[mid] = WidgetLive(value=value if usable else None, state=m.state, observed_at=m.reported_at or m.observed_at)
        if mid == "soc" and usable and value is not None:
            soc = WidgetSoc(value=value, observed_at=m.reported_at or m.observed_at)
    return live, soc


async def widget_snapshot(snap: Snapshot | None, service: EnergyService | None, now: datetime,
                          errors: tuple[type[BaseException], ...] = (), profile: BalanceProfile = BalanceProfile.E3DC_BALANCE
                          ) -> WidgetSnapshot:
    live, soc = live_part(snap)
    window = today = spark = None
    if service is not None:
        try:
            w_end = _floor(now, QUARTER)
            w_start = w_end - QUARTER
            s_end = _floor(now, SPARK_STEP)
            s_start = s_end - timedelta(hours=24)
            days = [await service.power_columns(d, profile) for d in overlapping_days(s_start, max(w_end, s_end))]
            values = {}
            for key, (sid, sign) in WINDOW_SERIES.items():
                mean, cov, until = window_stats(days, sid, w_start, w_end)
                values[key] = WidgetWindowValue(mean_w=None if mean is None else round(sign * mean, 1), coverage=cov,
                                                measured_until=until)
            window = WidgetWindow(start=w_start, end=w_end, values=values)
            gen = points(days, "generation", s_start, s_end, int(SPARK_STEP.total_seconds()))
            cons = points(days, "consumption", s_start, s_end, int(SPARK_STEP.total_seconds()))
            spark = WidgetSparkline(start=s_start, resolution_s=int(SPARK_STEP.total_seconds()),
                                    generation_w=[p.mean for p in gen], consumption_w=[p.mean for p in cons])
            local = now.astimezone(TZ).date()
            period = await energy_period(service, "day", local, profile, now)
            t, k = period.totals, period.kpis
            today = WidgetToday(
                local_date=local.isoformat(),
                energy_wh={"pv_internal": t.pv_internal_wh, "external": t.external_wh, "generation": t.generation_wh,
                           "consumption": t.consumption_wh, "home": t.home_wh, "wallbox": t.wallbox_wh,
                           "battery_charge": t.battery_charge_wh, "battery_discharge": t.battery_discharge_wh,
                           "grid_import": t.grid_import_wh, "grid_export": t.grid_export_wh},
                coverage=dict(t.coverage), autarky_pct=k.autarky_pct, self_consumption_pct=k.self_consumption_pct,
                autarky_coverage=k.autarky_coverage, self_consumption_coverage=k.self_consumption_coverage,
                complete=period.complete)
            try:
                womo_wh, womo_cov = await _womo(service, local, now)
                today = today.model_copy(update={"womo_wh": womo_wh, "womo_coverage": womo_cov})
            except Exception as exc:   # noqa: BLE001 – an extra series never hides the day's totals
                log.warning("widget snapshot: motorhome energy unavailable – %s", type(exc).__name__)
        except errors:
            log.warning("widget snapshot: history unavailable – live part only")
    return WidgetSnapshot(generated_at=now, calculation_version=CALCULATION_VERSION, live=live, window=window, soc=soc,
                          today=today, sparkline=spark)
