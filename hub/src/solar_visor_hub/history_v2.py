"""Energy periods for /v2 (plan ha-addon P3 step 5) from either calculation.

- The existing plant (legacy profile): the unchanged existing calculation produces its period (energy_api), which is
  renamed to v2 ids here – equal to /v1 by construction, nothing recomputed, with one exception: where /v1 reports
  0 Wh with coverage 0 (nothing measured; the app corrects it), /v2 says null (Codex P3 review H1).
- Any other plant: days from the plant's history service (plant_history.py), assembled into buckets, totals, KPIs and
  rings with the same rules as the existing assembly (bucket sizes per period, coverage, 99.5 % for complete).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .daycalc import SlotEnergy
from .kpis import FULL, ring
from .models import EnergyPeriod
from .models_v2 import (
    PlantEnergyBucket,
    PlantEnergyKpis,
    PlantEnergyMethod,
    PlantEnergyPeriod,
    PlantEnergySource,
    PlantEnergyTotals,
)
from .periods import Slot, days, period_bounds, quarter_hours
from .plant_archive import CALCULATION_VERSION as GENERIC_VERSION
from .plant_daycalc import PlantDay

WEEK_BLOCK_HOURS = 6
PROVISIONAL = timedelta(minutes=2)

# --- existing plant ------------------------------------------------------------------------------------------------

LEGACY_CATEGORIES = {
    "pv_internal": "pv_1", "external": "pv_2", "generation": "generation", "consumption": "consumption",
    "wallbox": "wallbox", "home": "home", "battery_charge": "battery.charge", "battery_discharge": "battery.discharge",
    "grid_import": "grid.import", "grid_export": "grid.export",
    "mppt1": "pv_2.string_1", "mppt2": "pv_2.string_2", "mppt3": "pv_2.string_3",
    "supply_grid": "supply.grid", "supply_battery": "supply.battery", "supply_pv_internal": "supply.pv_1",
    "supply_external": "supply.pv_2",
}
LEGACY_TOTALS = {"pv_internal_wh": "pv_1", "external_wh": "pv_2", "generation_wh": "generation",
                 "consumption_wh": "consumption", "wallbox_wh": "wallbox", "home_wh": "home",
                 "battery_charge_wh": "battery.charge", "battery_discharge_wh": "battery.discharge",
                 "grid_import_wh": "grid.import", "grid_export_wh": "grid.export"}


def measured(energy_wh: float | None, coverage: float | None) -> float | None:
    """/v1 keeps 0 Wh for a pot without any measurement (coverage 0 or none); /v2 says unknown. A value other than 0 with
    coverage 0 stays: a derived pot whose inputs were measured at different times (guaranteed joint coverage 0) –
    bars and totals keep adding up."""
    return None if energy_wh == 0.0 and not coverage else energy_wh


def from_legacy(p: EnergyPeriod, plant_revision: int) -> PlantEnergyPeriod:
    """The existing calculation's period under v2 ids (coverage and quality untouched, unmeasured energy null)."""
    buckets = {LEGACY_CATEGORIES[c]: [PlantEnergyBucket(
        start=b.start, end=b.end, local_label=b.local_label, utc_offset_minutes=b.utc_offset_minutes,
        energy_wh=measured(b.energy_wh, b.coverage_fraction), method=PlantEnergyMethod(b.method.value),
        coverage_fraction=b.coverage_fraction,
        complete=b.complete, provisional=b.provisional, unresolved_wh_max=b.unresolved_wh_max,
        source_resolution_s=b.source_resolution_s, allocation=b.allocation.value,
        calculation_version=b.calculation_version, quality_reasons=b.quality_reasons) for b in series]
        for c, series in p.buckets.items() if c in LEGACY_CATEGORIES}
    t = p.totals
    energy = {new: measured(getattr(t, old), t.coverage.get(old.removesuffix("_wh"))) for old, new in LEGACY_TOTALS.items()}
    rename = {**{k.removesuffix("_wh"): v for k, v in LEGACY_TOTALS.items()}, **LEGACY_CATEGORIES}
    sources = {cat: PlantEnergySource(method="existing_calculation") for cat in energy}
    for cat in ("grid.import", "grid.export"):
        sources[cat] = PlantEnergySource(method="existing_calculation", mixed=t.grid_source == "mixed", note=t.grid_source)
    k = p.kpis
    return PlantEnergyPeriod(
        period=p.period, local_date=p.local_date, timezone=p.timezone, plant_revision=plant_revision,
        calculation_version=max((b.calculation_version for s in buckets.values() for b in s), default=0),
        buckets=buckets,
        totals=PlantEnergyTotals(energy_wh=energy,
                                 coverage={rename.get(c, c): v for c, v in t.coverage.items()},
                                 unresolved_wh_max={rename.get(c, c): v for c, v in t.unresolved_wh_max.items()},
                                 sources=sources),
        kpis=PlantEnergyKpis(
            autarky_pct=k.autarky_pct, self_consumption_pct=k.self_consumption_pct, autarky_coverage=k.autarky_coverage,
            self_consumption_coverage=k.self_consumption_coverage, direct_generation_side_wh=k.direct_generation_side_wh,
            direct_consumption_side_wh=k.direct_consumption_side_wh, balance_residual_wh=k.balance_residual_wh,
            residual_warning=k.residual_warning, cross_check_residual_wh=k.e3dc_balance_residual_wh,
            generation_ring=k.generation_ring, consumption_ring=k.consumption_ring),
        complete=p.complete, refreshing=p.refreshing)


