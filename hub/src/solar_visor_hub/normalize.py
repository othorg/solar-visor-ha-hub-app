"""Normalisation of Home Assistant states into canonical values (plan §3.3 step 4).

- ``unknown`` / ``unavailable`` / empty → ``None`` (never 0)
- unit conversion per point (kW→W, kWh→Wh, mA→A …) based on the state's own unit attribute
- sign applied exactly once here
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .config import MetricMapping
from .models import Unit

UNAVAILABLE = frozenset({"unknown", "unavailable", "", "none", "null"})

# (source unit, canonical unit) → factor
_FACTORS: dict[tuple[str, Unit], float] = {
    ("W", Unit.W): 1.0,
    ("kW", Unit.W): 1_000.0,
    ("MW", Unit.W): 1_000_000.0,
    ("Wh", Unit.WH): 1.0,
    ("kWh", Unit.WH): 1_000.0,
    ("MWh", Unit.WH): 1_000_000.0,
    ("V", Unit.V): 1.0,
    ("A", Unit.A): 1.0,
    ("mA", Unit.A): 0.001,
    ("%", Unit.PERCENT): 1.0,
}


@dataclass(frozen=True)
class Normalized:
    value: float | bool | str | None
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def known(self) -> bool:
        return self.value is not None


def normalize(mapping: MetricMapping, raw_state: str | None, unit_attr: str | None) -> Normalized:
    if raw_state is None or str(raw_state).strip().lower() in UNAVAILABLE:
        return Normalized(None, ("unavailable",))
    raw = str(raw_state).strip()

    if mapping.unit is Unit.BOOL:
        low = raw.lower()
        if low in ("on", "true"):
            return Normalized(True)
        if low in ("off", "false"):
            return Normalized(False)
        return Normalized(None, ("not_boolean",))

    if mapping.unit is Unit.TEXT:
        return Normalized(raw)

    try:
        number = float(raw)
    except ValueError:
        return Normalized(None, ("not_numeric",))
    if not math.isfinite(number):
        return Normalized(None, ("non_finite",))

    source_unit = (unit_attr or mapping.unit.value).strip()
    factor = _FACTORS.get((source_unit, mapping.unit))
    if factor is None:
        return Normalized(None, (f"unit_mismatch:{source_unit}->{mapping.unit.value}",))
    return Normalized(number * factor * mapping.sign)


def compatible_unit(ha_unit: str | None, canonical: Unit, domain: str) -> bool:
    """Can an entity with this unit/domain feed a metric with the canonical unit? (mapping editor)"""
    if canonical is Unit.BOOL:
        return domain == "binary_sensor"
    if canonical is Unit.TEXT:
        return domain in ("sensor", "select", "input_select") and not ha_unit
    return ((ha_unit or "").strip(), canonical) in _FACTORS

