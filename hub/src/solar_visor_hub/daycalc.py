"""Energy of one window (normally a local day) from raw series – pure, shared by tests and the service.

Model (Codex P3 review): every category is a list of *known intervals with an average power*:
- power series: sample-and-hold segments (hold times, journal veto, PV night rule, wallbox rules);
- counters: consecutive trusted readings (difference spread over their interval) + a trailing hold.
Coverage, uncertainty and KPIs all derive from these intervals, so epoch boundaries, partial coverage and
"today" (window ends at *now*) are handled uniformly. Uncovered time inside the applicable range gets an
upper bound (a bounding series where known, else the physical maximum); time outside a category's epoch
or after *now* is not applicable (no energy, no uncertainty).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .energy import (
    SOC_HOLD,
    Point,
    Segment,
    counter_timeline,
    cut,
    exceeding,
    hold_segments,
    integrate,
    merge_intervals,
    uncovered,
)
from .epochs import IOMETER_FROM, SOC_STEP_WH, SeriesSpec, epoch_start
from .periods import Slot
from .sun import night_intervals

COUNTER_RESOLUTION_WH = 20.0   # openWB 0.01 kWh steps: ±1 step at each end of a counter interval
# A PV reading up to this value when the night ends counts as zero after it: around dusk inverters and meters toggle
# between 0 and a few watts
DUSK_RESIDUAL_W = 25.0
# An idle battery's SOC drifts in single 1-% steps (BMS consumption: a few steps over many hours at 0 W; after a charge
# it falls back one step within about an hour) and flaps between two neighbours at a rounding boundary (within
# minutes). A shift of that two-value band at least this long after the previous one implies
# ≤ ~520 W – real charging moves the SOC every 5–15 min and the battery reading would show it
SOC_DRIFT_MIN_INTERVAL = timedelta(minutes=30)
# The SOC follows the end of a charge/discharge with a lag: up to 44 min and three steps after the last battery reading
# (2025-12-06, 2026-01-04) – changes inside this window still belong to the measured flow before the idle phase
SOC_SETTLE = timedelta(minutes=45)
# HA demonstrably wrote to InfluxDB while the house consumption (changes every ~10 s) has readings at most this far
# apart; the inferences above (idle battery, PV zero after the night) only hold there – never across a real outage
# (e.g. 2026-05-03 19:10Z → 05-04 05:20Z: the morning PV would otherwise count as 0)
PIPELINE_GAP = timedelta(minutes=15)


@dataclass
class RawSeries:
    points: list[Point]                     # carry-in first (if any), window points, counters: + first point after
    base: Point | None = None               # counters: highest trusted reading before the window (same epoch)


@dataclass
class SlotEnergy:
    wh: float | None
    coverage: float
    unresolved_wh: float = 0.0
    method: str = "sample_hold"
    time_allocated: bool = False
    confidence: str = "observed"
    reasons: set[str] = field(default_factory=set)
    applicable_s: float = 0.0               # seconds of the slot in which the category had a source (coverage base)

    @property
    def covered_s(self) -> float:
        return self.coverage * self.applicable_s


@dataclass
class HistoryTimeline:
    """Validated intervals of one category for power history (same rules as the energy): segments with average
    power, applicable time and estimated time (reconstructed epochs, idle inference, counter fill)."""
    segs: list[Segment]
    applicable: list[tuple[datetime, datetime]]
    estimated: list[tuple[datetime, datetime]]


HISTORY_CATEGORIES = ("pv_internal", "external", "consumption", "wallbox", "battery_charge", "battery_discharge",
                      "grid_import", "grid_export", "grid_import_e3dc", "grid_export_e3dc", "mppt1", "mppt2", "mppt3",
                      "battery_soc", "womo_shore", "womo_battery_charge", "womo_battery_discharge",
                      "s10_string1", "s10_string2", "kostal_dc_sum")


@dataclass
class DayResult:
    categories: dict[str, list[SlotEnergy]]
    # per slot: (numerator Wh, denominator Wh, joint seconds, applicable seconds) on common valid intervals
    kpi_parts: dict[str, list[tuple[float, float, float, float]]]
    high_water_end: dict[str, float | None]
    counter_anomalies: dict[str, int]
    # earliest start of a counter interval with positive energy that began before the window: cached results of
    # the days it spans may be stale (they were computed before the closing reading existed)
    counter_spans_from: datetime | None = None
    counter_closed_at: datetime | None = None   # the reading that closed such an interval
    # per counter measurement: (earliest span start, latest closing reading) – keeps the source identity
    counter_spans: dict[str, tuple[datetime, datetime]] = field(default_factory=dict)
    timelines: dict[str, HistoryTimeline] = field(default_factory=dict)


@dataclass
class _Timeline:
    segs: list[Segment] = field(default_factory=list)
    applicable: list[tuple[datetime, datetime]] = field(default_factory=list)
    p_max_w: float = 0.0
    method: str = "sample_hold"
    reconstructed: list[tuple[datetime, datetime]] = field(default_factory=list)
    bound: list[Segment] | None = None
    extra_unresolved: dict[int, float] = field(default_factory=dict)   # slot index → Wh
    reasons: dict[int, set[str]] = field(default_factory=dict)
    filled: list[tuple[datetime, datetime]] = field(default_factory=list)   # counter-filled (power estimated)


def _intersect(a: list[tuple[datetime, datetime]], b: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    out, i, j = [], 0, 0
    a, b = merge_intervals(a), merge_intervals(b)
    while i < len(a) and j < len(b):
        s, e = max(a[i][0], b[j][0]), min(a[i][1], b[j][1])
        if e > s:
            out.append((s, e))
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return out


def _covered(segs: list[Segment]) -> list[tuple[datetime, datetime]]:
    return merge_intervals([(s.start, s.end) for s in segs])


def _seconds(iv: list[tuple[datetime, datetime]], a: datetime, b: datetime) -> float:
    return sum(max(0.0, (min(e, b) - max(s, a)).total_seconds()) for s, e in iv)


def _status_off(points: Sequence[Point], start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    """Intervals where a binary status is off (value 0); valid until the next change (no hold limit)."""
    out = []
    for i, p in enumerate(points):
        nxt = points[i + 1].t if i + 1 < len(points) else end
        s, e = max(p.t, start), min(nxt, end)
        if e > s and p.value == 0:
            out.append((s, e))
    return out


def _fill(segs: list[Segment], intervals: list[tuple[datetime, datetime]], value: float = 0.0) -> list[Segment]:
    """Adds constant segments over the *uncovered* parts of the (merged) intervals – never replaces data."""
    filled = list(segs)
    for a, b in merge_intervals(intervals):
        filled += [Segment(x, y, value) for x, y in uncovered(segs, a, b)]
    return sorted(filled, key=lambda x: x.start)


# kept for tests/backwards compatibility
def _fill_zero(segs: list[Segment], off: list[tuple[datetime, datetime]]) -> list[Segment]:
    """Status 'off' confirms zero in uncovered time; measured power is never removed (historic openWB 1.9
    status lagged real charging – energy was measured while 'charging off')."""
    return _fill(segs, off)


def _fill_night(segs: list[Segment], nights: list[tuple[datetime, datetime]]) -> list[Segment]:
    """PV is physically zero while the sun is down – also without a preceding zero and through source
    outages (decision 2026-10-04; the outage itself stays visible in the journal)."""
    return _fill(segs, nights)


def _alive(series: Sequence[Sequence[Point]], start: datetime, end: datetime) -> list[tuple[datetime, datetime]] | None:
    """Intervals in which the pipeline HA → InfluxDB demonstrably worked: between consecutive readings of the house
    consumption at most ``PIPELINE_GAP`` apart – a longer pause is an outage from its first second (Codex review
    2026-10-08); the open end up to the window end counts while it is short. None without such a series (unit tests
    of single categories)."""
    if not series:
        return None
    ts = sorted({p.t for pts in series for p in pts if p.value is not None})
    iv = [(t, nxt) for t, nxt in zip(ts, ts[1:], strict=False) if nxt - t <= PIPELINE_GAP]
    if ts and end - ts[-1] <= PIPELINE_GAP:
        iv.append((ts[-1], end))
    return _intersect(merge_intervals(iv), [(start, end)])


def _within(segs: list[Segment], alive: list[tuple[datetime, datetime]] | None) -> list[Segment]:
    if alive is None:
        return segs
    return [Segment(max(s.start, a), min(s.end, b), s.value) for s in segs for a, b in alive
            if min(s.end, b) > max(s.start, a)]


def _zero_after_night(segs: list[Segment], pts: Sequence[Point], nights: list[tuple[datetime, datetime]],
                      hold: timedelta | None, end: datetime, vetoes: Sequence[tuple[datetime, datetime]] = (),
                      alive: list[tuple[datetime, datetime]] | None = None) -> list[Segment]:
    """PV that read (about) zero when the night ended stays zero until the next reading, for at most the zero hold
    counted from the end of the night (``None``: until the next reading – E3/DC series while the pipeline is alive): HA
    writes nothing while the value is unchanged and a waking inverter writes a new one (2026-10-08: 0 W from 16:35Z,
    next reading 06:00Z, night rule until about 05:45Z – a gap in complete data).
    Not after an unavailable reading, never across a journal veto or a pipeline outage, never replacing a reading."""
    filled = list(segs)
    for _a, b in nights:
        if b >= end:
            continue
        before = [p for p in pts if p.t < b]
        if not before or before[-1].value is None or before[-1].value > DUSK_RESIDUAL_W:
            continue
        nxt = next((p.t for p in pts if p.t >= b), end)
        zero = _within([Segment(b, min(nxt, end) if hold is None else min(nxt, b + hold, end), 0.0)], alive)
        for x, y in vetoes:
            zero = cut(zero, x, y)
        for z in zero:
            filled += [Segment(x, y, 0.0) for x, y in uncovered(filled, z.start, z.end)]
    return sorted(filled, key=lambda x: x.start)


def _battery_idle(segs: list[Segment], pts: list[Point], soc: list[Point], start: datetime, end: datetime,
                  vetoes: list[tuple[datetime, datetime]] = (),
                  alive: list[tuple[datetime, datetime]] | None = None) -> tuple[list[Segment], list[tuple[datetime, datetime]]]:
    """A battery reading of exactly 0 W stays valid beyond the zero hold while the SOC does not change: HA writes
    no new point for an idle battery (empty in winter for up to 40 h); a change of the battery power would itself
    have been written. The unchanged SOC is independent evidence against unnoticed data loss (net ≤ one 1-% step,
    reported as uncertainty; the phase is marked as inferred/reconstructed).

    The SOC may drift meanwhile (user decision 2026-10-08, data: idle phases at 0 W in December 2025/January 2026):
    after ``SOC_SETTLE`` (the SOC catching up with the flow before) it may move within a band of two neighbouring
    values (rounding flaps) and shift that band by one step at least ``SOC_DRIFT_MIN_INTERVAL`` after the previous
    shift; every band is its own phase with one step of uncertainty. A larger jump, a faster shift or an invalid SOC
    ends the inference.

    Never applied where availability is explicitly missing (Codex P4 review): the battery reading before the gap
    must be a valid 0 (not 'unavailable'), every SOC reading must be valid (0–100), and journal vetoes of the
    battery or SOC entity cut the inferred phase, as does a pipeline outage (no house consumption readings)."""
    if not soc:
        return segs, []
    filled = list(segs)
    phases: list[tuple[datetime, datetime]] = []
    vetoed = [Segment(v0, v1, 0.0) for v0, v1 in merge_intervals(list(vetoes))]
    for a, b in uncovered(segs, start, end):
        before = [p for p in pts if p.t <= a]
        if not before or before[-1].value != 0:
            continue                                   # last reading unavailable or not zero
        since = before[-1].t
        settled = since + SOC_SETTLE
        # SOC once settled after the idle start; an idle phase that began before the window only brings the SOC
        # carry-in (2026-01-03: idle since 2026-01-02 12:54Z, SOC carry-in 18:32Z) – then the inference starts from it
        soc_before = [p for p in soc if p.t <= min(settled, a)] or [p for p in soc[:1] if p.t <= a]
        if not soc_before or soc_before[-1].value is None or not 0 <= soc_before[-1].value <= 100:
            continue                                   # no valid SOC evidence at the start of the idle phase
        ref = soc_before[-1]
        lo = hi = ref.value                            # band of at most two neighbouring SOC values
        shifted, stop, steps = max(ref.t, since), b, []
        for p in soc:
            if not ref.t < p.t < b:
                continue
            v = p.value
            if v is None or not 0 <= v <= 100:
                stop = p.t                             # invalid SOC
                break
            if lo <= v <= hi:
                continue                               # inside the band (rounding flap)
            if hi - lo == 0 and abs(v - lo) == 1:
                lo, hi = min(lo, v), max(hi, v)        # the band forms: still within one step
                continue
            if (v == hi + 1 or v == lo - 1) and p.t - shifted >= SOC_DRIFT_MIN_INTERVAL:
                lo, hi = (hi, v) if v > hi else (v, lo)
                shifted = p.t
                steps.append(p.t)
                continue
            stop = p.t                                 # a jump or a shift too fast for unnoticed drift
            break
        cuts = [a, *(t for t in steps if a < t < stop), stop]
        for c0, c1 in zip(cuts, cuts[1:], strict=False):
            for piece in _within([Segment(x, y, 0.0) for x, y in uncovered(vetoed, c0, c1)], alive):
                filled.append(piece)
                phases.append((piece.start, piece.end))
    return sorted(filled, key=lambda x: x.start), phases


def compute_day(slots: Sequence[Slot], specs: Sequence[SeriesSpec], raw: dict[str, RawSeries],
                invalid: dict[str, list[tuple[datetime, datetime]]] | None = None,
                window_end: datetime | None = None, evidence: Sequence[Point] | None = None) -> DayResult:
    """``evidence``: house consumption readings for computations without the consumption spec (motorhome, PV detail
    charts, KOSTAL AC line) – the zero inferences need the same proof of a working pipeline there."""
    invalid = invalid or {}
    start = slots[0].start
    end = min(slots[-1].end, window_end) if window_end else slots[-1].end
    tl: dict[str, _Timeline] = {}
    hwm_end: dict[str, float | None] = {}
    anomalies: dict[str, int] = {}

    def timeline(cat: str, spec: SeriesSpec, vf: datetime, vt: datetime) -> _Timeline:
        t = tl.setdefault(cat, _Timeline(p_max_w=spec.p_max_w))
        t.applicable.append((vf, vt))
        t.p_max_w = max(t.p_max_w, spec.p_max_w)
        if spec.confidence == "reconstructed":
            t.reconstructed.append((vf, vt))
        return t

    def points(spec: SeriesSpec) -> list[Point]:
        return raw.get(spec.measurement, RawSeries([])).points

    status_off: list[tuple[datetime, datetime]] = []
    soc_points: list[Point] = []
    soc_vetoes: list[tuple[datetime, datetime]] = []
    spans_from: datetime | None = None
    closed_at: datetime | None = None
    counter_spans: dict[str, tuple[datetime, datetime]] = {}
    wallbox_specs: list[tuple[SeriesSpec, datetime, datetime]] = []
    counter_tl: dict[str, list[Segment]] = {}

    # evidence series first (status, SOC) – power series below depend on them regardless of spec order
    for spec in specs:
        vf, vt = max(spec.valid_from, start), min(spec.valid_to, end)
        if vt <= vf:
            continue
        if spec.kind == "status":
            status_off += _status_off(points(spec), vf, vt)
        elif spec.kind == "soc":
            soc_points = list(points(spec))                     # keep 'unavailable' markers (None)
            soc_vetoes = list(invalid.get(spec.measurement, ()))
            valid = [Point(p.t, p.value if p.value is not None and 0 <= p.value <= 100 else None) for p in soc_points]
            soc_t = timeline("battery_soc", spec, vf, vt)
            soc_t.segs += hold_segments(valid, vf, vt, SOC_HOLD, soc_vetoes)

    alive = _alive([evidence] if evidence is not None else
                   [points(s) for s in {s.measurement: s for s in specs
                                        if s.kind == "power" and s.categories == ("consumption",)}.values()], start, end)
    for spec in specs:
        vf, vt = max(spec.valid_from, start), min(spec.valid_to, end)
        if vt <= vf or spec.kind in ("status", "soc"):
            continue
        if spec.kind == "absent":                          # device not installed yet: known zero
            timeline(spec.categories[0], spec, vf, vt).segs.append(Segment(vf, vt, 0.0))
            continue
        if spec.kind == "counter":
            es = epoch_start(spec.measurement, spec.valid_from)   # specs are clipped to the window – use the epoch
            pts = [p for p in points(spec)
                   if es <= p.t < spec.valid_to or (spec.valid_to >= end and p.t >= end)]
            base = raw.get(spec.measurement, RawSeries([])).base
            if base is not None and base.t < es:
                base = None                                    # never carry a reading across an epoch start
            ct = counter_timeline(pts, base, spec.p_max_w, vf, vt, hold=spec.idle_hold)
            hold = ct.hold
            for a_, b_ in invalid.get(spec.measurement, ()):   # availability veto: no idle inference while unavailable
                hold = cut(hold, a_, b_)
            t = timeline(spec.categories[0], spec, vf, vt)
            t.segs += ct.segments + hold
            t.method = "counter_delta"
            # power history: a counter interval much longer than the reading cadence only says how much energy, not
            # when – its average power is an estimate (energy itself is measured)
            t.filled += ct.estimated
            counter_tl.setdefault(spec.categories[0], []).extend(ct.segments + hold)
            if ct.spans_from is not None and (spans_from is None or ct.spans_from < spans_from):
                spans_from = ct.spans_from
            if ct.closed_at is not None and (closed_at is None or ct.closed_at > closed_at):
                closed_at = ct.closed_at
            if ct.spans_from is not None and ct.closed_at is not None:
                prev = counter_spans.get(spec.measurement)
                counter_spans[spec.measurement] = (min(ct.spans_from, prev[0]) if prev else ct.spans_from,
                                                   max(ct.closed_at, prev[1]) if prev else ct.closed_at)
            hwm_end[spec.measurement] = ct.trusted[-1].value if ct.trusted else None
            anomalies[spec.measurement] = anomalies.get(spec.measurement, 0) + ct.anomalies
            continue
        segs = hold_segments(points(spec), vf, vt, spec.hold, invalid.get(spec.measurement, ()))
        idle_phases: list[tuple[datetime, datetime]] = []
        if spec.categories == ("battery_charge", "battery_discharge"):
            segs, idle_phases = _battery_idle(segs, points(spec), soc_points, vf, vt,
                                              list(invalid.get(spec.measurement, ())) + soc_vetoes, alive)
        if spec.clip_negative:
            segs = [Segment(s.start, s.end, max(0.0, s.value)) for s in segs]
        if spec.solar:
            nights = night_intervals(vf, vt)
            segs = _fill_night(segs, nights)
            after = None if spec.e3dc and alive is not None else spec.hold.zero
            segs = _zero_after_night(segs, points(spec), nights, after, vt, invalid.get(spec.measurement, ()), alive)
        if spec.categories == ("wallbox",):
            wallbox_specs.append((spec, vf, vt))
            timeline("wallbox", spec, vf, vt).segs += segs
            continue
        if len(spec.categories) == 1:
            timeline(spec.categories[0], spec, vf, vt).segs += segs
        else:
            pos_t, neg_t = timeline(spec.categories[0], spec, vf, vt), timeline(spec.categories[1], spec, vf, vt)
            pos_t.segs += [Segment(s.start, s.end, max(0.0, s.value)) for s in segs]
            neg_t.segs += [Segment(s.start, s.end, max(0.0, -s.value)) for s in segs]
            for p0, p1 in idle_phases:                # inferred idle: ≤ one SOC step net, marked reconstructed
                i = _slot_index(slots, p0)
                for tt in (pos_t, neg_t):
                    tt.reconstructed.append((p0, p1))
                    if i is not None:
                        tt.extra_unresolved[i] = tt.extra_unresolved.get(i, 0.0) + SOC_STEP_WH
                    for j, sl in enumerate(slots):
                        if sl.end > p0 and sl.start < p1:
                            tt.reasons.setdefault(j, set()).add("idle_by_soc")

    consumption = tl.get("consumption", _Timeline()).segs
    if "wallbox" in tl:
        _wallbox_rules(tl["wallbox"], slots, status_off, consumption, counter_tl.get("wallbox_counter", []))

    # grid at the billing meter by epoch (the meter from its epoch on, E3/DC before) – never by
    # availability: a meter outage stays unresolved instead of silently switching the measurement boundary
    for direction in ("import", "export"):
        meter, e3dc = tl.get(f"grid_{direction}_meter"), tl.get(f"grid_{direction}_e3dc")
        sel = _Timeline(p_max_w=max(meter.p_max_w if meter else 0.0, e3dc.p_max_w if e3dc else 0.0))
        for src, lo, hi in ((e3dc, start, min(end, IOMETER_FROM)), (meter, max(start, IOMETER_FROM), end)):
            if hi <= lo or src is None:                # no source at all (before the data start): not a gap
                continue
            sel.applicable.append((lo, hi))
            sel.segs += [Segment(max(s.start, lo), min(s.end, hi), s.value) for s in src.segs if s.end > lo and s.start < hi]
            sel.reconstructed += _intersect(src.reconstructed, [(lo, hi)])
            sel.filled += _intersect(src.filled, [(lo, hi)])
            if lo >= IOMETER_FROM:
                sel.method = "counter_delta"
        tl[f"grid_{direction}"] = sel

    categories = {cat: _slots(t, slots, start, end) for cat, t in tl.items() if cat != "battery_soc"}
    kpi_parts = _kpi_parts(tl, slots, start, end)
    timelines = {cat: HistoryTimeline(sorted(t.segs, key=lambda x: x.start),
                                      merge_intervals([(max(a, start), min(b, end)) for a, b in t.applicable
                                                       if min(b, end) > max(a, start)]),
                                      merge_intervals(t.reconstructed + t.filled))
                 for cat, t in tl.items() if cat in HISTORY_CATEGORIES}
    return DayResult(categories, kpi_parts, hwm_end, anomalies, spans_from, closed_at, counter_spans, timelines)


def _wallbox_rules(t: _Timeline, slots: Sequence[Slot], status_off: list[tuple[datetime, datetime]],
                   consumption: list[Segment], counter: list[Segment]) -> None:
    applicable = merge_intervals(t.applicable)
    segs = sorted(t.segs, key=lambda x: x.start)
    # status 'off' confirms zero in uncovered time
    segs = _fill(segs, _intersect(status_off, applicable))
    # values above the total consumption for ≥ 60 s: frozen/retained → cut (unknown, not zero)
    for a, b in exceeding(segs, consumption):
        segs = cut(segs, a, b)
    # K1: gaps resolved by the energy counter – only the counter budget not already measured as power
    filled: list[Segment] = []
    for c in counter:
        gaps = _intersect(uncovered(segs, c.start, c.end), applicable)
        if not gaps:
            continue
        budget = c.value * (c.end - c.start).total_seconds() / 3600.0
        remaining = max(0.0, budget - integrate(segs, c.start, c.end))
        gap_h = sum((b - a).total_seconds() for a, b in gaps) / 3600.0
        filled += [Segment(a, b, remaining / gap_h) for a, b in gaps]
        t.filled += gaps
        idx = _slot_index(slots, gaps[0][0])
        if idx is not None:
            if budget > 0:
                t.extra_unresolved[idx] = t.extra_unresolved.get(idx, 0.0) + COUNTER_RESOLUTION_WH
            for a, _b in gaps:
                j = _slot_index(slots, a)
                if j is not None:
                    t.reasons.setdefault(j, set()).add("counter_filled")
    t.segs = sorted(segs + filled, key=lambda x: x.start)
    t.bound = consumption


def _slot_index(slots: Sequence[Slot], t: datetime) -> int | None:
    for i, s in enumerate(slots):
        if s.start <= t < s.end:
            return i
    return None


def _slots(t: _Timeline, slots: Sequence[Slot], start: datetime, end: datetime) -> list[SlotEnergy]:
    applicable = merge_intervals([(max(a, start), min(b, end)) for a, b in t.applicable if min(b, end) > max(a, start)])
    segs = sorted(t.segs, key=lambda x: x.start)
    covered = _covered(segs)
    out = []
    for i, sl in enumerate(slots):
        app = _intersect(applicable, [(sl.start, sl.end)])
        app_s = sum((b - a).total_seconds() for a, b in app)
        if app_s <= 0:
            out.append(SlotEnergy(None, 0.0, 0.0, t.method, reasons={"future"} if sl.start >= end else {"no_source"}))
            continue
        pos = sum(max(sg.value, 0.0) * (min(sg.end, b) - max(sg.start, a)).total_seconds() / 3600.0
                  for a, b in app for sg in segs if sg.end > a and sg.start < b)
        cov_s = _seconds(_intersect(covered, app), sl.start, sl.end)
        gaps = [g for a, b in app for g in uncovered(segs, a, b)]
        unresolved = 0.0
        for a, b in gaps:
            known = 0.0
            for r in t.bound or []:
                s_, e_ = max(a, r.start), min(b, r.end)
                if e_ > s_:
                    unresolved += max(r.value, 0.0) * (e_ - s_).total_seconds() / 3600.0
                    known += (e_ - s_).total_seconds()
            unresolved += t.p_max_w * max(0.0, (b - a).total_seconds() - known) / 3600.0
        unresolved += t.extra_unresolved.get(i, 0.0)
        reasons = set(t.reasons.get(i, set()))
        if gaps:
            reasons.add("gap")
        if sl.end > end:
            reasons.add("partial")
        allocated = t.method == "counter_delta" and any(
            sg.value > 0 and (sg.start < sl.start or sg.end > sl.end) for sg in segs if sg.end > sl.start and sg.start < sl.end)
        recon = bool(_intersect(t.reconstructed, app))
        out.append(SlotEnergy(pos, min(1.0, cov_s / app_s), unresolved, t.method, allocated,
                              "reconstructed" if recon else "observed", reasons, app_s))
    return out


def _kpi_parts(tl: dict[str, _Timeline], slots: Sequence[Slot], start: datetime,
               end: datetime) -> dict[str, list[tuple[float, float, float, float]]]:
    """Numerator/denominator integrated on the intersection of the inputs' known intervals (plan §3.5 K1)."""
    def get(c: str) -> _Timeline:
        return tl.get(c, _Timeline())

    defs = {
        "autarky": (["consumption", "grid_import"], ["grid_import"], ["consumption"]),
        "self_consumption": (["pv_internal", "external", "grid_export"], ["grid_export"], ["pv_internal", "external"]),
    }
    out: dict[str, list[tuple[float, float, float, float]]] = {}
    for name, (inputs, num, den) in defs.items():
        joint = _covered(get(inputs[0]).segs)
        for c in inputs[1:]:
            joint = _intersect(joint, _covered(get(c).segs))
        rows = []
        for sl in slots:
            app_s = max(0.0, (min(sl.end, end) - max(sl.start, start)).total_seconds())
            iv = _intersect(joint, [(sl.start, min(sl.end, end))]) if app_s > 0 else []
            n = sum(integrate(get(c).segs, a, b) for c in num for a, b in iv)
            d = sum(integrate(get(c).segs, a, b) for c in den for a, b in iv)
            rows.append((n, d, sum((b - a).total_seconds() for a, b in iv), app_s))
        out[name] = rows
    return out