# --- any other plant -----------------------------------------------------------------------------------------------

ABSENT = ("no_source", "future")


def _cov(parts: Sequence[SlotEnergy], durations: Sequence[float]) -> float:
    """Covered / applicable seconds where a slot that should have a value but is unknown counts as applicable and
    uncovered (the coverage of the known slots alone would overstate it)."""
    covered = app = 0.0
    for p, dur in zip(parts, durations, strict=True):
        if p.wh is not None:
            covered += p.covered_s
            app += p.applicable_s
        elif not p.reasons & set(ABSENT):
            app += dur
    return min(1.0, covered / app) if app > 0 else 0.0


def _method(parts: Sequence[SlotEnergy]) -> PlantEnergyMethod:
    methods = {p.method for p in parts if p.wh is not None}
    if len(methods) == 1:
        m = methods.pop()
        return PlantEnergyMethod(m) if m in PlantEnergyMethod._value2member_map_ else PlantEnergyMethod.DERIVED
    return PlantEnergyMethod.DERIVED


def _due(p: SlotEnergy) -> bool:
    """The slot has a value or should have one (not before a source existed, not in the future)."""
    return p.wh is not None or not p.reasons & set(ABSENT)


def _bucket(start: datetime, end: datetime, label: str, offset: int, parts: Sequence[SlotEnergy], now: datetime,
            resolution_s: int | None, durations: Sequence[float]) -> PlantEnergyBucket:
    known = [p for p in parts if p.wh is not None]
    cov = _cov(parts, durations)
    return PlantEnergyBucket(
        start=start, end=end, local_label=label, utc_offset_minutes=offset,
        energy_wh=round(sum(p.wh or 0.0 for p in known), 1) if known else None, method=_method(parts),
        coverage_fraction=round(min(1.0, cov), 4), complete=bool(known) and cov >= FULL,
        provisional=end > now - PROVISIONAL,
        unresolved_wh_max=round(sum(p.unresolved_wh for p in parts if _due(p)), 1) or None,
        source_resolution_s=resolution_s,
        allocation="time_allocated" if any(p.time_allocated for p in known) else "observed",
        calculation_version=GENERIC_VERSION, quality_reasons=sorted(set().union(*(p.reasons for p in parts))) if parts else [])


def _pad(n: int, future: bool) -> list[SlotEnergy]:
    return [SlotEnergy(None, 0.0, reasons={"future" if future else "no_source"}) for _ in range(n)]


