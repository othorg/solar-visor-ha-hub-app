"""History service for a plant without the existing calculation (plan ha-addon P3 step 4).

Days come from the archive when it has them in a usable state, else they are computed from the recorder and archived
(plant_archive.py). A background run keeps every day of the recorder's keep window archived, oldest first, so a day
nobody opened still keeps its 15-minute resolution after Home Assistant has deleted the raw data.

Reading goes over the hub's single Home Assistant session (``HaClient.read``: one heavy request at a time); without a
session nothing new is computed – archived days are still served, a day due for a refresh in its last archived state
(``stale``, the period says ``refreshing``). The calculation and the archive run in a worker thread: the event loop
serves the live values meanwhile. Nothing is archived before Home Assistant has told the time zone (day bounds).
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .ha_history import Read, oldest_statistic, statistic_ids
from .periods import days as local_days
from .periods import period_bounds, quarter_hours, zone
from .plant import Plant, dump
from .plant_archive import CALCULATION_VERSION, ArchivedDay, PlantArchive, source_key
from .plant_daycalc import LEAD_IN, TIER_HOUR, TIER_RAW, PlantDay, compute_day, requirements
from .plant_history_source import read_day
from .sun import night_intervals

log = logging.getLogger("solar_visor_hub.plant_history")
FINAL_AFTER = timedelta(hours=6)          # late statistics and counter readings have arrived by then (T5)
REFRESH_TODAY = timedelta(minutes=5)
REFRESH_RECENT = timedelta(minutes=30)
META_MAX_AGE = timedelta(hours=1)
WINDOW_MAX_AGE = timedelta(hours=24)
DEFAULT_WINDOW = timedelta(days=10)       # HA's default keep time, until measured
Invalid = Callable[[str, datetime, datetime], list[tuple[datetime, datetime]]]
# reading failed: an archived state is better than none (programming errors are not caught)
READ_ERRORS: tuple[type[BaseException], ...] = (ConnectionError, TimeoutError, OSError, RuntimeError)


class PlantHistory:
    def __init__(self, plant: Plant, archive: PlantArchive, read: Read, connected: Callable[[], bool],
                 units: Callable[[], dict[str, str | None]], timezone: Callable[[], str | None],
                 invalid: Invalid | None = None, now: Callable[[], datetime] = lambda: datetime.now(UTC),
                 location: Callable[[], tuple[float, float] | None] = lambda: None) -> None:
        self.plant, self.archive, self._read = plant, archive, read
        self._connected, self._units, self._timezone, self._invalid, self._now = connected, units, timezone, invalid, now
        self._location = location
        self._closed = False
        self._inflight: dict[date, asyncio.Task[PlantDay]] = {}
        self._meta: dict[str, dict[str, Any]] | None = None
        self._meta_at: datetime | None = None
        self.window_start: datetime | None = None       # oldest 5-minute statistic: the keep window of the recorder
        self._window_at: datetime | None = None
        archive.record_plant(plant, now())

    # --- calendar -------------------------------------------------------------------------------------------------
    @property
    def tz(self) -> ZoneInfo:
        return zone(self._timezone())

    def today(self) -> date:
        return self._now().astimezone(self.tz).date()

    def tier_for(self, d: date) -> str:
        start = period_bounds("day", d, self.tz)[0]
        window = self.window_start or (self._now() - DEFAULT_WINDOW)
        return TIER_RAW if start >= window else TIER_HOUR

    # --- days -----------------------------------------------------------------------------------------------------
    def cached(self, d: date) -> PlantDay | None:
        """The archived day if it is usable as it is (no Home Assistant needed). Blocking: SQLite."""
        a, usable = self._lookup(d)
        return a.day if a is not None and usable else None

    def _lookup(self, d: date) -> tuple[ArchivedDay | None, bool]:
        """Blocking (SQLite, decoding): called in a worker thread from the service."""
        a = self.archive.get(d)
        if a is None:
            return None, False
        usable = self._usable(a, d)
        if usable:
            _ = a.day                       # decode here, not on the event loop
        return a, usable

    def _usable(self, a: ArchivedDay, d: date) -> bool:
        if a.tz != self.tz.key:
            return False                    # other day bounds: computed under another time zone (Codex P3 M6)
        if a.durable:
            return True                     # frozen (user decision 2026-11-15, 3)
        start, end = period_bounds("day", d, self.tz)
        if a.final:
            plant = self.archive.plant_at(start, self.plant)
            return a.calc_version == CALCULATION_VERSION and a.source_key == source_key(plant, a.tier)
        age = self._now() - a.computed_at
        return age < (REFRESH_TODAY if self._now() < end else REFRESH_RECENT)

    async def day(self, d: date) -> PlantDay:
        a, usable = await asyncio.to_thread(self._lookup, d)
        if a is not None and usable:
            return a.day
        if self._closed:
            raise ConnectionError("history service stopped")
        task = self._inflight.get(d)
        if task is None:
            task = asyncio.get_running_loop().create_task(self._compute(d))
            self._inflight[d] = task
            task.add_done_callback(lambda _t, d=d: self._inflight.pop(d, None))
        try:
            return await asyncio.shield(task)
        except READ_ERRORS:
            if a is None or a.tz != self.tz.key:
                raise
            old = await asyncio.to_thread(lambda: a.day)
            return dataclasses.replace(old, stale=True)      # not archived: the next request tries again

    async def days(self, dates: list[date], progress: Callable[[float], None] | None = None) -> list[PlantDay]:
        out = []
        for i, d in enumerate(dates):
            out.append(await self.day(d))
            if progress:
                progress((i + 1) / len(dates))
        return out

    async def revisions(self, start: datetime, end: datetime, current: Plant) -> dict[datetime, Plant]:
        """The plant active at the start of each local day of [start, end) – one object per revision."""
        tz = self.tz
        starts = [period_bounds("day", d, tz)[0] for d in local_days(start, end, tz)]

        def load() -> dict[datetime, Plant]:
            # one object per distinct plant – a source override can change a revision from one day on (Codex P4 M3)
            seen: dict[str, Plant] = {json.dumps(dump(current), sort_keys=True): current}
            out = {}
            for s in starts:
                pl = self.archive.plant_at(s, current)
                out[s] = seen.setdefault(json.dumps(dump(pl), sort_keys=True), pl)
            return out
        return await asyncio.to_thread(load)

    async def missing(self, dates: list[date]) -> list[date]:
        return await asyncio.to_thread(lambda: [d for d in dates if not self._usable_only(d)])

    def _usable_only(self, d: date) -> bool:
        a = self.archive.get(d)
        return a is not None and self._usable(a, d)

    async def _compute(self, d: date) -> PlantDay:
        tz, now, tz_known = self.tz, self._now(), self._timezone() is not None
        start, end = period_bounds("day", d, tz)
        slots = quarter_hours(start, end, tz)
        gen = await asyncio.to_thread(self.archive.override_gen)       # sources as this computation sees them
        plant = await asyncio.to_thread(self.archive.plant_at, start, self.plant)
        if start >= now:                      # nothing has happened yet: no request, nothing archived
            return compute_day(plant, slots, start, start, TIER_RAW, {})
        if not self._connected():
            raise ConnectionError("no Home Assistant session")
        await self._refresh_window()
        tier = self.tier_for(d)
        until = min(end, now)
        data = await read_day(plant, start, until, tier, self._read, await self._statistic_meta(), self._units())
        location = self._location()

        def finish() -> PlantDay:             # worker thread: journal, calculation, archive
            invalid = {}
            if self._invalid is not None:     # source outages end raw holds and counter holds alike
                for metric_id in requirements(plant, tier):
                    entity = plant.points()[metric_id].entity
                    invalid[entity] = self._invalid(entity, start - LEAD_IN, until)
            nights = night_intervals(start, until, lat=location[0], lon=location[1]) if location else None
            day = compute_day(plant, slots, start, until, tier, data, invalid, nights)
            if tz_known:
                self.archive.put(d, tz.key, day, now, now >= end + FINAL_AFTER, plant, gen)
            return day

        return await asyncio.to_thread(finish)

    async def _statistic_meta(self) -> dict[str, dict[str, Any]]:
        now = self._now()
        if self._meta is None or self._meta_at is None or now - self._meta_at > META_MAX_AGE:
            self._meta, self._meta_at = await statistic_ids(self._read), now
        return self._meta

    async def _refresh_window(self) -> None:
        """Oldest 5-minute statistic of any source the plant reads (means and counters): from there on a day can be
        computed in full resolution (raw states have the same keep time, P3 T4). A source added later does not move
        it – the oldest of all counts. Measured once a day."""
        now = self._now()
        if self._window_at is not None and now - self._window_at < WINDOW_MAX_AGE:
            return
        meta = await self._statistic_meta()
        entities = sorted({p.entity for p in self.plant.points().values() if p.entity in meta})
        if entities:
            self.window_start = await oldest_statistic(self._read, entities, now) or self.window_start
        self._window_at = now

    # --- background -----------------------------------------------------------------------------------------------
    async def keep_archived(self) -> int:
        """Archives every day of the keep window that is not archived for good yet, oldest first. Returns the number
        of days computed."""
        if not self._connected() or self._timezone() is None or self._closed:
            return 0
        await self._refresh_window()
        first = (self.window_start or (self._now() - DEFAULT_WINDOW)).astimezone(self.tz).date() + timedelta(days=1)
        done, d = 0, first
        while d <= self.today():
            if not await asyncio.to_thread(self._usable_only, d) and not (await self.day(d)).stale:
                done += 1
            d += timedelta(days=1)
        return done

    async def aclose(self) -> None:
        """No new computations; running ones are cancelled and collected (shutdown)."""
        self._closed = True
        tasks = list(self._inflight.values())
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def run_forever(self, first_delay: float = 60.0, interval: float = 3600.0,
                          sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        await sleep(first_delay)
        while True:
            try:
                n = await self.keep_archived()
                if n:
                    log.info("archived %d day(s) of the plant's history", n)
            except asyncio.CancelledError:
                raise
            except Exception:   # noqa: BLE001 – retried next hour; the live API must never suffer from it
                log.exception("archiving the plant's history failed – retrying later")
            await sleep(interval)