def total(slots: list[SlotEnergy]) -> tuple[float, float, float]:
    """(known Wh, coverage = covered / applicable seconds, unresolved Wh upper bound)."""
    applicable = [s for s in slots if s.wh is not None]
    if not applicable:
        return 0.0, 0.0, 0.0
    return (sum(s.wh or 0.0 for s in applicable), coverage(applicable), sum(s.unresolved_wh for s in applicable))


def combine_slots(a: SlotEnergy, b: SlotEnergy, sign: int) -> SlotEnergy:
    """Derived category per slot: guaranteed joint coverage (covered seconds of both minus the applicable time),
    not min() – disjoint halves have no joint coverage (Codex P5 finding 3). Always formed per 15-min slot, so day,
    week, month and year report the same coverage (aggregates store the derived categories)."""
    wh = None if a.wh is None or b.wh is None else a.wh + sign * b.wh
    app = max(a.applicable_s, b.applicable_s)
    cov = max(0.0, a.covered_s + b.covered_s - app) / app if app > 0 and wh is not None else 0.0
    return SlotEnergy(wh, min(1.0, cov), a.unresolved_wh + b.unresolved_wh, a.method,
                      a.time_allocated or b.time_allocated,
                      "reconstructed" if "reconstructed" in (a.confidence, b.confidence) else "observed",
                      a.reasons | b.reasons, app if wh is not None else 0.0)