def plant_period(period: str, d: date, tz: ZoneInfo, plant_days: Sequence[tuple[date, PlantDay]], now: datetime,
                 plant_revision: int, measured_consumption: bool) -> PlantEnergyPeriod:
    """``plant_days``: every local day of the period with its computed day (days that have not begun included)."""
    names: list[str] = []
    for _, pd in plant_days:
        for c in pd.categories:
            if c not in names:
                names.append(c)
    per_day: list[tuple[date, list[Slot], dict[str, list[SlotEnergy]], dict[str, int]]] = []
    for dd, pd in plant_days:
        start, end = period_bounds("day", dd, tz)
        slots = quarter_hours(start, end, tz)
        future = start >= now
        cats = {c: pd.categories.get(c) or _pad(len(slots), future) for c in names}
        res = {c: pd.sources[c].resolution_s for c in pd.sources}
        per_day.append((dd, slots, cats, res))

    def resolution(c: str, idx: Sequence[int]) -> int | None:
        values = {per_day[i][3].get(c) for i in idx} - {None, 0}
        return max(values) if values else None

    buckets: dict[str, list[PlantEnergyBucket]] = {}
    if period in ("day", "week"):
        slots_all = [(i, s) for i, (_, slots, _, _) in enumerate(per_day) for s in slots]
        groups: dict[str, list[int]] = {}
        for k, (_, s) in enumerate(slots_all):
            local = s.start.astimezone(tz)
            key = (s.local_label if period == "day"
                   else f"{local.date().isoformat()}T{local.hour // WEEK_BLOCK_HOURS * WEEK_BLOCK_HOURS:02d}:00")
            groups.setdefault(key if period == "week" else str(k), []).append(k)
        flat = {c: [e for _, _, cats, _ in per_day for e in cats[c]] for c in names}
        durations = [(s.end - s.start).total_seconds() for _, s in slots_all]
        for c in names:
            buckets[c] = []
            for key, idx in groups.items():
                first, last = slots_all[idx[0]][1], slots_all[idx[-1]][1]
                label = first.local_label if period == "day" else key
                buckets[c].append(_bucket(first.start, last.end, label, first.utc_offset_minutes,
                                          [flat[c][k] for k in idx], now,
                                          resolution(c, sorted({slots_all[k][0] for k in idx})),
                                          [durations[k] for k in idx]))
    else:
        groups = {}
        for i, (dd, _, _, _) in enumerate(per_day):
            iso = dd.isocalendar()
            groups.setdefault(f"{iso.year}-W{iso.week:02d}" if period == "year" else dd.isoformat(), []).append(i)
        for c in names:
            buckets[c] = []
            for key, idx in groups.items():
                first, last = per_day[idx[0]][1][0], per_day[idx[-1]][1][-1]
                slot_parts = [e for i in idx for e in per_day[i][2][c]]
                durs = [(sl.end - sl.start).total_seconds() for i in idx for sl in per_day[i][1]]
                buckets[c].append(_bucket(first.start, last.end, key, first.utc_offset_minutes, slot_parts, now,
                                          resolution(c, idx), durs))

    totals, kpis = _totals_and_kpis(names, per_day, plant_days, now, measured_consumption)
    elapsed = any(s.start < now for _, slots, _, _ in per_day for s in slots)
    # measured categories decide completeness – also one without a single known value (Codex P3 review M2)
    base = [c for c in names if not c.startswith("supply.") and c not in ("generation", "home")
            and totals.sources.get(c, PlantEnergySource(method="derived")).method != "derived"]
    due = {c: any(_due(e) for _, _, cats, _ in per_day for e in cats[c]) for c in base}
    complete = elapsed and all(not due[c] or (totals.coverage.get(c) or 0.0) >= FULL for c in base)
    return PlantEnergyPeriod(period=period, local_date=d.isoformat(), timezone=tz.key, plant_revision=plant_revision,
                             calculation_version=GENERIC_VERSION, buckets=buckets, totals=totals, kpis=kpis,
                             complete=complete, refreshing=any(pd.stale for _, pd in plant_days))


