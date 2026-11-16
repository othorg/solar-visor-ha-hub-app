"""``GET /v1/consumers``: the HA energy dashboard's consumers for the home detail sheet (plan home-consumers 2026-10-06).

Now: the subscribed power sensors (stale without a report for 15 min); the wallbox and the motorhome from the hub's
checked live metrics (an old retained openWB value never shows up here). Today: HA hourly statistics up to the last
compiled hour, capped by what the energy engine has surely computed; a consumer whose rows do not cover the period is
a known partial sum ("incomplete"), a negative hour (a counter reset HA did not recognise) makes it unknown. The wallbox
energy comes from the engine (its HA daily counter has no last_reset – negative midnight changes).
Not tracked: engine consumption over the same period − Σ measured consumers (like HA: what nothing measures is
untracked; the number of consumers without a full value is reported), only if the engine covers the period completely.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from .consumers import (
    UNTRACKED_TOLERANCE_W,
    UNTRACKED_TOLERANCE_WH,
    ConsumerSpec,
    counted,
    display_name,
    stat_time,
    untracked,
)
from .history import WRITE_LATENCY, DayData, EnergyService
from .kpis import FULL
from .live import LiveModel
from .models import BalanceProfile, ConsumersRest, HomeConsumer, HomeConsumers, MetricState
from .periods import TZ, local_midnight_utc, period_bounds, quarter_hours

log = logging.getLogger(__name__)
STATS_TTL = timedelta(seconds=120)


@dataclass
class _Stats:
    key: tuple[date, tuple[str, ...]]
    fetched_at: datetime
    rows: dict


def _floor_hour(t: datetime) -> datetime:
    return t.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def _rest(r) -> ConsumersRest:
    return ConsumersRest(value=None if r.value is None else round(r.value, 1), reason=r.reason, unmeasured=r.unmeasured)


class ConsumersService:
    def __init__(self, ha, live: LiveModel, energy: EnergyService | None,
                 now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._ha, self._live, self._energy, self._now = ha, live, energy, now
        self._stats: _Stats | None = None

    async def _statistics(self, ids: list[str], start: datetime, day: date, now: datetime) -> dict | None:
        """Cached per local day and catalog (never yesterday's energy after midnight, never mixed catalogs)."""
        key = (day, tuple(ids))
        if self._stats and self._stats.key == key and now - self._stats.fetched_at < STATS_TTL:
            return self._stats.rows
        try:
            rows = await self._ha.consumer_statistics(ids, start)
        except (ConnectionError, TimeoutError, RuntimeError, OSError) as exc:
            log.warning("HA statistics: %s", type(exc).__name__)
            return None
        self._stats = _Stats(key, now, rows)
        return rows

    async def _engine_day(self, day: date, profile: BalanceProfile) -> DayData | None:
        if self._energy is None:
            return None
        try:
            return await self._energy.day(day, profile)
        except Exception as exc:   # noqa: BLE001 – InfluxDB down: no rest, the consumers stay
            log.warning("energy engine for consumers: %s", type(exc).__name__)
            return None

    @staticmethod
    def _engine_sum(data: DayData | None, day: date, category: str, until: datetime) -> float | None:
        """Energy of a category over [local midnight, until) – only if every slot is fully computed and covered."""
        if data is None:
            return None
        slots = quarter_hours(*period_bounds("day", day))
        parts = [e for s, e in zip(slots, data.categories.get(category) or [], strict=False) if s.end <= until]
        expected = sum(1 for s in slots if s.end <= until)
        complete = len(parts) == expected > 0 and all(
            e.wh is not None and e.coverage >= FULL and e.applicable_s >= 899.0 for e in parts)
        return sum(e.wh or 0.0 for e in parts) if complete else None

    @staticmethod
    def _device_energy(rows: list | None, start: datetime, until: datetime | None) -> tuple[float | None, str | None]:
        """Σ hourly change in [start, until): unknown without rows or with a negative hour; a known partial sum
        ("incomplete") if hours are missing (a consumer that fell behind does not stop everybody else)."""
        if until is None or not rows:
            return None, "no_statistics"
        used = [r for r in rows if isinstance(r, dict) and (end := stat_time(r.get("end"))) is not None and end <= until]
        changes = [r.get("change") for r in used if isinstance(r.get("change"), (int, float))]
        if any(c < 0 for c in changes):
            return None, "counter_reset"
        if not changes:
            return None, "no_statistics"
        hours = round((until - start).total_seconds() / 3600)
        return sum(changes) * 1000.0, (None if len(changes) >= hours else "incomplete")

    async def view(self, profile: BalanceProfile = BalanceProfile.E3DC_BALANCE) -> HomeConsumers:
        now = self._now()
        day = now.astimezone(TZ).date()
        start = local_midnight_utc(day)
        store = getattr(self._ha, "consumers", None)
        empty_rest = ConsumersRest(value=None, reason="total_unknown")

        def unavailable(reason: str) -> HomeConsumers:
            return HomeConsumers(available=False, reason=reason, generated_at=now, local_date=day, stats_from=start,
                                 stats_until=None, consumption_w=None, consumption_today_wh=None, consumers=[],
                                 untracked_power=empty_rest, untracked_energy=empty_rest)

        if self._ha is None or store is None or not self._ha.connected:
            return unavailable("ha_disconnected")
        if store.loaded_at is None:
            return unavailable("no_catalog")
        specs: list[ConsumerSpec] = list(store.catalog)
        if not specs:
            return unavailable("not_configured")

        mapping = self._live.mapping
        wallbox_entity = mapping.metrics["wallbox_power"].entity if "wallbox_power" in mapping.metrics else None
        womo_entity = mapping.metrics["womo_shore_power"].entity if "womo_shore_power" in mapping.metrics else None
        snap = self._live.snapshot(now, profile)

        def metric(mid: str) -> tuple[float | None, MetricState | None]:
            p = snap.metrics.get(mid)
            if p is None:
                return None, None
            value = float(p.value) if isinstance(p.value, (int, float)) and not isinstance(p.value, bool) else None
            return value, p.state

        def is_wallbox(s: ConsumerSpec) -> bool:
            return bool(s.power_entity) and s.power_entity == wallbox_entity

        # today: the last compiled HA hour, capped by what the engine has surely computed
        device_ids = [s.consumer_id for s in specs if not is_wallbox(s)]
        rows = (await self._statistics(device_ids, start, day, now) if device_ids else {}) or {}
        ends = [e for sid in device_ids for r in (rows.get(sid) or []) if isinstance(r, dict)
                and (e := stat_time(r.get("end"))) is not None]
        engine = await self._engine_day(day, profile)
        engine_end = _floor_hour(engine.computed_at - WRITE_LATENCY) if engine is not None else None
        candidates = [t for t in (max(ends) if ends else None, engine_end) if t is not None]
        until = min(candidates) if candidates else None
        if until is not None and until <= start:
            until = None
        consumption_today = self._engine_sum(engine, day, "consumption", until) if until else None

        consumers: list[HomeConsumer] = []
        powers: list[float | None] = []
        energies: list[float | None] = []
        extra_p = extra_e = 0
        for spec, counts in zip(specs, counted(specs), strict=True):
            name, kind = display_name(spec, store.names, wallbox_entity, womo_entity)
            reported = None
            if kind == "wallbox":
                value, state = metric("wallbox_power")
                # untrusted wallbox = 0 W in its row, like the dashboard (user decision 2026-10-05); for the rest it
                # counts as not measured
                power, pstate = (value, "ok") if state is MetricState.OK and value is not None else (0.0, "assumed")
                energy = self._engine_sum(engine, day, "wallbox", until) if until else None
                ereason = None if energy is not None else "incomplete"
            else:
                if kind == "womo":
                    value, state = metric("womo_shore_power")
                    power, pstate = (value, "ok") if state is MetricState.OK and value is not None else (None, "unknown")
                else:
                    reading = store.readings.get(spec.power_entity) if spec.power_entity else None
                    pstate = reading.state(now) if reading else "unknown"
                    power = reading.value_w if reading and pstate == "ok" else None
                    reported = reading.reported_at if reading else None
                energy, ereason = self._device_energy(rows.get(spec.consumer_id), start, until)
            consumers.append(HomeConsumer(consumer_id=spec.consumer_id, name=name, kind=kind,
                                          power_w=None if power is None else round(power, 1), power_state=pstate,
                                          power_reported_at=reported,
                                          energy_today_wh=None if energy is None else round(energy, 1),
                                          energy_reason=ereason))
            if counts:
                powers.append(power)
                energies.append(energy)
                extra_p += pstate == "assumed"
                extra_e += ereason == "incomplete" and energy is not None

        consumption_w, cstate = metric("consumption_total")
        total_w = consumption_w if cstate is MetricState.OK else None
        rest_p = untracked(total_w, powers, UNTRACKED_TOLERANCE_W, extra_p)
        missing = sum(1 for e in energies if e is None) + extra_e
        if until is None or not ends:
            rest_e = ConsumersRest(value=None, reason="no_statistics", unmeasured=missing)
        elif consumption_today is None:
            rest_e = ConsumersRest(value=None, reason="total_incomplete", unmeasured=missing)
        else:
            rest_e = _rest(untracked(consumption_today, energies, UNTRACKED_TOLERANCE_WH, extra_e))
        return HomeConsumers(
            available=True, generated_at=now, local_date=day, stats_from=start, stats_until=until,
            consumption_w=None if total_w is None else round(total_w, 1),
            consumption_today_wh=None if consumption_today is None else round(consumption_today, 1),
            consumers=consumers, untracked_power=_rest(rest_p), untracked_energy=rest_e)
