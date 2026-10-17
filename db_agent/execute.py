"""Run statements against one configured database, read-only and time-boxed.

Every statement runs with the configured time limit: MySQL sessions start
read-only with ``max_execution_time`` set; PostgreSQL bounds each statement
with ``SET LOCAL statement_timeout`` (poolers refuse those settings at
startup) and relies on the read-only account. The time zone is set at
connection startup, outside any transaction (a PostgreSQL ``SET`` inside a
transaction is undone by the rollback that ends it). The guard rejects
``SET`` and ``set_config()``, so a statement can't change them back.

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

from . import guard
from .config import Connection, Limits
from .convert import to_json
from .dialects import Dialect, for_backend

__all__ = ["CLEANUP_S", "Database", "QueryResult", "client_wait", "error_code"]

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_S = 10
POOL_TIMEOUT_S = 10
DB_WAIT_S = CONNECT_TIMEOUT_S + POOL_TIMEOUT_S
"""The most a statement waits before it starts: a pool slot, then a connection."""
GRACE_S = 3
"""How much longer than the server's limit the client waits for its answer."""
CLEANUP_S = 2
"""What closing a connection that stopped answering takes (SQLAlchemy's limit)."""


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
        try:
            url = make_url(connection.url())
        except (sa_exc.ArgumentError, ValueError):
            # Not the URL or the parser's message: either may hold the password.
            raise UserError(
                f"{connection.url_env} is not a valid database URL",
                hint="use postgresql+asyncpg://user:password@host:port/db "
                "or mysql+asyncmy://user:password@host:port/db",
            ) from None
        self.dialect: Dialect = for_backend(url.get_backend_name(), name)
        self._url = url
        self._engine: AsyncEngine | None = None
        self.version = ""

    @property
    def label(self) -> str:
        """What the model reads: "PostgreSQL 16.4" / "MySQL 8.0.36"."""
        return f"{self.dialect.display} {self.version}".strip()

    def default_schemas(self) -> list[str] | None:
        """The schemas to discover; None means every non-system one."""
        if self.config.schemas is not None:
            return self.config.schemas
        if self.dialect.schema_is_database and self._url.database:
            return [self._url.database]
        return None

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
                connect_args=self.dialect.session_args(
                    _ms_of(self.limits.statement_timeout_s),
                    self.config.timezone,
                    CONNECT_TIMEOUT_S,
                ),
            )
            hook = self.dialect.on_connect
            if hook is not None:

                @event.listens_for(engine.sync_engine, "connect")
                def _on_connect(dbapi_conn: Any, _record: Any) -> None:
                    dbapi_conn.run_async(hook)

            self._engine = engine
        return self._engine

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
        stmt = guard.prepare(sql, self.dialect.name, max_rows + 1) if checked else sql
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
                if self.dialect.bound_each:
                    # Only the statement run first: the limit lasts the transaction.
                    await self._bound(conn, "", None)
                return await conn.run_sync(fn)

        return await asyncio.wait_for(go(), self.deadline(None, statements=statements))

    def deadline(self, timeout_s: float | None = None, *, statements: int = 1) -> float:
        limit = self.limits.statement_timeout_s if timeout_s is None else timeout_s
        return client_wait(limit, statements)

    async def _bound(self, conn: AsyncConnection, sql: str, timeout_s: float | None) -> str:
        """Give one statement its time limit: ``timeout_s`` for one of ours,
        longer or shorter than the session's; else the configured one, when
        the session doesn't carry it (see :attr:`Dialect.bound`)."""
        if timeout_s is None:
            if not self.dialect.bound_each:
                return sql
            timeout_s = self.limits.statement_timeout_s
        first, sql = self.dialect.bound(sql, _ms_of(timeout_s))
        if first is not None:
            await conn.exec_driver_sql(first)
        return sql

    async def load_version(self) -> str:
        rows = await self.fetch(self.dialect.version_sql)
        self.version = str(rows[0][0]).split()[0] if rows else ""
        return self.version

    async def aclose(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None

    def _tool_error(self, exc: Exception) -> ToolError:
        if isinstance(exc, sa_exc.DBAPIError) and not _unreachable(exc, self.dialect):
            message = self.scrub(_message(exc))
            return ToolError(f"{self.name}: {message}", hint=self.dialect.hint(error_code(exc)))
        if isinstance(exc, (sa_exc.DBAPIError, sa_exc.TimeoutError, OSError, asyncio.TimeoutError)):
            # A lost connection, no pool slot, a refused or slow connect, no answer.
            message = f"{self.name} is unreachable or busy: {type(exc).__name__}"
            if isinstance(exc, sa_exc.DBAPIError):
                message += f" ({self.scrub(_message(exc))})"
            return ToolError(message, hint=_RETRY)
        # Not the database's error: the driver failed to read a value, say.
        logger.warning("%s: unexpected error running a statement", self.name, exc_info=exc)
        return ToolError(
            f"{self.name}: {type(exc).__name__}: {self.scrub(str(exc))[:300]}",
            hint="if a column has an unusual type, cast it to text in SQL; otherwise " + _RETRY,
        )

    def scrub(self, message: str) -> str:
        """``message`` without the URL's password, user name and host.

        Whole words only: a user named ``postgres`` leaves ``postgresql`` alone."""
        for secret in (self._url.password, self._url.username, self._url.host):
            if secret and len(str(secret)) > 2:
                # ASCII neighbors only: a localized message may put CJK text
                # right against a name, and that must still be scrubbed.
                word = rf"(?<![A-Za-z0-9_.-]){re.escape(str(secret))}(?![A-Za-z0-9_.-])"
                message = re.sub(word, "***", message)
        return message

    def error_text(self, exc: BaseException) -> str:
        """One scrubbed line saying what went wrong, for a person to read."""
        if isinstance(exc, sa_exc.DBAPIError):
            return self.scrub(_message(exc))
        line = (str(exc).splitlines() or [""])[0][:300]  # a timeout's message is empty
        return type(exc).__name__ + (f": {self.scrub(line)}" if line else "")


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


def _unreachable(exc: sa_exc.DBAPIError, dialect: Dialect) -> bool:
    """A failure of the connection, not of the statement: no SQL fixes it."""
    return exc.connection_invalidated or error_code(exc) in dialect.connection_codes


def error_code(exc: sa_exc.DBAPIError) -> str:
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


def _ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


def _ms_of(seconds: float) -> int:
    # At least 1: zero means "no limit" to both servers.
    return max(1, int(seconds * 1000))
