"""Run statements against one configured database, read-only and time-boxed.

Every connection in the pool starts its session read-only, with the
statement timeout and time zone set — at connection startup, outside any
transaction (a PostgreSQL ``SET`` inside a transaction is undone by the
rollback that ends it). The guard rejects ``SET`` and ``set_config()``, so a
statement can't change them back.
"""

from __future__ import annotations

import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import sqlalchemy.exc as sa_exc
from lovia import ToolError, UserError
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from . import guard
from .config import Connection, Limits
from .convert import parse_tz, to_json

__all__ = ["Database", "QueryResult"]

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
        self._tz = parse_tz(connection.timezone)

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
            self._engine = create_async_engine(
                self._url,
                pool_size=5,
                max_overflow=5,
                pool_pre_ping=True,
                pool_recycle=1800,
                pool_timeout=10,
                connect_args=self._session_args(),
            )
        return self._engine

    def _session_args(self) -> dict[str, Any]:
        ms = int(self.limits.statement_timeout_s * 1000)
        tz = self.config.timezone
        if self.dialect == "postgres":
            settings = {"default_transaction_read_only": "on", "statement_timeout": str(ms)}
            if tz:
                settings["TimeZone"] = tz
            return {"server_settings": settings}
        # One SET statement: init_command runs without multi-statement support.
        sets = ["SESSION transaction_read_only = 1", f"SESSION max_execution_time = {ms}"]
        if tz:
            sets.append(f"time_zone = '{tz}'")  # validated in config.py
        return {"init_command": "SET " + ", ".join(sets)}

    def quote(self, *parts: str) -> str:
        """A qualified identifier in this dialect's quoting."""
        q = self.engine.dialect.identifier_preparer.quote_identifier
        return ".".join(q(p) for p in parts)

    async def run(self, sql: str, *, max_rows: int, checked: bool = True) -> QueryResult:
        """Run a model-written statement (``checked``) or one of ours.

        At most ``max_rows`` rows come back; ``truncated`` says whether there
        were more. Database errors become :class:`ToolError`s with a hint.
        """
        stmt = guard.prepare(sql, self.dialect, max_rows + 1) if checked else sql
        start = time.perf_counter()
        try:
            async with self.engine.connect() as conn:
                # exec_driver_sql: the text goes to the driver as is, so a
                # literal like '12:30' or '%x%' isn't read as a bind parameter.
                result = await conn.exec_driver_sql(stmt, execution_options={"no_parameters": True})
                if not result.returns_rows:
                    return QueryResult([], [], False, _ms(start))
                columns = [str(c) for c in result.keys()]
                rows = result.fetchmany(max_rows + 1)
        except sa_exc.DBAPIError as exc:
            raise self._tool_error(exc) from None
        except (sa_exc.TimeoutError, OSError) as exc:
            raise ToolError(
                f"{self.name} is unreachable or busy: {type(exc).__name__}",
                hint="retry once shortly; if it keeps failing, tell the user",
            ) from None
        return QueryResult(
            columns,
            [[to_json(v, self._tz) for v in r] for r in rows[:max_rows]],
            len(rows) > max_rows,
            _ms(start),
        )

    async def fetch(self, sql: str, params: Mapping[str, Any] | None = None) -> Sequence[Any]:
        """Rows of one of our own (trusted, parameterized) queries."""
        async with self.engine.connect() as conn:
            return (await conn.execute(text(sql), dict(params or {}))).all()

    async def load_version(self) -> str:
        sql = "SHOW server_version" if self.dialect == "postgres" else "SELECT VERSION()"
        rows = await self.fetch(sql)
        self.version = str(rows[0][0]).split()[0] if rows else ""
        return self.version

    async def aclose(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None

    def _tool_error(self, exc: sa_exc.DBAPIError) -> ToolError:
        message = self._scrub(_message(exc))
        return ToolError(f"{self.name}: {message}", hint=_hint(_code(exc)))

    def _scrub(self, message: str) -> str:
        for secret in (self._url.password, self._url.username, self._url.host):
            if secret and len(str(secret)) > 2:
                message = message.replace(str(secret), "***")
        return message


def _message(exc: sa_exc.DBAPIError) -> str:
    raw = str(exc.orig) if exc.orig is not None else str(exc)
    # asyncpg via SQLAlchemy: "<class 'asyncpg.exceptions.UndefinedColumnError'>: …"
    raw = re.sub(r"^<class '[\w.]+'>:\s*", "", raw.strip())
    # asyncmy: "(1054, \"Unknown column 'x' in 'field list'\")"
    m = re.match(r"^\((\d+),\s*['\"](.*)['\"]\)$", raw, re.S)
    if m:
        raw = m.group(2)
    return raw.splitlines()[0][:500] if raw else type(exc.orig or exc).__name__


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


def _ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)
