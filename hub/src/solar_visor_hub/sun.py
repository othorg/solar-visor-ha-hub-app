"""Sun elevation (NOAA approximation, ±0.1° – plenty for day/night) for the PV night rule (P3).

PV power is physically zero while the sun is below the horizon. HA writes a single 0 at dusk and
nothing until dawn (≈ 11–16 h), longer than any hold time; astronomy is independent evidence that
the zero holds – no gap and no unresolved energy at night.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

from . import profile

# site of the existing-plant calculation (profile); generic plants pass Home Assistant's location
DEFAULT_LAT, DEFAULT_LON = profile.SITE
NIGHT_ELEVATION_DEG = 2.0           # PV zero rule: below +2° the array yields at most a few % of peak
# for a few minutes; a change from 0 would be written by HA as a new point anyway (change-based history)


def elevation_deg(t: datetime, lat: float = DEFAULT_LAT, lon: float = DEFAULT_LON) -> float:
    t = t.astimezone(UTC)
    jd = t.timestamp() / 86400.0 + 2440587.5
    n = jd - 2451545.0
    g = math.radians((357.529 + 0.98560028 * n) % 360)
    q = (280.459 + 0.98564736 * n) % 360
    lam = math.radians((q + 1.915 * math.sin(g) + 0.020 * math.sin(2 * g)) % 360)
    eps = math.radians(23.439 - 0.00000036 * n)
    ra = math.atan2(math.cos(eps) * math.sin(lam), math.cos(lam))
    dec = math.asin(math.sin(eps) * math.sin(lam))
    gmst = (18.697374558 + 24.06570982441908 * n) % 24
    ha = math.radians((gmst * 15 + lon) % 360) - ra
    phi = math.radians(lat)
    return math.degrees(math.asin(math.sin(phi) * math.sin(dec) + math.cos(phi) * math.cos(dec) * math.cos(ha)))


def night_intervals(start: datetime, end: datetime, step: timedelta = timedelta(minutes=5),
                    lat: float = DEFAULT_LAT, lon: float = DEFAULT_LON) -> list[tuple[datetime, datetime]]:
    """Intervals in [start, end) with the sun below NIGHT_ELEVATION_DEG (5-min resolution, conservative:
    an interval only counts if both its ends are below)."""
    out: list[tuple[datetime, datetime]] = []
    t = start
    below_t = elevation_deg(t, lat, lon) < NIGHT_ELEVATION_DEG      # each grid point evaluated once (same result)
    while t < end:
        nxt = min(end, t + step)
        below_nxt = elevation_deg(nxt, lat, lon) < NIGHT_ELEVATION_DEG
        if below_t and below_nxt:
            if out and out[-1][1] == t:
                out[-1] = (out[-1][0], nxt)
            else:
                out.append((t, nxt))
        t, below_t = nxt, below_nxt
    return out
