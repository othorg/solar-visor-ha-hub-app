"""Types of a plant profile – free of any installation's data (plan hub-publish P4).

The data of one specific installation (data epochs, events, site, defaults) lives in a profile module that uses these
types: ``own_profile`` for the author's plant (private, not published) or the neutral ``profile_placeholder``.
``profile`` picks one; ``epochs``/``plant_events`` re-export its names for their callers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from .energy import E3DC_HOLD, HoldRule
from .models import BalanceProfile

T_MAX = datetime(2100, 1, 1, tzinfo=UTC)
# neutral lower bound of the type; a profile passes its own data start explicitly
EARLIEST = datetime(2000, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class SeriesSpec:
    """One measurement feeding one or two categories in [valid_from, valid_to)."""
    measurement: str
    kind: str                         # "power" | "counter" | "status" | "soc" | "absent"
    categories: tuple[str, ...]       # power: (positive,) or (positive, negative) for net series
    valid_from: datetime = EARLIEST
    valid_to: datetime = T_MAX
    hold: HoldRule = E3DC_HOLD
    p_max_w: float = 25_000.0
    clip_negative: bool = False       # legacy 'additional' is signed (KOSTAL standby) – only ≥ 0 counts
    solar: bool = False               # PV power: zero holds through astronomical night (sun.py)
    # same integration (hacs-e3dc) as the house consumption: while that one is written, so is this one – a zero after
    # the night then holds until the next reading (dark winter days), not just the zero hold (gap analysis 2026-10-08)
    e3dc: bool = False
    default_unit: str | None = None   # unit assumed when a point carries none (IOmeter counters: Wh)
    base_from: datetime | None = None # counters: earliest trusted reading usable as base (default: valid_from)
    idle_hold: timedelta = timedelta(hours=24)   # counters: no new reading = no energy for at most this long
    confidence: str = "observed"
    profiles: tuple[BalanceProfile, ...] = field(default=tuple(BalanceProfile))


# a larger PV array changes the generation and everything that depends on it
GENERATION_DEPENDENT = frozenset({"generation", "external", "grid_export", "self_consumption", "autarky"})
# another grid meter changes every grid figure and both ratios (self-consumption uses the export)
GRID_DEPENDENT = frozenset({"grid_import", "grid_export", "autarky", "self_consumption"})


@dataclass(frozen=True)
class PlantEvent:
    event_id: str
    at: datetime                                   # UTC
    kind: str                                      # plant | meter | data
    metrics: frozenset[str]
    profiles: tuple[BalanceProfile, ...] = tuple(BalanceProfile)
    verified: bool = True
