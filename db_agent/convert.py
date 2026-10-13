"""Database values → JSON a browser reads faithfully.

`ui_result` refuses NaN, and a JavaScript number holds 53 bits: a bigint id
or a long decimal would silently change in the page. Such values become
strings; dates become ISO 8601; binary becomes a size note.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import re
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__all__ = ["MAX_CELL_CHARS", "parse_tz", "to_json"]

MAX_CELL_CHARS = 500
_SAFE_INT = 2**53


def to_json(v: Any, tz: dt.tzinfo | None = None) -> Any:
    """One cell as a JSON-safe value: str, int, float, bool, None, list or dict.

    An aware datetime is shown in ``tz`` when given — asyncpg decodes
    ``timestamptz`` as UTC whatever the session's time zone.
    """
    if tz is not None and isinstance(v, dt.datetime) and v.tzinfo is not None:
        v = v.astimezone(tz)
    if v is None or isinstance(v, bool):
        return v
    if isinstance(v, int):
        return v if -_SAFE_INT < v < _SAFE_INT else str(v)
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, Decimal):
        if not v.is_finite():
            return None
        # 15 significant digits round-trip through a double; more would not.
        if len(v.as_tuple().digits) > 15:
            return str(v)
        return int(v) if v == v.to_integral_value() and abs(v) < _SAFE_INT else float(v)
    if isinstance(v, str):
        return _clip(v)
    if isinstance(v, (dt.datetime, dt.date, dt.time)):
        return v.isoformat()
    if isinstance(v, dt.timedelta):
        return v.total_seconds()
    if isinstance(v, (bytes, bytearray, memoryview)):
        return f"<binary {_size(len(v))}>"
    if isinstance(v, dict):
        return _clip_json({str(k): to_json(x, tz) for k, x in v.items()})
    if isinstance(v, (list, tuple)):
        return _clip_json([to_json(x, tz) for x in v])
    return _clip(str(v))  # UUID, IP addresses, enums, ranges, …


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


def _clip(s: str) -> str:
    return s if len(s) <= MAX_CELL_CHARS else s[: MAX_CELL_CHARS - 1] + "…"


def _clip_json(v: Any) -> Any:
    text = json.dumps(v, ensure_ascii=False)
    return v if len(text) <= MAX_CELL_CHARS else _clip(text)


def _size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    return f"{n / 1024:.1f} KB" if n < 1024**2 else f"{n / 1024**2:.1f} MB"
