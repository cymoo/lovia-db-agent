"""Error hints key on driver codes — servers localize their messages."""

from __future__ import annotations

import pytest
import sqlalchemy.exc as sa_exc

from db_agent.execute import _code, _hint, _pg_time_zone


class _MySQLError(Exception):
    sqlstate = "42000"  # coarse: syntax errors and denied commands share it


class _PostgresError(Exception):
    sqlstate = "25006"


def test_mysql_errno_wins_over_its_coarse_sqlstate() -> None:
    exc = sa_exc.DBAPIError("DELETE …", None, _MySQLError(1142, "DELETE command denied"))
    assert _code(exc) == "1142" and "only read" in _hint(_code(exc))


def test_postgres_sqlstate() -> None:
    exc = sa_exc.DBAPIError("DELETE …", None, _PostgresError("只读事务中不能执行 DELETE"))
    assert _code(exc) == "25006" and "read-only" in _hint(_code(exc))


def test_unknown_errors_get_a_generic_hint() -> None:
    exc = sa_exc.DBAPIError("…", None, Exception("boom"))
    assert _code(exc) == "" and _hint("") == "fix the SQL and try again"


@pytest.mark.parametrize(
    ("iso", "postgres"),
    [
        ("+08:00", "UTC-08:00"),  # PostgreSQL reads offsets POSIX-style: east is negative
        ("-05:30", "UTC+05:30"),
        ("+0545", "UTC-05:45"),
        ("Asia/Shanghai", "Asia/Shanghai"),
    ],
)
def test_postgres_time_zone_offsets(iso: str, postgres: str) -> None:
    assert _pg_time_zone(iso) == postgres