def _totals_and_kpis(names: list[str], per_day: list, plant_days: Sequence[tuple[date, PlantDay]], now: datetime,
                     measured_consumption: bool) -> tuple[PlantEnergyTotals, PlantEnergyKpis]:
    all_slots = {c: [e for _, _, cats, _ in per_day for e in cats[c]] for c in names}
    durations = [(sl.end - sl.start).total_seconds() for _, slots, _, _ in per_day for sl in slots]
    energy: dict[str, float | None] = {}
    cov: dict[str, float | None] = {}
    unresolved: dict[str, float] = {}
    for c, slots in all_slots.items():
        known = [e for e in slots if e.wh is not None]
        energy[c] = round(sum(e.wh or 0.0 for e in known), 1) if known else None
        cov[c] = round(_cov(slots, durations), 4) if known else (0.0 if any(_due(e) for e in slots) else None)
        u = round(sum(e.unresolved_wh for e in slots if _due(e)), 1)
        if u:
            unresolved[c] = u
    sources: dict[str, PlantEnergySource] = {}
    for c in names:
        seen = {(pd.sources[c].method, pd.sources[c].metric_id, pd.sources[c].resolution_s)
                for _, pd in plant_days if c in pd.sources}
        if seen:
            method, metric, res = sorted(seen, key=lambda x: (x[0], x[1] or "", x[2] or 0))[-1]
            sources[c] = PlantEnergySource(method=method, metric_id=metric, resolution_s=res or None, mixed=len(seen) > 1)

    parts: dict[str, list[tuple[float, float, float, float]]] = {}
    for _, pd in plant_days:
        for k, rows in pd.kpi_parts.items():
            parts.setdefault(k, []).extend(rows)

    def ratio(name: str) -> tuple[float | None, float | None]:
        rows = parts.get(name) or []
        num, den = sum(r[0] for r in rows), sum(r[1] for r in rows)
        joint, app = sum(r[2] for r in rows), sum(r[3] for r in rows)
        if den <= 0 or joint <= 0:
            return None, (round(joint / app, 4) if app > 0 else None)
        return round(100 * (1 - num / den), 2), round(joint / app, 4)

    def e(c: str) -> float | None:
        return energy.get(c)

    aut, aut_cov = ratio("autarky")
    sc, sc_cov = ratio("self_consumption")
    gen, cons = e("generation"), e("consumption")
    chg, dis = e("battery.charge"), e("battery.discharge")
    imp, exp = e("grid.import"), e("grid.export")
    has_battery = "battery.net" in names
    chg0, dis0 = (chg, dis) if has_battery else (0.0, 0.0)
    flows = [gen, cons, imp, exp, chg0, dis0]
    residual = threshold = None
    if all(v is not None for v in flows):
        sources_wh = gen + imp + dis0                      # type: ignore[operator]
        sinks_wh = cons + chg0 + exp                       # type: ignore[operator]
        threshold = max(50.0, 0.01 * max(sources_wh, sinks_wh))
        if measured_consumption:
            residual = round(sources_wh - sinks_wh, 1)

    def covs(*cats: str) -> list[float | None]:
        return [cov.get(c) if c in cov else None for c in cats]

    gen_ring = cons_ring = None
    if all(v is not None for v in flows) and threshold is not None:
        gen_ring = ring(gen, chg0, exp, covs("generation", "battery.charge", "grid.export"), residual or 0.0, threshold)  # type: ignore[arg-type]
        cons_ring = ring(cons, dis0, imp, covs("consumption", "battery.discharge", "grid.import"), residual or 0.0, threshold)  # type: ignore[arg-type]
    kpis = PlantEnergyKpis(
        autarky_pct=aut, self_consumption_pct=sc, autarky_coverage=aut_cov, self_consumption_coverage=sc_cov,
        direct_generation_side_wh=round(max(0.0, gen - chg0 - exp), 1) if None not in (gen, chg0, exp) else None,   # type: ignore[operator]
        direct_consumption_side_wh=round(max(0.0, cons - dis0 - imp), 1) if None not in (cons, dis0, imp) else None,  # type: ignore[operator]
        balance_residual_wh=residual, residual_warning=residual is not None and threshold is not None and abs(residual) > threshold,
        generation_ring=gen_ring, consumption_ring=cons_ring)
    return PlantEnergyTotals(energy_wh=energy, coverage=cov, unresolved_wh_max=unresolved, sources=sources), kpis


def period_days(period: str, d: date, tz: ZoneInfo) -> list[date]:
    return days(*period_bounds(period, d, tz), tz)
