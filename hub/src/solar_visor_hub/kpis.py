"""Period totals and KPIs (plan §3.5) from 15-min slot energies and KPI parts.

- Grid energy comes from the epoch-selected categories ``grid_import``/``grid_export`` (billing meter from its
  epoch on, E3/DC before) – never switched by availability.
- KPIs integrate numerator and denominator on the common valid intervals of their inputs (``kpi_parts``),
  coverage = joint seconds / applicable seconds. Denominator 0 → not determinable (None).
- Residual R = sources − sinks from measured sums, never distributed or corrected.
- Rings allocate the period totals; their state is decided here (client renders only): a ring is complete only
  if the guaranteed joint coverage of *all* its inputs is full (Codex P5 finding 4).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from .daycalc import SlotEnergy, coverage
from .epochs import IOMETER_FROM
from .models import EnergyKpis, EnergyRing, EnergyTotals, RingState

# complete from 99.5 % coverage (user 2026-10-08: 0.1 % made almost every period „incomplete“); the client mirrors
# it as CoverageRule.complete
FULL = 0.995
BASE = ("pv_internal", "external", "consumption", "wallbox", "battery_charge", "battery_discharge", "grid_import",
        "grid_export")


def joint(*covs: float | None) -> float:
    """Guaranteed joint coverage of inputs with individual coverages (1 − Σ uncovered); an input without a
    source (None) has nothing to cover. min() would overstate it: two disjoint halves overlap nowhere."""
    return max(0.0, 1.0 - sum(1.0 - c for c in covs if c is not None))


def category_coverage(parts: Sequence[SlotEnergy]) -> float | None:
    return coverage(parts) if any(p.wh is not None for p in parts) else None


def _sum(cats: dict[str, list[SlotEnergy]], cat: str) -> float:
    return sum(e.wh or 0.0 for e in cats.get(cat, []))


def grid_source(slot_starts: Sequence[datetime]) -> str:
    kinds = {"iometer" if t >= IOMETER_FROM else "e3dc" for t in slot_starts}
    return kinds.pop() if len(kinds) == 1 else ("mixed" if kinds else "none")


def totals(cats: dict[str, list[SlotEnergy]], slot_starts: Sequence[datetime],
           derived: dict[str, list[SlotEnergy]] | None = None) -> EnergyTotals:
    """``derived``: generation/home per part (guaranteed joint coverage per part) for their coverage."""
    s = {k: _sum(cats, k) for k in BASE}
    src = {**cats, **(derived or {})}
    cov = {k: category_coverage(src.get(k, [])) for k in (*BASE, "generation", "home")}
    if derived is None:
        cov["generation"] = joint(cov["pv_internal"], cov["external"]) if cov["pv_internal"] is not None else None
        cov["home"] = joint(cov["consumption"], cov["wallbox"]) if cov["consumption"] is not None else None
    unresolved = {k: round(sum(e.unresolved_wh for e in src.get(k, []) if e.wh is not None), 1)
                  for k in (*BASE, "generation", "home")}
    return EnergyTotals(
        pv_internal_wh=round(s["pv_internal"], 1), external_wh=round(s["external"], 1),
        generation_wh=round(s["pv_internal"] + s["external"], 1), consumption_wh=round(s["consumption"], 1),
        wallbox_wh=round(s["wallbox"], 1), home_wh=round(s["consumption"] - s["wallbox"], 1),
        battery_charge_wh=round(s["battery_charge"], 1), battery_discharge_wh=round(s["battery_discharge"], 1),
        grid_import_wh=round(s["grid_import"], 1), grid_export_wh=round(s["grid_export"], 1),
        grid_source=grid_source(slot_starts),
        coverage={k: None if v is None else round(v, 4) for k, v in cov.items()},
        unresolved_wh_max={k: v for k, v in unresolved.items() if v})


def ring(total_wh: float, battery_wh: float, grid_wh: float, covs: Sequence[float | None], residual_wh: float,
         threshold_wh: float) -> EnergyRing:
    """covs[0] is the ring's total (generation or consumption); without a source there the ring has no data."""
    raw = total_wh - battery_wh - grid_wh
    cov = joint(*covs)
    if covs[0] is None or (cov == 0.0 and total_wh == 0.0):
        state = RingState.NONE
    elif cov < FULL:
        state = RingState.INCOMPLETE
    elif raw < -threshold_wh:
        state = RingState.UNRESOLVABLE
    elif abs(residual_wh) > threshold_wh:
        state = RingState.UNCERTAIN
    else:
        state = RingState.OK
    return EnergyRing(state=state, total_wh=round(total_wh, 1), direct_wh=round(max(0.0, raw), 1),
                      direct_raw_wh=round(raw, 1), battery_wh=round(battery_wh, 1), grid_wh=round(grid_wh, 1),
                      coverage=round(cov, 4), threshold_wh=round(threshold_wh, 1))


def kpis(cats: dict[str, list[SlotEnergy]], parts: dict[str, list[tuple[float, float, float, float]]],
         derived: dict[str, list[SlotEnergy]] | None = None) -> EnergyKpis:
    def ratio(name: str) -> tuple[float | None, float | None]:
        rows = parts.get(name) or []
        num, den = sum(r[0] for r in rows), sum(r[1] for r in rows)
        joint, app = sum(r[2] for r in rows), sum(r[3] for r in rows)
        pct = round(100 * (1 - num / den), 2) if den > 0 else None
        return pct, (round(joint / app, 4) if app > 0 else None)

    pv, ext, cons = _sum(cats, "pv_internal"), _sum(cats, "external"), _sum(cats, "consumption")
    chg, dis = _sum(cats, "battery_charge"), _sum(cats, "battery_discharge")
    imp, exp = _sum(cats, "grid_import"), _sum(cats, "grid_export")
    imp_e, exp_e = _sum(cats, "grid_import_e3dc"), _sum(cats, "grid_export_e3dc")
    if not any(cats.values()) and not any(parts.values()):
        return EnergyKpis(autarky_pct=None, self_consumption_pct=None, direct_generation_side_wh=None,
                          direct_consumption_side_wh=None, balance_residual_wh=None, residual_warning=False)
    gen = pv + ext
    residual = (pv + ext + imp + dis) - (cons + chg + exp)
    t_e = max(50.0, 0.01 * max(pv + ext + imp + dis, cons + chg + exp))
    aut, aut_cov = ratio("autarky")
    sc, sc_cov = ratio("self_consumption")
    cov = {k: category_coverage(cats.get(k, [])) for k in BASE}
    if derived and "generation" in derived:            # same per-part joint coverage as the totals
        gen_cov = category_coverage(derived["generation"])
    else:
        gen_cov = joint(cov["pv_internal"], cov["external"]) if cov["pv_internal"] is not None else None
    return EnergyKpis(
        autarky_pct=aut, self_consumption_pct=sc,
        direct_generation_side_wh=round(max(0.0, gen - chg - exp), 1),
        direct_consumption_side_wh=round(max(0.0, cons - dis - imp), 1),
        balance_residual_wh=round(residual, 1), residual_warning=abs(residual) > t_e,
        autarky_coverage=aut_cov, self_consumption_coverage=sc_cov,
        e3dc_balance_residual_wh=round((pv + ext + imp_e + dis) - (cons + chg + exp_e), 1),
        generation_ring=ring(gen, chg, exp, [gen_cov, cov["battery_charge"], cov["grid_export"]], residual, t_e),
        consumption_ring=ring(cons, dis, imp, [cov["consumption"], cov["battery_discharge"], cov["grid_import"]],
                              residual, t_e),
    )
