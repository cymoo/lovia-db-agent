"""PostgreSQL dates and times, decoded without overflow.

asyncpg decodes ``date`` and ``timestamp`` into Python objects, which stop at
year 9999 and know no BC — one such value anywhere in a result fails the whole
query ("date value out of range"). These codecs take the raw binary values
(asyncpg's ``tuple`` format: days or microseconds since 2000-01-01) and
return ISO 8601 text for every value PostgreSQL can hold. They stay binary on
the wire, so arrays, ranges and composite rows of these types decode too.

``timestamptz`` is shown in the session's time zone, as SQL sees it: asyncpg
alone would return UTC, whatever the session says.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__all__ = ["register"]

_EPOCH = dt.datetime(2000, 1, 1)
_EPOCH_UTC = _EPOCH.replace(tzinfo=dt.timezone.utc)
_EPOCH_ORDINAL = _EPOCH.toordinal()
_DAY_US = 86_400_000_000
_CYCLE_DAYS = 146_097  # 400 Gregorian years
# PostgreSQL's 'infinity' and '-infinity'.
_DATE_INF, _DATE_NEG_INF = 2**31 - 1, -(2**31)
_TS_INF, _TS_NEG_INF = 2**63 - 1, -(2**63)


async def register(conn: Any) -> None:
    """Install the codecs on a new asyncpg connection (in the session's time zone)."""
    name, offset = await conn.fetchrow(
        "SELECT current_setting('TimeZone'), EXTRACT(timezone FROM now())::int"
    )
    tz = _zone(name) or dt.timezone(dt.timedelta(seconds=offset))

    def timestamptz(value: tuple[int]) -> str:
        return _timestamp(value[0], tz)

    for type_name, decoder, encoder in (
        ("date", _date, _encode_date),
        ("timestamp", lambda v: _timestamp(v[0], None), _encode_timestamp),
        ("timestamptz", timestamptz, _encode_timestamp),
    ):
        await conn.set_type_codec(
            type_name, schema="pg_catalog", encoder=encoder, decoder=decoder, format="tuple"
        )


def _zone(name: str) -> dt.tzinfo | None:
    """A named zone, with its daylight saving rules. Not an offset like
    ``+08:00``: PostgreSQL reads those POSIX-style (east is negative), so the
    caller takes the offset the server reports instead."""
    if not name or name[0] in "+-" or name[0].isdigit():
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def _date(value: tuple[int]) -> str:
    days = value[0]
    if days == _DATE_INF:
        return "infinity"
    if days == _DATE_NEG_INF:
        return "-infinity"
    return _day(days)


def _timestamp(us: int, tz: dt.tzinfo | None) -> str:
    if us == _TS_INF:
        return "infinity"
    if us == _TS_NEG_INF:
        return "-infinity"
    try:
        if tz is None:
            return (_EPOCH + dt.timedelta(microseconds=us)).isoformat()
        return (_EPOCH_UTC + dt.timedelta(microseconds=us)).astimezone(tz).isoformat()
    except OverflowError:
        # Beyond Python's years 1-9999 — shown in UTC, where no zone rules apply.
        days, rest = divmod(us, _DAY_US)
        clock = (dt.datetime.min + dt.timedelta(microseconds=rest)).time().isoformat()
        day = _day(days)
        date, bc = (day[:-3], " BC") if day.endswith(" BC") else (day, "")
        return f"{date}T{clock}" + ("+00:00" if tz is not None else "") + bc


def _day(days: int) -> str:
    """The proleptic Gregorian date ``days`` after 2000-01-01, any year."""
    ordinal = _EPOCH_ORDINAL + days
    # Shift by whole 400-year cycles (each exactly 146,097 days) into the
    # range Python's date handles, then shift the year back.
    cycles, rest = divmod(ordinal - 1, _CYCLE_DAYS)
    d = dt.date.fromordinal(rest + 1)
    year = d.year + 400 * cycles
    if year >= 1:
        return f"{year:04d}-{d.month:02d}-{d.day:02d}"
    return f"{1 - year:04d}-{d.month:02d}-{d.day:02d} BC"  # there is no year 0


# Never used by our own queries (they bind no dates); here so the codecs are complete.
def _encode_date(value: dt.date) -> tuple[int]:
    return (value.toordinal() - _EPOCH_ORDINAL,)


def _encode_timestamp(value: dt.datetime) -> tuple[int]:
    base = _EPOCH_UTC if value.tzinfo is not None else _EPOCH
    return ((value - base) // dt.timedelta(microseconds=1),)
