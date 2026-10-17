"""Everything that differs between PostgreSQL and MySQL, in one place.

A :class:`Dialect` holds what the rest of the code needs to know about a
server: how to open a session, how to bound one statement, where the
catalog lives, what its error codes mean and what the model should know
about its SQL. The other modules ask the connection's
dialect and never branch on which one it is.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from lovia import UserError

from . import pgtypes

__all__ = ["MYSQL", "POSTGRES", "Dialect", "for_backend"]


@dataclass(frozen=True, eq=False)
class Dialect:
    """One kind of server. There are two instances: :data:`POSTGRES` and :data:`MYSQL`."""

    name: str
    """sqlglot's name for it: "postgres" / "mysql"."""
    display: str
    """What the model reads before the version: "PostgreSQL" / "MySQL"."""
    system_schemas: tuple[str, ...]
    """Never discovered."""
    schema_is_database: bool
    """MySQL's schemas are databases: with none configured, discover the URL's."""
    session_args: Callable[[int, str | None, int], dict[str, Any]]
    """``connect_args`` for a session, given the statement timeout (ms), the
    time zone (validated in config.py, or None) and the connect timeout (s)."""
    bound: Callable[[str, int], tuple[str | None, str]]
    """Give one statement its own time limit (ms): a statement to run first
    on the same connection (or None), and the statement to run."""
    bound_each: bool
    """The session carries no statement timeout: :attr:`bound` gives every
    statement the configured one."""
    version_sql: str
    """One row, one column: the version first, maybe more after a space."""
    read_only_sql: str
    """One row, one column: "on" or 1 when the session can't write."""
    tables_sql: str
    """Rows of (schema, table, "table" | "view", comment, estimated rows).
    ``:system`` is the system schemas; ``{filter}`` is where load_catalog adds
    ``AND <schema_column> IN :schemas``."""
    columns_sql: str
    """Rows of (schema, table, column, type, nullable, comment), as tables_sql."""
    schema_column: str
    """The schema column in tables_sql and columns_sql, for ``{filter}``."""
    notes: str
    """What the model should remember about the SQL, in a line."""
    hints: Mapping[str, str]
    """Hints by error code: SQLSTATE (PostgreSQL) or error number (MySQL)."""
    connection_codes: frozenset[str]
    """Error codes about the connection itself, not the statement: no SQL fixes them."""
    on_connect: Callable[[Any], Awaitable[None]] | None = None
    """Run on every new driver connection, before its first statement."""

    def hint(self, code: str) -> str:
        return self.hints.get(code, "fix the SQL and try again")


def for_backend(backend: str, connection: str) -> Dialect:
    """The dialect of a SQLAlchemy backend name; :class:`UserError` if unsupported."""
    if backend == "postgresql":
        return POSTGRES
    if backend == "mysql":
        return MYSQL
    if backend == "mariadb":
        raise UserError(
            f"{connection}: MariaDB is not supported",
            hint="it has no max_execution_time, which bounds every query; "
            "use MySQL 8 or PostgreSQL",
        )
    raise UserError(
        f"{connection}: unsupported database {backend!r}",
        hint="use a postgresql+asyncpg:// or mysql+asyncmy:// URL",
    )


_NAMES = "check the names with search_schema / describe_tables; don't guess"
_DENIED = (
    "the account isn't allowed this: it may only read, and only what it was granted. "
    "Don't retry — tell the user it isn't available"
)
_SLOW = "the query took too long: filter more, narrow the time range, or aggregate first"
_READ_ONLY = "this database is read-only: only SELECT queries run"
_SYNTAX = "check the SQL against this database's dialect"
_GROUP_BY = "every selected column must be in GROUP BY or inside an aggregate"


# ---- PostgreSQL ------------------------------------------------------------


def _pg_session_args(timeout_ms: int, tz: str | None, connect_timeout_s: int) -> dict[str, Any]:
    # No read-only or statement_timeout startup parameters: poolers such as
    # PgBouncer and some hosted servers refuse them. The account is read-only
    # (see the README), and every statement gets its limit from _pg_bound.
    # TimeZone applies at startup, outside any transaction: a SET inside one
    # would be undone by the rollback that ends it.
    args: dict[str, Any] = {"timeout": connect_timeout_s}
    if tz:
        args["server_settings"] = {"TimeZone": _pg_time_zone(tz)}
    return args


def _pg_bound(sql: str, ms: int) -> tuple[str | None, str]:
    # Undone when the connection returns to the pool and its transaction ends.
    return f"SET LOCAL statement_timeout = {ms}", sql


def _pg_time_zone(tz: str) -> str:
    """An ISO offset as PostgreSQL means it: it reads ``+08:00`` POSIX-style,
    as eight hours *west* of UTC, so east must be spelled ``UTC-08:00``."""
    m = _OFFSET.fullmatch(tz)
    if not m:
        return tz  # a zone name
    sign = "-" if m.group(1) == "+" else "+"
    return f"UTC{sign}{m.group(2)}:{m.group(3)}"


_OFFSET = re.compile(r"([+-])(\d{1,2}):?(\d{2})")

