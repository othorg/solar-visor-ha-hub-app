"""Data epochs (plan §3.4; measured from the first point of each measurement).

Each energy category is fed by exactly one source per time range. Boundaries inside a day are handled
by computing the parts separately. ``CALCULATION_VERSION`` invalidates cached buckets when rules change.
Series of a plant's oldest epochs can be marked ``reconstructed`` (semantics not fully provable). The epochs and
series of a concrete plant live in its profile (``profile``).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .models import BalanceProfile
from .profile import (  # noqa: F401 – re-exported: the plant's epochs live in its profile (plan hub-publish P4)
    CONSUMPTION_COMPLETE_FROM,
    DATA_SINCE,
    DC3_FROM,
    DETAIL_SERIES,
    EXTRA_SERIES,
    IOMETER_BASE_FROM,
    IOMETER_FROM,
    LEGACY_END,
    OPENWB_COUNTER_EPOCHS,
    P_MAX,
    PIPELINE_EVIDENCE,
    SERIES,
    SHELLY_WOMO_FROM,
    SOC_STEP_WH,
    SPLIT_SENSORS,
    T_MIN,
    WOMO_HOLD,
    WOMO_SERIES,
)
from .profile_types import T_MAX, SeriesSpec  # noqa: F401 – re-exported

CALCULATION_VERSION = 13  # 9: SOC-step bound, 10: P4 review (idle vetoes, per-source counter hold, absent sources),
                          # 11: coverage in seconds (P5 review), 12: idle across SOC drift, PV zero after the night,
                          # nothing applicable before the data start (gap analysis 2026-10-08),
                          # 13: E3/DC string energy (s10_string1/2) in the energy engine

STATUS_LOOKBACK = timedelta(days=30)
LOOKBACK = timedelta(hours=48)


def long_lookback(spec: SeriesSpec) -> bool:
    """Carry-in up to STATUS_LOOKBACK: status/SOC, and E3/DC PV whose zero from days ago still holds (a snow-covered
    array reports nothing for days)."""
    return spec.kind in ("status", "soc") or (spec.solar and spec.e3dc)


def series_for(start: datetime, end: datetime, profile: BalanceProfile) -> list[SeriesSpec]:
    """Specs overlapping [start, end) for the profile, clipped to the window."""
    out = []
    for s in SERIES:
        if profile not in s.profiles or s.valid_to <= start or s.valid_from >= end:
            continue
        out.append(SeriesSpec(**{**s.__dict__, "valid_from": max(s.valid_from, start), "valid_to": min(s.valid_to, end)}))
    return out


def epoch_start(measurement: str, t: datetime) -> datetime:
    """Earliest time a trusted base reading may come from for ``measurement`` at ``t``: the start of its source
    epoch, or an explicit ``base_from`` (counter readings valid before the usage epoch). Lookbacks never cross it."""
    specs = [s for s in SERIES if s.measurement == measurement and s.valid_from <= t]
    if not specs:
        return T_MIN
    current = max(specs, key=lambda s: s.valid_from)
    return current.base_from or current.valid_from


def measurements() -> list[str]:
    return sorted({s.measurement for s in SERIES if s.measurement})
