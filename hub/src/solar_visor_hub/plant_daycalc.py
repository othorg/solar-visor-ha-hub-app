"""Generic day calculation for any plant (plan ha-addon P3 step 3). Pure: no I/O, no clock.

Input: the plant, the day's slots and, per data point, what was read from the recorder (``PointData``). Output: energy
per 15-min slot for every category of the plant, under v2 ids, with coverage, method and source – the same
``SlotEnergy`` the existing calculation produces, so totals, KPIs and rings can be built the same way for both.

Tiers (P3 detail plan §4, user decisions 2026-11-15):
- ``raw`` – the day lies in the recorder's keep window: 5-minute statistics for power without a sign and for counters
  (time-weighted means, measured equal to the existing sample-and-hold within 0–1 Wh per day, P0), raw states for power
  with a sign (grid, battery: the directions are split per hold segment – means would mix them);
- ``hour`` – older days: hourly statistics, spread evenly over the four quarter hours of each hour
  (``time_allocated``); a signed power gives only its balance (``<module>.net``), the directions stay ``null``.

Source order per category is fixed by the plant, never switched on failure: a counter (``energy_total``,
``import_energy``/``export_energy``) wins over a power value; the battery's daily counters are display only.
Unknown stays ``null``; a slot before a point's ``valid_from`` has no source (not 0).

Statistics are written every period while a sensor is available, also when its value does not change (measured
2026-11-15: an idle counter, 863 of 864 rows in three days) – a missing row means unknown. A counter therefore holds
its last reading for one period only (the next row may still be compiled), never across a source outage.
PV is zero while the sun is below the horizon (``nights``, from the location of Home Assistant): the night rule of the
existing calculation, so an inverter that reports ``unavailable`` at night leaves no gap.

A PV generator with full feed-in keeps its own category (``pv_n``, read and night-filled like any generator) but is no
term of ``generation``, the consumption, ``supply.*`` or the KPIs; ``full_feed_in`` is the sum of those generators –
derived, only on days whose plant has one. A plant without one computes exactly as before (no new category, same
order, same source key).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .daycalc import SlotEnergy, _fill, _intersect, _joint, _slots, _Timeline
from .energy import HoldRule, Point as EPoint, Segment, counter_timeline, cut, hold_segments
from .ha_history import StatRow
from .periods import Slot
from .plant import Kind, Plant
from .plant import Point as PlantPoint

TIER_RAW, TIER_HOUR = "raw", "hour"
RESOLUTION_S = {TIER_RAW: 900, TIER_HOUR: 3600}
STAT_PERIOD = {TIER_RAW: "5minute", TIER_HOUR: "hour"}
# raw states of a signed power: a value holds until the next one or an unavailable mark, at most this long (HA writes
# on change only; unmeasured defaults, P3 detail plan T5)
RAW_HOLD = HoldRule(nonzero=timedelta(hours=1), zero=timedelta(hours=48))
LEAD_IN = timedelta(hours=1)          # raw states are read from this long before the day: the carry-in
COUNTER_LEAD = timedelta(hours=24)    # counter rows before the day: their highest reading is the base (rollbacks)
P_MAX_W = {Kind.GRID: 50_000.0, Kind.PV: 50_000.0, Kind.BATTERY: 50_000.0, Kind.WALLBOX: 22_000.0,
           Kind.GENERIC: 25_000.0, Kind.CONSUMPTION: 50_000.0}
NO_SOURCE = "no_source"


@dataclass(frozen=True)
class PointData:
    """What was read for one data point: statistics rows of the tier's period (power: ``mean``, counter: ``sum``;
    counters include the last row before the day as base) and/or raw states as canonical values (sign applied,
    ``None`` = unavailable), carry-in first."""
    stats: Sequence[StatRow] | None = None
    raw: Sequence[EPoint] | None = None


@dataclass(frozen=True)
class CategorySource:
    method: str                 # "statistic_mean" | "counter_delta" | "sample_hold" | "derived"
    metric_id: str | None       # data point it came from (derived: None)
    resolution_s: int


@dataclass
class PlantDay:
    tier: str
    resolution_s: int
    categories: dict[str, list[SlotEnergy]]
    sources: dict[str, CategorySource]
    kpi_parts: dict[str, list[tuple[float, float, float, float]]]
    counter_anomalies: dict[str, int] = field(default_factory=dict)
    stale: bool = False         # an older archived state, served while Home Assistant cannot be read (not archived)


def _empty(n: int, reason: str) -> list[SlotEnergy]:
    return [SlotEnergy(None, 0.0, reasons={reason}) for _ in range(n)]


def _applicable(point: PlantPoint, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    a = max(start, point.valid_from) if point.valid_from else start
    return [(a, end)] if end > a else []


def _means(rows: Sequence[StatRow], start: datetime, end: datetime, sign: int) -> list[Segment]:
    out = []
    for r in rows:
        if r.mean is None:
            continue
        s, e = max(r.start, start), min(r.end, end)
        if e > s:
            out.append(Segment(s, e, r.mean * sign))
    return sorted(out, key=lambda x: x.start)


def _clip(segs: list[Segment], intervals: list[tuple[datetime, datetime]]) -> list[Segment]:
    """The parts of the segments inside the intervals (a point's validity)."""
    return [Segment(max(s.start, a), min(s.end, b), s.value) for s in segs for a, b in intervals
            if min(s.end, b) > max(s.start, a)]


def _slots_of(segs: list[Segment], applicable: list[tuple[datetime, datetime]], slots: Sequence[Slot],
              start: datetime, end: datetime, p_max: float, method: str, allocated: bool) -> list[SlotEnergy]:
    out = _slots(_Timeline(segs=segs, applicable=applicable, p_max_w=p_max, method=method), slots, start, end)
    for e in out:
        if e.wh is not None and e.coverage == 0.0:
            # applicable but nothing measured: unknown, not 0 (the existing calculation reports 0 with coverage 0
            # here and the app corrects it – v2 says it, P3 finding 5)
            e.wh = None
            e.reasons = (e.reasons - {"gap"}) | {"no_data"}
        elif allocated and e.wh is not None:
            e.time_allocated = True
    return out


def _positive(segs: list[Segment], sign: int) -> list[Segment]:
    """One direction of a signed series: ``sign=+1`` the positive part, ``-1`` the magnitude of the negative part
    (covered wherever the series is – a zero is a known zero)."""
    return [Segment(s.start, s.end, max(s.value * sign, 0.0)) for s in segs]


def _sum(name: str, parts: list[tuple[list[SlotEnergy], int]], method: str = "derived") -> list[SlotEnergy]:
    """Signed sum per slot. A part without a source in a slot is absent (a generator not yet installed); a part that
    is unknown makes the slot unknown – never 0."""
    n = len(parts[0][0]) if parts else 0
    out = []
    for i in range(n):
        row = [(p[i], sign) for p, sign in parts]
        if any(e.wh is None and NO_SOURCE not in e.reasons for e, _ in row):
            out.append(SlotEnergy(None, 0.0, reasons=set().union(*(e.reasons for e, _ in row if e.wh is None)) - {NO_SOURCE}
                                  or {"input_unknown"}))
            continue
        known = [(e, sign) for e, sign in row if e.wh is not None]
        if not known:
            out.append(SlotEnergy(None, 0.0, reasons={NO_SOURCE}))
            continue
        wh = sum(e.wh * sign for e, sign in known)      # type: ignore[operator]
        out.append(_joint([e for e, _ in known], wh, method))
    return out


def compute_day(plant: Plant, slots: Sequence[Slot], start: datetime, end: datetime, tier: str,
                data: dict[str, PointData], invalid: dict[str, list[tuple[datetime, datetime]]] | None = None,
                nights: list[tuple[datetime, datetime]] | None = None) -> PlantDay:
    """Energy of [start, end) – ``end`` is the end of the day or now for the running day; slots after it are
    ``future``. ``data`` is keyed by metric id (``pv_1.power``); ``invalid`` by entity (journal: source outages);
    ``nights``: sun below the horizon (PV zero), None without a known location."""
    n = len(slots)
    points = plant.points()
    allocated = tier == TIER_HOUR
    cats: dict[str, list[SlotEnergy]] = {}
    sources: dict[str, CategorySource] = {}
    anomalies: dict[str, int] = {}
    res = RESOLUTION_S[tier]
    period = timedelta(seconds=300 if tier == TIER_RAW else 3600)
    invalid = invalid or {}

    def night(segs: list[Segment], kind: Kind, app: list[tuple[datetime, datetime]]) -> list[Segment]:
        return _fill(segs, _intersect(nights, app)) if kind is Kind.PV and nights else segs

    def power(cat: str, metric_id: str, kind: Kind) -> None:
        """Energy of a power without a sign from statistic means (both tiers)."""
        point, d = points[metric_id], data.get(metric_id)
        app = _applicable(point, start, end)
        means = _means(d.stats, start, end, point.sign) if d is not None and d.stats is not None else []
        cats[cat] = _slots_of(night(means, kind, app), app, slots, start, end, P_MAX_W[kind], "statistic_mean",
                              allocated)
        sources[cat] = CategorySource("statistic_mean", metric_id, int(period.total_seconds()))

    def counter(cat: str, metric_id: str, kind: Kind) -> None:
        point, d = points[metric_id], data.get(metric_id)
        app = _applicable(point, start, end)
        rows = sorted(d.stats or [], key=lambda r: r.end) if d else []
        pts = [EPoint(r.end, r.sum * point.sign) for r in rows if r.sum is not None]
        # base: the highest reading before the day – a dip just before midnight is a rollback, not new energy
        base = max((p for p in pts if p.t <= start), key=lambda p: (p.value, p.t), default=None)
        tl = counter_timeline([p for p in pts if p.t > start], base, P_MAX_W[kind], start, end, hold=period)
        hold = tl.hold
        for a, b in invalid.get(point.entity, ()):     # no "unchanged" while the source was out
            hold = cut(hold, a, b)
        anomalies[cat] = tl.anomalies + tl.implausible
        cats[cat] = _slots_of(night(sorted(tl.segments + hold, key=lambda x: x.start), kind, app), app, slots, start,
                              end, P_MAX_W[kind], "counter_delta", allocated)
        sources[cat] = CategorySource("counter_delta", metric_id, int(period.total_seconds()))

    def signed(prefix: str, metric_id: str, kind: Kind) -> None:
        """A power with a sign (grid: + import, battery: + charge) → ``<prefix>.in``/``.out`` and ``.net``."""
        point, d = points[metric_id], data.get(metric_id)
        app = _applicable(point, start, end)
        up, down = ("import", "export") if prefix == "grid" else ("charge", "discharge")
        if tier == TIER_RAW:
            # raw states (none read: every elapsed slot is unknown – "no_data" – and later ones "future")
            raw = list(d.raw) if d is not None and d.raw is not None else []
            segs = hold_segments(raw, start, end, RAW_HOLD, invalid.get(point.entity, ()))
            for name, sign in ((up, 1), (down, -1)):
                cats[f"{prefix}.{name}"] = _slots_of(_positive(segs, sign), app, slots, start, end, P_MAX_W[kind],
                                                     "sample_hold", False)
                sources[f"{prefix}.{name}"] = CategorySource("sample_hold", metric_id, 0)
            cats[f"{prefix}.net"] = _sum(f"{prefix}.net", [(cats[f"{prefix}.{up}"], 1), (cats[f"{prefix}.{down}"], -1)])
            sources[f"{prefix}.net"] = CategorySource("sample_hold", metric_id, 0)
            return
        # hour tier: the balance only – the directions cannot be told apart from means (user decision 2026-11-15, 2)
        means = _clip(_means(d.stats, start, end, point.sign), app) if d is not None and d.stats is not None else []
        net = _slots_of(means, app, slots, start, end, P_MAX_W[kind], "statistic_mean", allocated)
        cats[f"{prefix}.net"] = [_net(e, means, s) for e, s in zip(net, slots, strict=True)]
        sources[f"{prefix}.net"] = CategorySource("statistic_mean", metric_id, 3600)
        for name in (up, down):
            # what is not determinable is said per slot – only where a value was due (not before the point's
            # validity, not in the future)
            cats[f"{prefix}.{name}"] = [SlotEnergy(None, 0.0, reasons={"future"} if sl.start >= end else
                                                   {"signed_needs_raw"} if any(a <= sl.start < b for a, b in app)
                                                   else {NO_SOURCE}) for sl in slots]

    # --- generators (every PV has its category; the home balance sees only those that feed the home) -------------
    for m in plant.active():
        if m.kind is not Kind.PV:
            continue
        if f"{m.id}.energy_total" in points:
            counter(m.id, f"{m.id}.energy_total", Kind.PV)
        else:
            power(m.id, f"{m.id}.power", Kind.PV)
    generators = plant.generators()
    cats["generation"] = _sum("generation", [(cats[m.id], 1) for m in generators])
    sources["generation"] = CategorySource("derived", None, res)
    if feed_in := plant.feed_in():
        cats["full_feed_in"] = _sum("full_feed_in", [(cats[m.id], 1) for m in feed_in])
        sources["full_feed_in"] = CategorySource("derived", None, res)

    # --- grid ---------------------------------------------------------------------------------------------------
    grid = plant.modules["grid"]
    if "grid.import_energy" in points and "grid.export_energy" in points:
        counter("grid.import", "grid.import_energy", Kind.GRID)
        counter("grid.export", "grid.export_energy", Kind.GRID)
    elif grid.variant == "split":
        power("grid.import", "grid.import_power", Kind.GRID)
        power("grid.export", "grid.export_power", Kind.GRID)
    else:
        signed("grid", "grid.power", Kind.GRID)
    if "grid.net" not in cats:
        cats["grid.net"] = _sum("grid.net", [(cats["grid.import"], 1), (cats["grid.export"], -1)])
        sources["grid.net"] = CategorySource("derived", None, res)

    # --- battery (the daily counters of many devices are display only) ----------------------------------------
    if plant.modules["battery"].enabled:
        signed("battery", "battery.power", Kind.BATTERY)

    # --- consumers ------------------------------------------------------------------------------------------------
    for m in plant.active():
        if m.kind in (Kind.WALLBOX, Kind.GENERIC):
            if f"{m.id}.energy_total" in points:
                counter(m.id, f"{m.id}.energy_total", m.kind)
            else:
                power(m.id, f"{m.id}.power", m.kind)

    measured = "consumption.power" in points
    if measured:
        power("consumption", "consumption.power", Kind.CONSUMPTION)
    else:
        terms = [(cats["generation"], 1), (cats["grid.net"], 1)]
        if "battery.net" in cats:
            terms.append((cats["battery.net"], -1))
        cats["consumption"] = _sum("consumption", terms)
        sources["consumption"] = CategorySource("derived", None, res)

    beside = [cats[m.id] for m in plant.active() if m.kind is Kind.WALLBOX
              or (m.kind is Kind.GENERIC and m.balance == "beside_home")]
    cats["home"] = _sum("home", [(cats["consumption"], 1), *((c, -1) for c in beside)])
    sources["home"] = CategorySource("derived", None, res)

    _supply(cats, [m.id for m in generators], n)
    for k in cats:
        if k.startswith("supply."):
            sources[k] = CategorySource("derived", None, res)
    return PlantDay(tier=tier, resolution_s=res, categories=cats, sources=sources,
                    kpi_parts=_kpi_parts(cats, n), counter_anomalies={k: v for k, v in anomalies.items() if v})


def requirements(plant: Plant, tier: str) -> dict[str, set[str]]:
    """What ``compute_day`` reads for a plant and tier: metric id → {"mean", "sum", "raw"}. Mirrors the source order
    above – kept next to it so both change together (``test_plant_daycalc`` checks that nothing else is read)."""
    points = plant.points()
    need: dict[str, set[str]] = {}

    def add(metric_id: str, kind: str) -> None:
        need.setdefault(metric_id, set()).add(kind)

    def power_or_counter(module_id: str) -> None:
        if f"{module_id}.energy_total" in points:
            add(f"{module_id}.energy_total", "sum")
        else:
            add(f"{module_id}.power", "mean")

    def signed(metric_id: str) -> None:
        add(metric_id, "raw" if tier == TIER_RAW else "mean")

    for m in plant.active():
        if m.kind in (Kind.PV, Kind.WALLBOX, Kind.GENERIC):
            power_or_counter(m.id)
    if "grid.import_energy" in points and "grid.export_energy" in points:
        add("grid.import_energy", "sum")
        add("grid.export_energy", "sum")
    elif plant.modules["grid"].variant == "split":
        add("grid.import_power", "mean")
        add("grid.export_power", "mean")
    else:
        signed("grid.power")
    if plant.modules["battery"].enabled:
        signed("battery.power")
    if "consumption.power" in points:
        add("consumption.power", "mean")
    return need


def _net(e: SlotEnergy, means: list[Segment], slot: Slot) -> SlotEnergy:
    """``_slots`` keeps only the positive part (a direction); the balance of a signed mean is the signed integral."""
    if e.wh is None:
        return e
    wh = 0.0
    for s in means:
        a, b = max(s.start, slot.start), min(s.end, slot.end)
        if b > a:
            wh += s.value * (b - a).total_seconds() / 3600.0
    e.wh = wh
    return e


def _supply(cats: dict[str, list[SlotEnergy]], generators: list[str], n: int) -> None:
    """Where the consumption came from, per slot: grid import first, then battery discharge (each at most the rest of
    the consumption), the rest directly from generation, split by the generators' shares – the convention of the
    existing calculation (daycalc.supply_slot), for any number of generators. Unknown directions → unknown."""
    zero = SlotEnergy(0.0, 1.0, reasons={NO_SOURCE})
    keys = ["supply.grid", "supply.battery", *(f"supply.{g}" for g in generators)]
    out: dict[str, list[SlotEnergy]] = {k: [] for k in keys}
    for i in range(n):
        cons, imp = cats["consumption"][i], cats["grid.import"][i]
        dis = cats["battery.discharge"][i] if "battery.discharge" in cats else zero
        gens = [cats[g][i] for g in generators]
        inputs = [cons, imp, dis, *gens]
        if cons.wh is None or any(e.wh is None and NO_SOURCE not in e.reasons for e in inputs):
            reasons = set().union(*(e.reasons for e in inputs if e.wh is None)) or {"input_unknown"}
            for k in keys:
                out[k].append(SlotEnergy(None, 0.0, reasons=reasons))
            continue

        def wh(e: SlotEnergy) -> float:
            return max(0.0, e.wh or 0.0)

        c = wh(cons)
        g = min(wh(imp), c)
        b = min(wh(dis), c - g)
        total = sum(wh(e) for e in gens)
        direct = min(c - g - b, total)
        known = [e for e in inputs if e.wh is not None and e is not zero]
        out["supply.grid"].append(_joint([cons, imp], g, cons.method))
        out["supply.battery"].append(_joint([e for e in (cons, imp, dis) if e.wh is not None and e is not zero], b,
                                            cons.method))
        for gid, e in zip(generators, gens, strict=True):
            share = direct * wh(e) / total if total > 0 else 0.0
            out[f"supply.{gid}"].append(_joint(known, share, cons.method))
    cats.update(out)


def _kpi_parts(cats: dict[str, list[SlotEnergy]], n: int) -> dict[str, list[tuple[float, float, float, float]]]:
    """Per slot (numerator Wh, denominator Wh, joint seconds, applicable seconds) – autarky = 1 − import /
    consumption, self-consumption = 1 − export / generation; a slot counts only with both inputs known.

    Numerator and denominator stand for the same time: the guaranteed joint coverage of the slot. Each input's energy
    is scaled to it as if spread evenly over its covered time – inside one quarter hour (the existing calculation
    integrates both on the intersection of its timelines; the generic one keeps slots, Codex P3 review M1)."""
    defs = {"autarky": ("grid.import", "consumption"), "self_consumption": ("grid.export", "generation")}
    out: dict[str, list[tuple[float, float, float, float]]] = {}
    for name, (num, den) in defs.items():
        rows = []
        for i in range(n):
            a, b = cats[num][i], cats[den][i]
            app = max(a.applicable_s, b.applicable_s)
            if a.wh is None or b.wh is None or app <= 0:
                rows.append((0.0, 0.0, 0.0, app))
                continue
            joint = max(0.0, a.covered_s + b.covered_s - app)
            if joint <= 0:
                rows.append((0.0, 0.0, 0.0, app))
                continue
            rows.append((a.wh * min(1.0, joint / a.covered_s), b.wh * min(1.0, joint / b.covered_s), joint, app))
        out[name] = rows
    return out
