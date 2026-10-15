"""Database values → JSON the browser reads without loss."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from decimal import Decimal

import pytest
from asyncpg import Range

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
        # An average's tail is noise: 15 significant digits, as a number.
        (Decimal("1234567890.1234567"), 1234567890.12346),
        (Decimal("3.333333333333333333333333"), 3.33333333333333),
        (Decimal("12345678901234567.5"), "12345678901234567.5"),  # the integer part wouldn't fit
        (Decimal("12345678901234567"), "12345678901234567"),
        (Decimal("1E+3"), 1000),
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
        (Range("2026-01-01", "2026-02-01"), "[2026-01-01,2026-02-01)"),
        (Range(1, None), "[1,)"),
        (Range(empty=True), "empty"),
        # Bounds as written: rounded, these two would print alike.
        (
            Range(Decimal("1.23456789012345678"), Decimal("1.23456789012345679")),
            "[1.23456789012345678,1.23456789012345679)",
        ),
    ],
)
def test_to_json(value: object, expected: object) -> None:
    out = to_json(value)
    assert out == expected
    json.dumps(out, allow_nan=False)


def test_time_zones_by_name_or_offset() -> None:
    assert str(parse_tz("Asia/Shanghai")) == "Asia/Shanghai"
    assert parse_tz("-05:30") == dt.timezone(-dt.timedelta(hours=5, minutes=30))
    assert parse_tz("Not/AZone") is None and parse_tz(None) is None


def test_long_text_is_clipped() -> None:
    out = to_json("x" * 2000)
    assert len(out) == MAX_CELL_CHARS and out.endswith("…")
