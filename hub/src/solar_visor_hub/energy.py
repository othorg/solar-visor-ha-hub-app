"""Energy engine (plan §3.4, P3): change-based raw points → 15-min buckets. Pure, no I/O.

Rules (verified against the 2026-10-03 reference day, plan 01 §4):
- **Sample-and-hold**, never trapezoid: a value holds until the next point, an invalidity mark or the end
  of its hold time (E3/DC/KOSTAL ≠ 0: 30 min, = 0: 6 h; openWB ≠ 0: 120 s). Carry-in = last point before
  the window (looked up by the caller, ≤ 48 h).
- **Gaps are never 0**: uncovered time lowers ``coverage`` and adds an upper bound to ``unresolved_wh``.
- **Direction split per hold segment** before aggregation (battery/grid net series).
- **Counters**: only increments above the high-water mark count (rollback = anomaly; 1562.64 → 1556.23 →
  1562.65 kWh yields 0.01 kWh); an increment spanning several buckets is allocated by time.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .periods import Slot


@dataclass(frozen=True)
class Point:
    t: datetime
    value: float | None          # canonical unit (W for power, Wh for counters); None = unavailable


@dataclass(frozen=True)
class HoldRule:
    nonzero: timedelta
    zero: timedelta

    def hold(self, value: float) -> timedelta:
        return self.zero if value == 0 else self.nonzero


E3DC_HOLD = HoldRule(nonzero=timedelta(minutes=30), zero=timedelta(hours=6))
OPENWB_HOLD = HoldRule(nonzero=timedelta(seconds=120), zero=timedelta(hours=6))
# SOC is written on change (1-% steps); an idle battery keeps its SOC for up to 40 h (P4) – unavailability
# marks and journal vetoes end it earlier
SOC_HOLD = HoldRule(nonzero=timedelta(hours=48), zero=timedelta(hours=48))


@dataclass(frozen=True)
class Segment:
    start: datetime
    end: datetime
    value: float                 # W


@dataclass
class BucketEnergy:
    slot: Slot
    positive_wh: float = 0.0
    negative_wh: float = 0.0     # magnitude of the negative part
    covered_s: float = 0.0
    unresolved_wh: float = 0.0   # upper bound of the energy in uncovered time
    reasons: set[str] = field(default_factory=set)

    @property
    def coverage(self) -> float:
        total = (self.slot.end - self.slot.start).total_seconds()
        return min(1.0, self.covered_s / total) if total else 0.0


# --- sample-and-hold ---------------------------------------------------------------------------

def hold_segments(points: Sequence[Point], start: datetime, end: datetime, rule: HoldRule,
                  invalid: Iterable[tuple[datetime, datetime]] = ()) -> list[Segment]:
    """Valid constant-power segments inside [start, end). ``points`` sorted by time, may begin with the
    carry-in point before ``start``. ``invalid`` intervals (journal, plausibility filters) are cut out."""
    segs: list[Segment] = []
    for i, p in enumerate(points):
        if p.value is None:
            continue
        nxt = points[i + 1].t if i + 1 < len(points) else end
        s, e = max(p.t, start), min(nxt, p.t + rule.hold(p.value), end)
        if e > s:
            segs.append(Segment(s, e, p.value))
    for a, b in invalid:
        segs = cut(segs, a, b)
    return segs


def cut(segs: list[Segment], a: datetime, b: datetime) -> list[Segment]:
    """Removes [a, b) from the segments."""
    out: list[Segment] = []
    for sg in segs:
        if sg.end <= a or sg.start >= b:
            out.append(sg)
            continue
        if sg.start < a:
            out.append(Segment(sg.start, a, sg.value))
        if sg.end > b:
            out.append(Segment(b, sg.end, sg.value))
    return out


MIN_EXCEEDING = timedelta(seconds=60)


def exceeding(candidate: list[Segment], reference: list[Segment], tolerance_w: float = 100.0,
              tolerance_frac: float = 0.02, min_duration: timedelta = MIN_EXCEEDING) -> list[tuple[datetime, datetime]]:
    """Intervals where ``candidate`` > ``reference`` + max(tol_w, frac · reference) – e.g. a wallbox value
    above the total consumption that includes it (frozen/retained openWB value). Only compared where
    both are covered. Exceedances shorter than ``min_duration`` are timing skew between two independently
    reporting devices (measured: 24 × 2–10 s at charging steps), not a freeze."""
    out: list[tuple[datetime, datetime]] = []
    j = 0
    for c in candidate:
        while j < len(reference) and reference[j].end <= c.start:
            j += 1
        k = j
        while k < len(reference) and reference[k].start < c.end:
            r = reference[k]
            s, e = max(c.start, r.start), min(c.end, r.end)
            if e > s and c.value > r.value + max(tolerance_w, tolerance_frac * r.value):
                out.append((s, e))
            k += 1
    return [(a, b) for a, b in _merge(out) if b - a >= min_duration]


def _merge(iv: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    out: list[tuple[datetime, datetime]] = []
    for a, b in sorted(iv):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def bucketize(segs: list[Segment], slots: Sequence[Slot], p_max_w: float,
              unresolved_bound: list[Segment] | None = None) -> list[BucketEnergy]:
    """Integrates segments into slots (direction split). Uncovered time gets an upper bound:
    ``unresolved_bound`` segments if given (e.g. consumption for the wallbox), else ``p_max_w``."""
    buckets = [BucketEnergy(slot) for slot in slots]
    i = 0
    for b in buckets:
        while i < len(segs) and segs[i].end <= b.slot.start:
            i += 1
        k = i
        while k < len(segs) and segs[k].start < b.slot.end:
            sg = segs[k]
            s, e = max(sg.start, b.slot.start), min(sg.end, b.slot.end)
            if e > s:
                dt = (e - s).total_seconds()
                wh = sg.value * dt / 3600.0
                if wh >= 0:
                    b.positive_wh += wh
                else:
                    b.negative_wh -= wh
                b.covered_s += dt
            k += 1
        gap_s = (b.slot.end - b.slot.start).total_seconds() - b.covered_s
        if gap_s > 0.5:
            b.reasons.add("gap")
            if unresolved_bound is not None:
                b.unresolved_wh = _bound_in_gaps(b.slot, segs, unresolved_bound, p_max_w)
            else:
                b.unresolved_wh = p_max_w * gap_s / 3600.0
    return buckets


def _bound_in_gaps(slot: Slot, segs: list[Segment], bound: list[Segment], p_max_w: float) -> float:
    """Upper bound of the energy in the parts of ``slot`` not covered by ``segs``: the ``bound`` series where
    it is known (e.g. consumption for the wallbox), the physical maximum where it is not."""
    gaps = uncovered(segs, slot.start, slot.end)
    total = 0.0
    for a, b in gaps:
        known = 0.0
        for r in bound:
            s, e = max(a, r.start), min(b, r.end)
            if e > s:
                total += max(r.value, 0.0) * (e - s).total_seconds() / 3600.0
                known += (e - s).total_seconds()
        total += p_max_w * max(0.0, (b - a).total_seconds() - known) / 3600.0
    return total


def uncovered(segs: list[Segment], a: datetime, b: datetime) -> list[tuple[datetime, datetime]]:
    """Parts of [a, b) not covered by any segment."""
    covered = _merge([(max(s.start, a), min(s.end, b)) for s in segs if s.end > a and s.start < b])
    gaps, t = [], a
    for x, y in covered:
        if x > t:
            gaps.append((t, x))
        t = max(t, y)
    if t < b:
        gaps.append((t, b))
    return gaps


def merge_intervals(iv: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    return _merge(iv)


# --- counters ----------------------------------------------------------------------------------

COUNTER_HOLD = timedelta(days=31)    # no new reading = no energy (written on change only; P4: wallbox idle for weeks)
COUNTER_EXACT = timedelta(minutes=5)  # IOmeter writes every ~36 s; a longer interval bridges an outage (power estimated)
COUNTER_ESTIMATE_W = 50.0             # … unless its average power is negligible


@dataclass
class CounterTimeline:
    segments: list[Segment]          # average power between consecutive trusted readings
    hold: list[Segment]              # trailing "no new reading = no energy" (subject to availability vetoes)
    trusted: list[Point]
    anomalies: int
    implausible: int
    spans_from: datetime | None = None   # start of a positive interval that began before the window
    closed_at: datetime | None = None    # latest reading that closed such an interval
    estimated: list[tuple[datetime, datetime]] = field(default_factory=list)   # clipped spans of long intervals


def counter_timeline(points: Sequence[Point], base: Point | None, p_max_w: float, start: datetime, end: datetime,
                     hold: timedelta = COUNTER_HOLD) -> CounterTimeline:
    """Energy timeline of a cumulative counter inside [start, end).

    ``base`` = highest trusted reading before the window in the same epoch (value and its time), so the
    result does not depend on which day was computed first. A reading below the high-water mark is a
    rollback (anomaly, not trusted); recovery counts only above the previous maximum. A jump beyond physics
    (checked over ≥ 5 min) is rejected and does not advance the trusted state. Between two trusted readings
    the energy is known exactly (their difference, spread linearly); after the last one the counter holds
    (no change = no energy) for at most ``hold``. Points after ``end`` close the last interval."""
    trusted: list[Point] = [base] if base is not None and base.value is not None else []
    anomalies = implausible = 0
    below = False
    for p in points:
        if p.value is None:
            continue
        if not trusted:
            trusted.append(p)
            continue
        hwm = trusted[-1]
        if p.value < hwm.value:      # type: ignore[operator]
            if not below:
                anomalies += 1
            below = True
            continue
        below = False
        span_s = (p.t - hwm.t).total_seconds()
        if p.value - hwm.value > p_max_w * max(span_s, 300.0) / 3600.0 * 1.2:   # type: ignore[operator]
            implausible += 1
            continue
        if p.t > hwm.t:
            trusted.append(p)
    segs: list[Segment] = []
    spans_from: datetime | None = None
    closed_at: datetime | None = None
    estimated: list[tuple[datetime, datetime]] = []
    for a, b in zip(trusted, trusted[1:], strict=False):
        dur_h = (b.t - a.t).total_seconds() / 3600.0
        if dur_h <= 0:
            continue
        s_, e_ = max(a.t, start), min(b.t, end)
        if e_ > s_:
            segs.append(Segment(s_, e_, (b.value - a.value) / dur_h))   # type: ignore[operator]
            # unclipped duration; a few Wh spread over hours (idle import counter while exporting) is no estimate
            if b.t - a.t > COUNTER_EXACT and (b.value - a.value) / dur_h > COUNTER_ESTIMATE_W:   # type: ignore[operator]
                estimated.append((s_, e_))
            if a.t < start and b.value > a.value:   # type: ignore[operator]
                spans_from = a.t if spans_from is None else min(spans_from, a.t)
                closed_at = b.t if closed_at is None else max(closed_at, b.t)
    holds: list[Segment] = []
    if trusted:
        last = trusted[-1]
        s_, e_ = max(last.t, start), min(last.t + hold, end)
        if e_ > s_ and last.t < end:
            holds.append(Segment(s_, e_, 0.0))
    return CounterTimeline(segs, holds, trusted, anomalies, implausible, spans_from, closed_at, estimated)


def combine(a: Sequence[Segment], b: Sequence[Segment], sign: int = 1) -> list[Segment]:
    """a + sign·b on the intersection of both covered intervals (derived series, e.g. generation = PV +
    external): never adds a known value to an unknown one. Inputs sorted and non-overlapping."""
    out: list[Segment] = []
    i = j = 0
    while i < len(a) and j < len(b):
        s, e = max(a[i].start, b[j].start), min(a[i].end, b[j].end)
        if e > s:
            out.append(Segment(s, e, a[i].value + sign * b[j].value))
        if a[i].end <= b[j].end:
            i += 1
        else:
            j += 1
    return out


def integrate(segs: Sequence[Segment], a: datetime, b: datetime) -> float:
    """Energy (Wh) of the segments inside [a, b)."""
    total = 0.0
    for sg in segs:
        s_, e_ = max(sg.start, a), min(sg.end, b)
        if e_ > s_:
            total += sg.value * (e_ - s_).total_seconds() / 3600.0
    return total
