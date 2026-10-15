"""Run statements against one configured database, read-only and time-boxed.

Every connection in the pool starts its session read-only, with the
statement timeout and time zone set — at connection startup, outside any
transaction (a PostgreSQL ``SET`` inside a transaction is undone by the
rollback that ends it). The guard rejects ``SET`` and ``set_config()``, so a
statement can't change them back.

Every wait is bounded — connecting, a pool slot, the statement — on the
server and again on the client: a server-side limit can't end a read from a
connection that stopped answering. Every failure of a model-written
statement leaves :meth:`Database.run` as a :class:`ToolError` with a hint,
never as a bare driver exception.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import sqlalchemy.exc as sa_exc
from lovia import ToolError, UserError
from sqlalchemy import bindparam, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from . import guard, pgtypes
from .config import Connection, Limits
from .convert import to_json

__all__ = ["CLEANUP_S", "Database", "QueryResult", "client_wait"]

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_S = 10
POOL_TIMEOUT_S = 10
DB_WAIT_S = CONNECT_TIMEOUT_S + POOL_TIMEOUT_S
"""The most a statement waits before it starts: a pool slot, then a connection."""
GRACE_S = 3
"""How much longer than the server's limit the client waits for its answer."""
CLEANUP_S = 2
"""What closing a connection that stopped answering takes (SQLAlchemy's limit)."""

_SYSTEM_SCHEMAS = {
    "postgres": ("pg_catalog", "information_schema"),
    "mysql": ("mysql", "sys", "information_schema", "performance_schema"),
}


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[list[Any]]
    """JSON-safe cells (see convert.py)."""
    truncated: bool
    """More rows existed than were fetched."""
    elapsed_ms: int


