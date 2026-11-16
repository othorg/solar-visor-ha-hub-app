"""Plant and data-source events that limit historical comparisons (comparison plan 2026-10-06, §3.6).

Versioned metadata derived from the data epochs (``epochs.py``): a comparison whose periods lie on both sides of an
event – or contain it – shows the affected measures as "limited comparability" (the change stays visible with a
permanent hint). Events the data cannot reveal (battery replacement, new large consumers) can be added here.
"""

from __future__ import annotations

from datetime import datetime

from . import profile
from .models import BalanceProfile
from .profile_types import GENERATION_DEPENDENT, GRID_DEPENDENT, PlantEvent  # noqa: F401 – re-exported

# the plant's events live in its profile (plan hub-publish P4); the public placeholder has none
EVENTS: tuple[PlantEvent, ...] = profile.EVENTS


def events_between(start: datetime, end: datetime, profile: BalanceProfile) -> list[PlantEvent]:
    """Events strictly inside (start, end): the compared sections lie on both sides of them or contain them."""
    return [e for e in EVENTS if start < e.at < end and profile in e.profiles]
