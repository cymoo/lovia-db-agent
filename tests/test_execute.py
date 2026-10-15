"""Error hints key on driver codes — servers localize their messages."""

from __future__ import annotations

import sqlalchemy.exc as sa_exc

from db_agent.dialects import MYSQL, POSTGRES
from db_agent.execute import _code, _unreachable


class _MySQLError(Exception):
    sqlstate = "42000"  # coarse: syntax errors and denied commands share it


class _PostgresError(Exception):
    sqlstate = "25006"


def test_mysql_errno_wins_over_its_coarse_sqlstate() -> None:
    exc = sa_exc.DBAPIError("DELETE …", None, _MySQLError(1142, "DELETE command denied"))
    assert _code(exc) == "1142" and "only read" in MYSQL.hint(_code(exc))


def test_postgres_sqlstate() -> None:
    exc = sa_exc.DBAPIError("DELETE …", None, _PostgresError("只读事务中不能执行 DELETE"))
    assert _code(exc) == "25006" and "read-only" in POSTGRES.hint(_code(exc))


def test_unknown_errors_get_a_generic_hint() -> None:
    exc = sa_exc.DBAPIError("…", None, Exception("boom"))
    assert _code(exc) == "" and POSTGRES.hint("") == "fix the SQL and try again"
    # Each dialect reads only its own codes.
    assert POSTGRES.hint("1142") == MYSQL.hint("25006") == "fix the SQL and try again"


class _ConnectError(Exception):
    sqlstate = "08006"


def test_connection_failures_are_not_the_sqls_fault() -> None:
    refused = sa_exc.OperationalError("…", None, _MySQLError(2003, "Can't connect to MySQL"))
    gone = sa_exc.OperationalError("…", None, _ConnectError("connection failure"))
    syntax = sa_exc.ProgrammingError("…", None, _MySQLError(1064, "You have an error"))
    assert _unreachable(refused, MYSQL) and _unreachable(gone, POSTGRES)
    assert not _unreachable(syntax, MYSQL)
    assert not _unreachable(refused, POSTGRES)  # each dialect reads only its own codes
