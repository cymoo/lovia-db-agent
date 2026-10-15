"""Database values → JSON a browser reads faithfully.

`ui_result` refuses NaN, and a JavaScript number holds 53 bits: a bigint id
would silently change in the page, so it becomes a string. Decimals become
numbers when their integer part fits a double (an average's 20th digit is
noise), strings when it doesn't. Dates become ISO 8601 (PostgreSQL's arrive
as text already, see pgtypes.py); binary becomes a size note.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import re
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from asyncpg import Range, Record

__all__ = ["MAX_CELL_CHARS", "cell_text", "clip", "parse_tz", "to_json"]

MAX_CELL_CHARS = 500
_SAFE_INT = 2**53
# Significant digits a double carries faithfully.
_DOUBLE_DIGITS = 15


def to_json(v: Any) -> Any:
    """One cell as a JSON-safe value: str, int, float, bool, None, list or dict."""
    if v is None or isinstance(v, bool):
        return v
    if isinstance(v, int):
        return v if -_SAFE_INT < v < _SAFE_INT else str(v)
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, Decimal):
        if not v.is_finite():
            return None
        if v == v.to_integral_value():
            return int(v) if abs(v) < _SAFE_INT else str(v)
        # An integer part past 15 digits would change; digits past the 15th
        # significant one are rounding noise (avg(), numeric division).
        if v.adjusted() >= _DOUBLE_DIGITS:
            return str(v)
        return float(f"{v:.{_DOUBLE_DIGITS}g}")
    if isinstance(v, str):
        return clip(v, MAX_CELL_CHARS)
    if isinstance(v, (dt.datetime, dt.date, dt.time)):
        return v.isoformat()
    if isinstance(v, dt.timedelta):
        return v.total_seconds()
    if isinstance(v, (bytes, bytearray, memoryview)):
        return f"<binary {_size(len(v))}>"
    if isinstance(v, (dict, Record)):
        return _clip_json({str(k): to_json(x) for k, x in v.items()})
    if isinstance(v, (list, tuple)):
        return _clip_json([to_json(x) for x in v])
    if isinstance(v, Range):
        return _range(v)
    return clip(str(v), MAX_CELL_CHARS)  # UUID, IP addresses, enums, …


def clip(s: str, n: int) -> str:
    """``s`` cut to at most ``n`` characters, the last an ellipsis."""
    return s if len(s) <= n else s[: n - 1] + "…"


def cell_text(v: Any, n: int) -> str:
    """A JSON-safe cell (see :func:`to_json`) as one line of text for the
    model, at most ``n`` characters: tabs and line breaks become spaces."""
    if v is None:
        return ""
    s = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)
    return clip(s.replace("\t", " ").replace("\r", " ").replace("\n", " "), n)


def parse_tz(name: str | None) -> dt.tzinfo | None:
    """``Asia/Shanghai`` or ``+08:00`` as a tzinfo; None if absent or unknown."""
    if not name:
        return None
    m = re.fullmatch(r"([+-])(\d{1,2}):?(\d{2})", name)
    if m:
        delta = dt.timedelta(hours=int(m.group(2)), minutes=int(m.group(3)))
        return dt.timezone(delta if m.group(1) == "+" else -delta)
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def _range(r: Range) -> str:
    """PostgreSQL's own spelling: ``[2026-01-01,2026-02-01)``, ``empty``."""
    if r.isempty:
        return "empty"

    def bound(v: Any) -> str:
        # As written, not through to_json: the range is text already, and a
        # Decimal bound rounded to a double could make it read empty.
        return "" if v is None else str(v)

    return (
        ("[" if r.lower_inc else "(")
        + f"{bound(r.lower)},{bound(r.upper)}"
        + ("]" if r.upper_inc else ")")
    )


def _clip_json(v: Any) -> Any:
    text = json.dumps(v, ensure_ascii=False)
    return v if len(text) <= MAX_CELL_CHARS else clip(text, MAX_CELL_CHARS)


def _size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    return f"{n / 1024:.1f} KB" if n < 1024**2 else f"{n / 1024**2:.1f} MB"