# Readable tables, views and materialized views; partitions roll up into their parent.
_PG_TABLES = """
SELECT n.nspname, c.relname,
       CASE WHEN c.relkind IN ('v', 'm') THEN 'view' ELSE 'table' END,
       obj_description(c.oid, 'pg_class'),
       CASE WHEN c.reltuples < 0 THEN NULL ELSE c.reltuples::bigint END
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f') AND NOT c.relispartition
  AND n.nspname NOT IN :system AND n.nspname NOT LIKE 'pg\\_toast%'
  AND has_table_privilege(c.oid, 'SELECT'){filter}
"""
_PG_COLUMNS = """
SELECT n.nspname, c.relname, a.attname, format_type(a.atttypid, a.atttypmod),
       NOT a.attnotnull, col_description(c.oid, a.attnum)
FROM pg_attribute a
JOIN pg_class c ON c.oid = a.attrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE a.attnum > 0 AND NOT a.attisdropped
  AND c.relkind IN ('r', 'p', 'v', 'm', 'f') AND NOT c.relispartition
  AND n.nspname NOT IN :system AND n.nspname NOT LIKE 'pg\\_toast%'
  AND has_table_privilege(c.oid, 'SELECT'){filter}
ORDER BY n.nspname, c.relname, a.attnum
"""

POSTGRES = Dialect(
    name="postgres",
    display="PostgreSQL",
    system_schemas=("pg_catalog", "information_schema"),
    schema_is_database=False,
    session_args=_pg_session_args,
    bound=_pg_bound,
    bound_each=True,
    version_sql="SHOW server_version",
    read_only_sql="SHOW transaction_read_only",
    tables_sql=_PG_TABLES,
    columns_sql=_PG_COLUMNS,
    schema_column="n.nspname",
    notes="PostgreSQL — date_trunc('month', t), to_char(t, 'YYYY-MM'), ILIKE, "
    '"double quotes" for identifiers, x::numeric casts',
    hints={
        "42703": _NAMES,  # undefined column
        "42P01": _NAMES,  # undefined table
        "42883": "no such function (or wrong argument types) in this dialect; cast or use another",
        "57014": _SLOW,
        "42501": _DENIED,
        "25006": _READ_ONLY,
        "42601": _SYNTAX,
        "22012": "guard the divisor, e.g. NULLIF(x, 0)",
        "42803": _GROUP_BY,
    },
    connection_codes=frozenset(
        {
            *("08000", "08001", "08003", "08004", "08006", "08007", "08P01"),  # class 08
            "57P01",  # admin shutdown
            "57P02",  # crash shutdown
            "57P03",  # cannot connect now
            "53300",  # too many connections
        }
    ),
    # Dates and timestamps as text, any year (see pgtypes.py).
    on_connect=pgtypes.register,
)


# ---- MySQL -----------------------------------------------------------------


def _mysql_session_args(timeout_ms: int, tz: str | None, connect_timeout_s: int) -> dict[str, Any]:
    # One SET statement: init_command runs without multi-statement support.
    sets = ["SESSION transaction_read_only = 1", f"SESSION max_execution_time = {timeout_ms}"]
    if tz:
        sets.append(f"time_zone = '{tz}'")  # validated in config.py
    return {"init_command": "SET " + ", ".join(sets), "connect_timeout": connect_timeout_s}


def _mysql_bound(sql: str, ms: int) -> tuple[str | None, str]:
    # An optimizer hint: it bounds this SELECT only.
    return None, _SELECT.sub(rf"\g<0> /*+ MAX_EXECUTION_TIME({ms}) */", sql, count=1)


_SELECT = re.compile(r"^\s*SELECT\b", re.I)

_MYSQL_TABLES = """
SELECT TABLE_SCHEMA, TABLE_NAME,
       CASE WHEN TABLE_TYPE = 'VIEW' THEN 'view' ELSE 'table' END,
       TABLE_COMMENT, TABLE_ROWS
FROM information_schema.TABLES
WHERE TABLE_SCHEMA NOT IN :system{filter}
"""
_MYSQL_COLUMNS = """
SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, COLUMN_TYPE,
       IS_NULLABLE = 'YES', COLUMN_COMMENT
FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA NOT IN :system{filter}
ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION
"""

MYSQL = Dialect(
    name="mysql",
    display="MySQL",
    system_schemas=("mysql", "sys", "information_schema", "performance_schema"),
    schema_is_database=True,
    session_args=_mysql_session_args,
    bound=_mysql_bound,
    bound_each=False,
    version_sql="SELECT VERSION()",
    read_only_sql="SELECT @@SESSION.transaction_read_only",
    tables_sql=_MYSQL_TABLES,
    columns_sql=_MYSQL_COLUMNS,
    schema_column="TABLE_SCHEMA",
    notes="MySQL — DATE_FORMAT(t, '%Y-%m'), DATE(t), `backticks` for identifiers, "
    "LIMIT n OFFSET m, SHOW for server facts",
    hints={
        "1054": _NAMES,  # unknown column
        "1146": _NAMES,  # table doesn't exist
        "1305": "no such function in this dialect; use another",
        "3024": _SLOW,
        "1142": _DENIED,  # command denied (also a write the grants don't allow)
        "1792": _READ_ONLY,
        "1064": _SYNTAX,
        "1055": _GROUP_BY,
    },
    connection_codes=frozenset(
        {
            "1040",  # too many connections
            "1053",  # server shutdown in progress
            "2002",  # can't connect through the socket
            "2003",  # can't connect to the server
            "2005",  # unknown host
            "2006",  # server has gone away
            "2013",  # lost connection during query
        }
    ),
)
