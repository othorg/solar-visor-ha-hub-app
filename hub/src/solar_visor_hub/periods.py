"""Calendar of the plant (plan §3.4, ha-addon P3): local periods → half-open UTC intervals, 15-min buckets.

All persistence and comparison in UTC; local time only for period boundaries and labels. The time zone defaults to
Europe/Berlin (the existing plant and its history); a plant set up in the add-on passes the zone Home Assistant reports.
Every zone in use today has offsets in whole quarter hours, so UTC quarter hours coincide with local quarter hours –
including the 23-h (92 buckets) and 25-h (100 buckets) days and zones such as +05:45 or +10:30.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Berlin")


def zone(name: str | None) -> ZoneInfo:
    """Time zone by IANA name; the default zone when the name is missing or unknown (HA not read yet)."""
    if not name:
        return TZ
    try:
        return ZoneInfo(name)
    except (ValueError, KeyError, OSError):         # ZoneInfoNotFoundError is a KeyError; malformed names ValueError
        return TZ
BUCKET = timedelta(minutes=15)
PERIODS = ("day", "week", "month", "year")


@dataclass(frozen=True)
class Slot:
    start: datetime          # UTC, inclusive
    end: datetime            # UTC, exclusive
    local_label: str         # "HH:MM" local wall clock at start (ambiguous hour distinguished by offset)
    utc_offset_minutes: int


def local_midnight_utc(d: date, tz: ZoneInfo = TZ) -> datetime:
    return datetime.combine(d, time(0), tzinfo=tz).astimezone(UTC)


def period_bounds(period: str, d: date, tz: ZoneInfo = TZ) -> tuple[datetime, datetime]:
    """Half-open [start, end) in UTC for the local period containing ``d``."""
    if period == "day":
        first, nxt = d, d + timedelta(days=1)
    elif period == "week":                       # ISO week, Monday
        first = d - timedelta(days=d.weekday())
        nxt = first + timedelta(days=7)
    elif period == "month":
        first = d.replace(day=1)
        nxt = (first.replace(year=first.year + 1, month=1) if first.month == 12
               else first.replace(month=first.month + 1))
    elif period == "year":
        first, nxt = d.replace(month=1, day=1), d.replace(year=d.year + 1, month=1, day=1)
    else:
        raise ValueError(f"unknown period {period!r}")
    return local_midnight_utc(first, tz), local_midnight_utc(nxt, tz)


def days(start: datetime, end: datetime, tz: ZoneInfo = TZ) -> list[date]:
    """Local dates whose midnight lies in [start, end)."""
    out, d = [], start.astimezone(tz).date()
    while local_midnight_utc(d, tz) < end:
        if local_midnight_utc(d, tz) >= start:
            out.append(d)
        d += timedelta(days=1)
    return out


def quarter_hours(start: datetime, end: datetime, tz: ZoneInfo = TZ) -> list[Slot]:
    if start.utcoffset() is None or end.utcoffset() is None:
        raise ValueError("timezone-aware datetimes required")
    out, t = [], start.astimezone(UTC)
    while t < end:
        local = t.astimezone(tz)
        offset = int(local.utcoffset().total_seconds() // 60)  # type: ignore[union-attr]
        out.append(Slot(t, t + BUCKET, local.strftime("%H:%M"), offset))
        t += BUCKET
    return out