DERIVED = {"generation": ("pv_internal", "external", 1), "home": ("consumption", "wallbox", -1)}
# Origin of the consumption (energy view bars for week/month/year, user decision 2026-10-05)
SUPPLY = ("supply_grid", "supply_battery", "supply_pv_internal", "supply_external")
DERIVED_CATEGORIES = (*DERIVED, *SUPPLY)


def _joint(parts: Sequence[SlotEnergy], wh: float, method: str) -> SlotEnergy:
    """Derived slot from several inputs: guaranteed joint coverage Σ covered − (k − 1) · applicable (the k-input form
    of ``combine_slots``); inputs without a source are left out."""
    known = [p for p in parts if p.wh is not None]
    app = max(p.applicable_s for p in known)
    cov = max(0.0, sum(p.covered_s for p in known) - (len(known) - 1) * app) / app if app > 0 else 0.0
    return SlotEnergy(wh, min(1.0, cov), sum(p.unresolved_wh for p in known), method,
                      any(p.time_allocated for p in known),
                      "reconstructed" if any(p.confidence == "reconstructed" for p in known) else "observed",
                      set().union(*(p.reasons for p in known)), app)


def supply_slot(cons: SlotEnergy, imp: SlotEnergy, dis: SlotEnergy, pv: SlotEnergy, ext: SlotEnergy) -> dict[str, SlotEnergy]:
    """Where the slot's consumption came from: grid import first (as in the autarky), then battery discharge – each
    at most the remaining consumption, so charging from the grid or exporting from the battery is never counted as
    supply – the rest directly from generation, at most PV intern + KOSTAL (a larger rest is balance residual, never
    distributed) and split by their shares in the slot. Per 15-min slot, so every period sums the same allocation
    (Codex review 2026-10-05). An accounting convention, not measured provenance. A flow explicitly without a source
    counts as 0; consumption without one, or a slot that has not begun, gives no value."""
    inputs = (cons, imp, dis, pv, ext)
    if cons.wh is None or any(e.wh is None and "no_source" not in e.reasons for e in inputs):
        return {k: SlotEnergy(None, 0.0, reasons=set().union(*(e.reasons for e in inputs if e.wh is None)))
                for k in SUPPLY}
    def wh(e: SlotEnergy) -> float:
        return max(0.0, e.wh or 0.0)
    c = wh(cons)
    grid = min(wh(imp), c)
    battery = min(wh(dis), c - grid)
    gen = wh(pv) + wh(ext)
    direct = min(c - grid - battery, gen)
    pv_share = direct * wh(pv) / gen if gen > 0 else 0.0
    return {
        "supply_grid": _joint([cons, imp], grid, cons.method),
        "supply_battery": _joint([cons, imp, dis], battery, cons.method),
        "supply_pv_internal": _joint([cons, imp, dis, pv, ext], pv_share, cons.method),
        "supply_external": _joint([cons, imp, dis, pv, ext], direct - pv_share, cons.method),
    }


def derived_slots(categories: dict[str, list[SlotEnergy]], n: int) -> dict[str, list[SlotEnergy]]:
    pad = [SlotEnergy(None, 0.0, reasons={"no_source"}) for _ in range(n)]
    out = {name: [combine_slots(x, y, sign) for x, y in zip(categories.get(a) or pad, categories.get(b) or pad, strict=True)]
           for name, (a, b, sign) in DERIVED.items()}
    rows = [supply_slot(*s) for s in zip(*(categories.get(c) or pad for c in
                                           ("consumption", "grid_import", "battery_discharge", "pv_internal", "external")),
                                         strict=True)]
    out |= {k: [r[k] for r in rows] for k in SUPPLY}
    return out


def coverage(parts: Sequence[SlotEnergy]) -> float:
    """Covered / applicable seconds over the parts with a source (weights DST days, partial days and epoch
    boundaries correctly – Codex P5 finding 3)."""
    known = [p for p in parts if p.wh is not None]
    app = sum(p.applicable_s for p in known)
    return min(1.0, sum(p.covered_s for p in known) / app) if app > 0 else 0.0
