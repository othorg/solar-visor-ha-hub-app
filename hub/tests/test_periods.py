"""Calendar Europe/Berlin (plan §3.4; P3 acceptance 2: DST days 92/100 buckets)."""

from datetime import UTC, date, datetime

import pytest

from solar_visor_hub.periods import days, period_bounds, quarter_hours


@pytest.mark.parametrize(("d", "buckets", "hours"), [
    (date(2026, 3, 29), 92, 23),    # spring forward
    (date(2025, 10, 26), 100, 25),  # fall back
    (date(2026, 10, 3), 96, 24),
])
def test_day_lengths(d, buckets, hours):
    start, end = period_bounds("day", d)
    slots = quarter_hours(start, end)
    assert len(slots) == buckets and (end - start).total_seconds() == hours * 3600
    assert slots[0].local_label == "00:00" and slots[-1].local_label == "23:45"


def test_fall_back_hour_appears_twice_with_different_offsets():
    slots = quarter_hours(*period_bounds("day", date(2025, 10, 26)))
    twos = [(s.local_label, s.utc_offset_minutes) for s in slots if s.local_label == "02:00"]
    assert twos == [("02:00", 120), ("02:00", 60)]


def test_spring_forward_skips_two_oclock():
    labels = [s.local_label for s in quarter_hours(*period_bounds("day", date(2026, 3, 29)))]
    assert "02:00" not in labels and "01:45" in labels and "03:00" in labels


def test_period_boundaries_are_local_midnights_in_utc():
    assert period_bounds("day", date(2026, 10, 3)) == (datetime(2026, 10, 2, 22, tzinfo=UTC),
                                                        datetime(2026, 10, 3, 22, tzinfo=UTC))
    assert period_bounds("week", date(2026, 10, 3))[0] == datetime(2026, 9, 27, 22, tzinfo=UTC)   # Monday 28.09.
    assert period_bounds("month", date(2026, 3, 15)) == (datetime(2026, 2, 28, 23, tzinfo=UTC),
                                                          datetime(2026, 3, 31, 22, tzinfo=UTC))  # DST inside
    start, end = period_bounds("year", date(2026, 6, 1))
    assert start == datetime(2025, 12, 31, 23, tzinfo=UTC) and end == datetime(2026, 12, 31, 23, tzinfo=UTC)
    assert len(days(start, end)) == 365
    assert period_bounds("month", date(2026, 12, 5))[1] == datetime(2026, 12, 31, 23, tzinfo=UTC)


def test_naive_datetimes_rejected():
    with pytest.raises(ValueError):
        quarter_hours(datetime(2026, 1, 1), datetime(2026, 1, 2))


# --- time zone of the plant (ha-addon P3 step 2) ---------------------------------------------------------------

from zoneinfo import ZoneInfo as _Zone

from solar_visor_hub.periods import TZ as _BERLIN
from solar_visor_hub.periods import days as _days
from solar_visor_hub.periods import local_midnight_utc as _midnight
from solar_visor_hub.periods import period_bounds as _bounds
from solar_visor_hub.periods import quarter_hours as _quarters
from solar_visor_hub.periods import zone as _zone


def test_default_zone_is_unchanged():
    from datetime import date
    for period in ("day", "week", "month", "year"):
        assert _bounds(period, date(2026, 3, 29)) == _bounds(period, date(2026, 3, 29), _BERLIN)
    assert _zone(None) is _BERLIN and _zone("") is _BERLIN and _zone("Not/AZone") is _BERLIN and _zone("../etc") is _BERLIN
    assert _zone("Europe/Vienna") == _Zone("Europe/Vienna")


def test_other_zones_give_their_own_days_and_quarter_hours():
    from datetime import date, timedelta
    ny = _Zone("America/New_York")
    start, end = _bounds("day", date(2026, 3, 8), ny)                 # spring forward: 23 h
    assert (end - start) == timedelta(hours=23) and len(_quarters(start, end, ny)) == 92
    start, end = _bounds("day", date(2026, 11, 1), ny)                # fall back: 25 h
    slots = _quarters(start, end, ny)
    assert len(slots) == 100 and slots[0].local_label == "00:00" and slots[0].utc_offset_minutes == -240
    assert [s.local_label for s in slots[4:12]].count("01:00") == 2   # the repeated hour, told apart by the offset
    kathmandu = _Zone("Asia/Kathmandu")                               # +05:45
    start, end = _bounds("day", date(2026, 11, 15), kathmandu)
    slots = _quarters(start, end, kathmandu)
    assert start.minute == 15 and len(slots) == 96 and [s.local_label for s in slots[:2]] == ["00:00", "00:15"]
    adelaide = _Zone("Australia/Adelaide")                            # +10:30 / +09:30
    start, end = _bounds("month", date(2026, 4, 10), adelaide)
    assert _days(start, end, adelaide)[0] == date(2026, 4, 1) and len(_days(start, end, adelaide)) == 30
    assert _midnight(date(2026, 4, 5), adelaide).minute == 30
