"""Loading for /v1/compare (comparison plan 2026-10-06 §4): which days, from where, consistently.

- The running day is loaded first: its ``computed_at`` bounds the cut.
- Every day is loaded once – overlapping periods share the same objects, so a day can never appear in two revisions.
  Days with slot data (cut day, DST pairs, day comparison) take their aggregate from the same slot data.
- Invalidation counters before/after the loads: a day invalidated meanwhile makes the answer ``refreshing`` and no
  change "reliable".
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta

from .comparison import Range, compose, cut_time, data_day, plan
from .history import DayAggregate, DayData, EnergyService, aggregate
from .models import BalanceProfile, CompareBasis, EnergyComparison


def requested_days(a: Range, b: Range) -> list[date]:
    return sorted(set(a.days()) | set(b.days()))


def missing(service: EnergyService, a: Range, b: Range, profile: BalanceProfile) -> list[date]:
    """Days of both periods that need a computation (deduplicated; job above the threshold)."""
    return service.missing(requested_days(a, b), profile)


def _runs(days: list[date]) -> list[list[date]]:
    """Contiguous runs (``day_aggregates`` reads one BETWEEN range per call)."""
    out: list[list[date]] = []
    for d in days:
        if out and out[-1][-1] + timedelta(days=1) == d:
            out[-1].append(d)
        else:
            out.append([d])
    return out


async def compare(service: EnergyService, kind: str, a: Range, b: Range, basis: CompareBasis | None,
                  profile: BalanceProfile, now: datetime) -> EnergyComparison:
    slot_data: dict[date, DayData] = {}
    refreshing = False
    running_day = data_day(now)
    before = service.generations([running_day])
    computed_at = None
    if a.contains(running_day) or b.contains(running_day):
        got, r = await service.day_slots([running_day], profile)
        refreshing |= r
        if got[0] is not None:
            slot_data[running_day] = got[0]
            computed_at = got[0].computed_at
    t = cut_time(now, computed_at)
    need = plan(kind, a, b, t)
    for d, g in service.generations([*need.aggregate_days, *need.slot_days]).items():
        before.setdefault(d, g)                      # keep the running day's count from before its load
    todo = sorted(need.slot_days - slot_data.keys())
    if todo:
        got_days, r = await service.day_slots(todo, profile)
        refreshing |= r
        slot_data |= {d: x for d, x in zip(todo, got_days, strict=True) if x is not None}
    aggs: dict[date, DayAggregate] = {d: aggregate(x) for d, x in slot_data.items()}
    for run in _runs(sorted(set(need.aggregate_days) - aggs.keys())):
        got_aggs, r = await service.day_aggregates(run, profile)
        refreshing |= r
        aggs |= dict(zip(run, got_aggs, strict=True))
    consistent = service.generations(before) == before
    # CPU work (a year: 730 days) off the event loop – live processing and other requests stay responsive
    return await asyncio.to_thread(compose, kind, a, b, basis, profile, now, t, aggs, slot_data, refreshing, consistent)
