"""Check a model-written statement and cap the rows it can return.

Not the security boundary: the read-only account (and the read-only session,
see `execute.py`) is. This layer turns a statement the database would refuse —
or one that would run for minutes — into a tool error the model can act on,
and puts a LIMIT on the outermost query so a huge result never leaves the
server. (Truncating while fetching is not enough: MySQL's driver reads a whole
result before the connection can be reused.)
"""

from __future__ import annotations

import logging
import re
from typing import Any

import sqlglot
from lovia import ToolError
from sqlglot import exp
from sqlglot.errors import SqlglotError

__all__ = ["Dialect", "prepare"]

Dialect = str  # "postgres" | "mysql" — sqlglot's names

# sqlglot logs a warning for every statement it falls back to parsing as an
# opaque Command (EXPLAIN, among others); we handle those ourselves.
logging.getLogger("sqlglot").setLevel(logging.ERROR)

_WRITES = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.TruncateTable,
    exp.Command,
)
# Functions that sleep, read the server's files, or reach out of the session.
# A properly limited account can't run most of them anyway; naming them here
# gives the model a clear error instead of a timeout or a permission failure.
_FUNCTIONS = frozenset(
    {
        "pg_sleep",
        "pg_sleep_for",
        "pg_sleep_until",
        "pg_read_file",
        "pg_read_binary_file",
        "pg_ls_dir",
        "lo_import",
        "lo_export",
        "dblink",
        "dblink_exec",
        "pg_terminate_backend",
        "pg_cancel_backend",
        "set_config",
        "sleep",
        "benchmark",
        "load_file",
        "get_lock",
    }
)
_QUERIES = (exp.Select, exp.Union, exp.Intersect, exp.Except)
# EXPLAIN's options before the statement: ANALYZE, VERBOSE, FORMAT=JSON, (…).
_EXPLAIN_OPTS = re.compile(
    r"^\s*(?:(?:analyze|verbose|extended|format\s*=\s*\w+)\b\s*|\([^)]*\)\s*)*", re.I
)

_HINT_ONE = "send one read-only statement: SELECT / WITH … SELECT / EXPLAIN / SHOW"


def prepare(sql: str, dialect: Dialect, limit: int | None) -> str:
    """The statement to run, its outermost LIMIT capped at ``limit``.

    Raise :class:`ToolError` (with a hint) unless ``sql`` is exactly one
    read-only statement. EXPLAIN and SHOW pass through unchanged.
    """
    try:
        trees = [t for t in sqlglot.parse(sql, read=dialect) if t is not None]
    except SqlglotError as exc:
        raise ToolError(
            f"could not parse the SQL: {_first_line(exc)}",
            hint="write it as one standard SELECT for this database's dialect",
        ) from None
    if not trees:
        raise ToolError("no SQL statement found", hint=_HINT_ONE)
    if len(trees) > 1:
        raise ToolError(f"got {len(trees)} statements; run one at a time", hint=_HINT_ONE)
    tree = trees[0]

    if isinstance(tree, exp.Command) and tree.name.upper() == "EXPLAIN":
        inner = _EXPLAIN_OPTS.sub("", tree.expression.name if tree.expression else "")
        prepare(inner, dialect, None)  # the explained statement must pass too
        return sql
    if isinstance(tree, exp.Show):
        return sql
    if not isinstance(tree, _QUERIES):
        raise ToolError(f"{_kind(tree)} is not allowed: this database is read-only", hint=_HINT_ONE)

    for node in tree.walk():
        if isinstance(node, _WRITES):
            raise ToolError(
                f"{_kind(node)} is not allowed: this database is read-only", hint=_HINT_ONE
            )
        if isinstance(node, exp.Func):
            name = (node.name if isinstance(node, exp.Anonymous) else node.sql_name()).lower()
            if name in _FUNCTIONS:
                raise ToolError(
                    f"the function {name}() is not allowed",
                    hint="query the data only; sleeping, file access and session "
                    "changes are off limits",
                )
        if isinstance(node, (exp.Select, *_QUERIES)) and node.args.get("into"):
            raise ToolError("SELECT … INTO writes a table and is not allowed", hint=_HINT_ONE)
        if isinstance(node, exp.Lock):
            raise ToolError(
                "locking clauses (FOR UPDATE / FOR SHARE) are not allowed",
                hint="drop the FOR … clause; reads need no locks",
            )

    if limit is None or _within(tree, limit):
        return sql
    # The `limit` arg holds a LIMIT or a FETCH FIRST; either way, replace it.
    tree.set("limit", exp.Limit(expression=exp.Literal.number(limit)))
    return tree.sql(dialect=dialect)


def _within(tree: exp.Expression, limit: int) -> bool:
    """Does the outermost query already return at most ``limit`` rows?"""
    clause = tree.args.get("limit")
    if clause is None:
        return False
    n = clause.args.get("count") if isinstance(clause, exp.Fetch) else clause.args.get("expression")
    return isinstance(n, exp.Literal) and n.is_int and int(n.name) <= limit


def _kind(node: Any) -> str:
    if isinstance(node, exp.Command):
        return node.name.upper() or "this command"
    return str(node.key).upper()


def _first_line(exc: Exception) -> str:
    return str(exc).strip().splitlines()[0][:200] if str(exc).strip() else type(exc).__name__
