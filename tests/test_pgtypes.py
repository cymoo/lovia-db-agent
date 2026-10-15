"""PostgreSQL dates and times as text, for every value the server can hold."""

from __future__ import annotations

import datetime as dt

import pytest

from db_agent.convert import parse_tz
from db_agent.pgtypes import _date, _encode_date, _encode_timestamp, _timestamp

_US = 86_400_000_000


@pytest.mark.parametrize(
    ("days", "text"),
    [
        (0, "2000-01-01"),
        (9770, "2026-10-01"),
        (-730119, "0001-01-01"),
        (-730120, "0001-12-31 BC"),  # the day before 1 AD; there is no year 0
        (-746117, "0044-03-15 BC"),
        (2921940, "10000-01-01"),
        (2**31 - 1, "infinity"),
        (-(2**31), "-infinity"),
    ],
)
def test_dates(days: int, text: str) -> None:
    assert _date((days,)) == text


def test_dates_round_trip_where_python_has_them() -> None:
    for d in (dt.date(1, 1, 1), dt.date(2000, 1, 1), dt.date(2026, 2, 28), dt.date(9999, 12, 31)):
        assert _date(_encode_date(d)) == d.isoformat()


def test_timestamps() -> None:
    assert _timestamp(_US + 1_500_000, None) == "2000-01-02T00:00:01.500000"
    assert _timestamp(2921940 * _US + 3_600_000_000, None) == "10000-01-01T01:00:00"
    assert _timestamp(-746117 * _US, None) == "0044-03-15T00:00:00 BC"
    assert _timestamp(2**63 - 1, None) == "infinity"
    naive = dt.datetime(2026, 10, 1, 8, 30)
    assert _timestamp(_encode_timestamp(naive)[0], None) == naive.isoformat()


def test_timestamptz_shows_in_the_session_time_zone() -> None:
    shanghai = parse_tz("Asia/Shanghai")
    utc_midnight = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    us = _encode_timestamp(utc_midnight)[0]
    assert _timestamp(us, shanghai) == "2026-01-01T08:00:00+08:00"
    assert _timestamp(us, dt.timezone.utc) == "2026-01-01T00:00:00+00:00"
    # Past Python's range: UTC, which needs no zone rules.
    assert _timestamp(2921940 * _US, shanghai) == "10000-01-01T00:00:00+00:00"
