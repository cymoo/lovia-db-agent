"""Database values → JSON the browser reads without loss."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from decimal import Decimal

import pytest

from db_agent.convert import MAX_CELL_CHARS, parse_tz, to_json


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        (True, True),
        (42, 42),
        (2**53, str(2**53)),  # a bigint id would change in JavaScript
        (-(2**60), str(-(2**60))),
        (1.5, 1.5),
        (float("nan"), None),
        (float("inf"), None),
        (Decimal("12.50"), 12.5),
        (Decimal("100"), 100),
        (Decimal("1234567890.1234567"), "1234567890.1234567"),  # 17 digits
        (Decimal("NaN"), None),
        (dt.date(2026, 10, 1), "2026-10-01"),
        (
            dt.datetime(2026, 10, 1, 8, 30, tzinfo=dt.timezone(dt.timedelta(hours=8))),
            "2026-10-01T08:30:00+08:00",
        ),
        (dt.timedelta(minutes=2), 120.0),
        (b"\x00" * 2048, "<binary 2.0 KB>"),
        (uuid.UUID(int=1), "00000000-0000-0000-0000-000000000001"),
        ({"a": [1, Decimal("2.5")]}, {"a": [1, 2.5]}),
    ],
)
def test_to_json(value: object, expected: object) -> None:
    out = to_json(value)
    assert out == expected
    json.dumps(out, allow_nan=False)


def test_aware_datetimes_show_in_the_connection_time_zone() -> None:
    utc = dt.datetime(2026, 1, 1, 16, 0, tzinfo=dt.timezone.utc)
    assert to_json(utc, parse_tz("Asia/Shanghai")) == "2026-01-02T00:00:00+08:00"
    assert to_json(utc, parse_tz("-05:30")) == "2026-01-01T10:30:00-05:30"
    assert to_json(dt.datetime(2026, 1, 1), parse_tz("+08:00")) == "2026-01-01T00:00:00"  # naive
    assert parse_tz("Not/AZone") is None and parse_tz(None) is None


def test_long_text_is_clipped() -> None:
    out = to_json("x" * 2000)
    assert len(out) == MAX_CELL_CHARS and out.endswith("…")