class Database:
    """One named connection: a lazily created pool and the dialect's quirks."""

    def __init__(self, name: str, connection: Connection, limits: Limits) -> None:
        self.name = name
        self.config = connection
        self.limits = limits
        url = make_url(connection.url())
        backend = url.get_backend_name()
        if backend not in ("postgresql", "mysql", "mariadb"):
            raise UserError(
                f"{name}: unsupported database {backend!r}",
                hint="use a postgresql+asyncpg:// or mysql+asyncmy:// URL",
            )
        self.dialect: guard.Dialect = "postgres" if backend == "postgresql" else "mysql"
        self._url = url
        self._engine: AsyncEngine | None = None
        self.version = ""

    @property
    def label(self) -> str:
        """What the model reads: "PostgreSQL 16.4" / "MySQL 8.0.36"."""
        kind = "PostgreSQL" if self.dialect == "postgres" else "MySQL"
        return f"{kind} {self.version}".strip()

    def default_schemas(self) -> list[str] | None:
        """The schemas to discover; None means every non-system one."""
        if self.config.schemas is not None:
            return self.config.schemas
        if self.dialect == "mysql" and self._url.database:
            return [self._url.database]
        return None

    @property
    def system_schemas(self) -> tuple[str, ...]:
        return _SYSTEM_SCHEMAS[self.dialect]

    @property
    def engine(self) -> AsyncEngine:
        if self._engine is None:
            engine = create_async_engine(
                self._url,
                pool_size=5,
                max_overflow=5,
                pool_pre_ping=True,
                pool_recycle=1800,
                pool_timeout=POOL_TIMEOUT_S,
                connect_args=self._session_args(),
            )
            if self.dialect == "postgres":

                @event.listens_for(engine.sync_engine, "connect")
                def _codecs(dbapi_conn: Any, _record: Any) -> None:
                    dbapi_conn.run_async(pgtypes.register)

            self._engine = engine
        return self._engine

    def _session_args(self) -> dict[str, Any]:
        ms = _ms_of(self.limits.statement_timeout_s)
        tz = self.config.timezone
        if self.dialect == "postgres":
            settings = {"default_transaction_read_only": "on", "statement_timeout": str(ms)}
            if tz:
                settings["TimeZone"] = _pg_time_zone(tz)
            return {"server_settings": settings, "timeout": CONNECT_TIMEOUT_S}
        # One SET statement: init_command runs without multi-statement support.
        sets = ["SESSION transaction_read_only = 1", f"SESSION max_execution_time = {ms}"]
        if tz:
            sets.append(f"time_zone = '{tz}'")  # validated in config.py
        return {"init_command": "SET " + ", ".join(sets), "connect_timeout": CONNECT_TIMEOUT_S}

    def quote(self, *parts: str) -> str:
        """A qualified identifier in this dialect's quoting."""
        q = self.engine.dialect.identifier_preparer.quote_identifier
        return ".".join(q(p) for p in parts)

    async def run(
        self,
        sql: str,
        *,
        max_rows: int,
        checked: bool = True,
        timeout_s: float | None = None,
    ) -> QueryResult:
        """Run a model-written statement (``checked``) or one of ours.

        At most ``max_rows`` rows come back; ``truncated`` says whether there
        were more. ``timeout_s`` replaces the statement timeout for one of our
        own SELECTs. Any failure becomes a :class:`ToolError` with a hint.
        """
        stmt = guard.prepare(sql, self.dialect, max_rows + 1) if checked else sql
        start = time.perf_counter()

        async def go() -> QueryResult:
            async with self.engine.connect() as conn:
                sql = await self._bound(conn, stmt, timeout_s)
                # exec_driver_sql: the text goes to the driver as is, so a
                # literal like '12:30' or '%x%' isn't read as a bind parameter.
                result = await conn.exec_driver_sql(sql, execution_options={"no_parameters": True})
                if not result.returns_rows:
                    return QueryResult([], [], False, _ms(start))
                columns = [str(c) for c in result.keys()]
                rows = result.fetchmany(max_rows + 1)
                cells = [[to_json(v) for v in r] for r in rows[:max_rows]]
            return QueryResult(columns, cells, len(rows) > max_rows, _ms(start))

        try:
            return await asyncio.wait_for(go(), self.deadline(timeout_s))
        except Exception as exc:  # noqa: BLE001 - every failure reaches the model as a ToolError
            raise self._tool_error(exc) from None

    async def fetch(
        self,
        sql: str,
        params: Mapping[str, Any] | None = None,
        *,
        expanding: Iterable[str] = (),
        timeout_s: float | None = None,
    ) -> Sequence[Any]:
        """Rows of one of our own (trusted, parameterized) queries.

        ``expanding`` names list parameters (``IN :names``); ``timeout_s`` as
        in :meth:`run`. Errors propagate as they are.
        """

        async def go() -> Sequence[Any]:
            async with self.engine.connect() as conn:
                bounded = await self._bound(conn, sql, timeout_s)
                stmt = text(bounded).bindparams(*(bindparam(n, expanding=True) for n in expanding))
                return (await conn.execute(stmt, dict(params or {}))).all()

        return await asyncio.wait_for(go(), self.deadline(timeout_s))

    async def reflect(self, fn: Callable[[Any], Any], *, statements: int) -> Any:
        """``fn(sync_connection)`` — SQLAlchemy inspection — bounded like
        ``statements`` statements. Errors propagate as they are."""

        async def go() -> Any:
            async with self.engine.connect() as conn:
                return await conn.run_sync(fn)

        return await asyncio.wait_for(go(), self.deadline(None, statements=statements))

    def deadline(self, timeout_s: float | None = None, *, statements: int = 1) -> float:
        limit = self.limits.statement_timeout_s if timeout_s is None else timeout_s
        return client_wait(limit, statements)

    async def _bound(self, conn: AsyncConnection, sql: str, timeout_s: float | None) -> str:
        """Give one statement of ours its own time limit, longer or shorter
        than the session's: PostgreSQL by ``SET LOCAL`` (undone when the
        connection returns to the pool), MySQL by an optimizer hint."""
        if timeout_s is None:
            return sql
        ms = _ms_of(timeout_s)
        if self.dialect == "postgres":
            await conn.exec_driver_sql(f"SET LOCAL statement_timeout = {ms}")
            return sql
        return _SELECT.sub(rf"\g<0> /*+ MAX_EXECUTION_TIME({ms}) */", sql, count=1)

    async def load_version(self) -> str:
        sql = "SHOW server_version" if self.dialect == "postgres" else "SELECT VERSION()"
        rows = await self.fetch(sql)
        self.version = str(rows[0][0]).split()[0] if rows else ""
        return self.version

    async def aclose(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None

    def _tool_error(self, exc: Exception) -> ToolError:
        if isinstance(exc, sa_exc.DBAPIError) and not _unreachable(exc):
            message = self._scrub(_message(exc))
            return ToolError(f"{self.name}: {message}", hint=_hint(_code(exc)))
        if isinstance(exc, (sa_exc.DBAPIError, sa_exc.TimeoutError, OSError, asyncio.TimeoutError)):
            # A lost connection, no pool slot, a refused or slow connect, no answer.
            message = f"{self.name} is unreachable or busy: {type(exc).__name__}"
            if isinstance(exc, sa_exc.DBAPIError):
                message += f" ({self._scrub(_message(exc))})"
            return ToolError(message, hint=_RETRY)
        # Not the database's error: the driver failed to read a value, say.
        logger.warning("%s: unexpected error running a statement", self.name, exc_info=exc)
        return ToolError(
            f"{self.name}: {type(exc).__name__}: {self._scrub(str(exc))[:300]}",
            hint="if a column has an unusual type, cast it to text in SQL; otherwise " + _RETRY,
        )

    def _scrub(self, message: str) -> str:
        for secret in (self._url.password, self._url.username, self._url.host):
            if secret and len(str(secret)) > 2:
                message = message.replace(str(secret), "***")
        return message


_SELECT = re.compile(r"^\s*SELECT\b", re.I)
_RETRY = "retry once shortly; if it keeps failing, tell the user"


def _message(exc: sa_exc.DBAPIError) -> str:
    raw = str(exc.orig) if exc.orig is not None else str(exc)
    # asyncpg via SQLAlchemy: "<class 'asyncpg.exceptions.UndefinedColumnError'>: …"
    raw = re.sub(r"^<class '[\w.]+'>:\s*", "", raw.strip())
    # asyncmy: "(1054, \"Unknown column 'x' in 'field list'\")"
    m = re.match(r"^\((\d+),\s*['\"](.*)['\"]\)$", raw, re.S)
    if m:
        raw = m.group(2)
    return raw.splitlines()[0][:500] if raw else type(exc.orig or exc).__name__


def client_wait(statement_s: float, statements: int = 1) -> float:
    """How long the client waits for one call: a pool slot and a connection,
    then the server's limit per statement, then some grace. Past it, the
    connection is dropped (taking up to :data:`CLEANUP_S` more)."""
    return DB_WAIT_S + statements * statement_s + GRACE_S


def _unreachable(exc: sa_exc.DBAPIError) -> bool:
    """A failure of the connection, not of the statement: no SQL fixes it."""
    code = _code(exc)
    return exc.connection_invalidated or code in _CONNECTION_CODES or code.startswith("08")


# MySQL client and server errors about the connection itself (PostgreSQL's
# are SQLSTATE class 08, plus the shutdown and overload states below).
_CONNECTION_CODES = frozenset(
    {
        "1040",  # too many connections
        "1053",  # server shutdown in progress
        "2002",  # can't connect through the socket
        "2003",  # can't connect to the server
        "2005",  # unknown host
        "2006",  # server has gone away
        "2013",  # lost connection during query
        "57P01",  # admin shutdown
        "57P02",  # crash shutdown
        "57P03",  # cannot connect now
        "53300",  # too many connections
    }
)


def _code(exc: sa_exc.DBAPIError) -> str:
    """The error's SQLSTATE (PostgreSQL) or error number (MySQL), as text.

    Hints key on codes, not messages: servers localize their messages.
    """
    orig = exc.orig
    # MySQL errors carry (errno, message); check that first — a MySQL SQLSTATE
    # is coarse (42000 covers syntax errors and denied commands alike).
    args = getattr(orig, "args", ())
    if args and isinstance(args[0], int):
        return str(args[0])
    state = getattr(orig, "sqlstate", None)
    return str(state) if state else ""


_NAMES = "check the names with search_schema / describe_tables; don't guess"
_DENIED = (
    "the account isn't allowed this: it may only read, and only what it was granted. "
    "Don't retry — tell the user it isn't available"
)
_HINTS = {
    # PostgreSQL SQLSTATEs
    "42703": _NAMES,  # undefined column
    "42P01": _NAMES,  # undefined table
    "42883": "no such function (or wrong argument types) in this dialect; cast or use another",
    "57014": "the query took too long: filter more, narrow the time range, or aggregate first",
    "42501": _DENIED,
    "25006": "this database is read-only: only SELECT queries run",
    "42601": "check the SQL against this database's dialect",
    "22012": "guard the divisor, e.g. NULLIF(x, 0)",
    "42803": "every selected column must be in GROUP BY or inside an aggregate",
    # MySQL error numbers
    "1054": _NAMES,  # unknown column
    "1146": _NAMES,  # table doesn't exist
    "1305": "no such function in this dialect; use another",
    "3024": "the query took too long: filter more, narrow the time range, or aggregate first",
    "1142": _DENIED,  # command denied (also a write the grants don't allow)
    "1792": "this database is read-only: only SELECT queries run",
    "1064": "check the SQL against this database's dialect",
    "1055": "every selected column must be in GROUP BY or inside an aggregate",
}


def _hint(code: str) -> str:
    return _HINTS.get(code, "fix the SQL and try again")


def _pg_time_zone(tz: str) -> str:
    """An ISO offset as PostgreSQL means it: it reads ``+08:00`` POSIX-style,
    as eight hours *west* of UTC, so east must be spelled ``UTC-08:00``."""
    m = _OFFSET.fullmatch(tz)
    if not m:
        return tz  # a zone name
    sign = "-" if m.group(1) == "+" else "+"
    return f"UTC{sign}{m.group(2)}:{m.group(3)}"


_OFFSET = re.compile(r"([+-])(\d{1,2}):?(\d{2})")


def _ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


def _ms_of(seconds: float) -> int:
    # At least 1: zero means "no limit" to both servers.
    return max(1, int(seconds * 1000))
