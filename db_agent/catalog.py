"""What tables and columns a database has: loaded once, searched in memory.

Two queries pull every readable table and column (with comments and row
estimates) — cheap even for thousands of tables. The system prompt gets as
much of the table list as a budget allows; `search_schema` finds the rest,
so discovery works the same for ten tables and ten thousand.
"""

from __future__ import annotations

import fnmatch
import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import bindparam, inspect, text

from .execute import Database

__all__ = ["Catalog", "Column", "Table", "describe", "load_catalog"]

COMMENT_CHARS = 60


@dataclass
class Column:
    name: str
    type: str
    nullable: bool
    comment: str = ""


@dataclass
class Table:
    schema: str
    name: str
    kind: str  # "table" | "view"
    comment: str = ""
    est_rows: int | None = None
    columns: list[Column] = field(default_factory=list)

    @property
    def qualified(self) -> str:
        return f"{self.schema}.{self.name}"

    def heading(self) -> str:
        """``shop.orders (table, ~6,000 rows) — 订单``"""
        rows = f", ~{self.est_rows:,} rows" if self.est_rows is not None else ""
        note = f" — {self.comment}" if self.comment else ""
        return f"{self.qualified} ({self.kind}{rows}){note}"


@dataclass
class Catalog:
    tables: list[Table]
    loaded_at: float = field(default_factory=time.monotonic)

    def find(self, ref: str) -> list[Table]:
        """Tables named ``ref`` — ``name`` or ``schema.name``, any case."""
        want = ref.strip().strip('"`').lower()
        exact = [t for t in self.tables if t.qualified.lower() == want]
        return exact or [t for t in self.tables if t.name.lower() == want]

    def search(self, query: str, limit: int) -> list[tuple[Table, list[Column]]]:
        """Tables matching any word of ``query``, best first, with the columns that matched.

        Names count more than comments, tables more than columns. Plain
        substring matching, so comments in any language match as written.
        """
        words = [w for w in query.lower().replace(",", " ").split() if w]
        if not words or words == ["*"]:
            ranked = sorted(self.tables, key=lambda t: -(t.est_rows or 0))
            return [(t, []) for t in ranked[:limit]]
        scored: list[tuple[int, Table, list[Column]]] = []
        for t in self.tables:
            score = 0
            cols: list[Column] = []
            name, comment = t.name.lower(), t.comment.lower()
            for w in words:
                score += 20 if name == w else 10 if w in name else 0
                score += 4 if w in comment else 0
                best = 0  # per word, only the best column counts: many hits ≠ relevance
                for c in t.columns:
                    hit = 5 if w in c.name.lower() else 2 if w in c.comment.lower() else 0
                    if hit:
                        best = max(best, hit)
                        if c not in cols:
                            cols.append(c)
                score += best
            if score:
                scored.append((score, t, cols))
        scored.sort(key=lambda s: (-s[0], -(s[1].est_rows or 0), s[1].qualified))
        return [(t, cols) for _, t, cols in scored[:limit]]

    def overview(self, budget_bytes: int) -> str:
        """The table list for the system prompt, at most ``budget_bytes`` of UTF-8.

        All of it when it fits; otherwise the largest tables (by estimated
        rows) and a pointer to `search_schema` for the rest. Bytes, not
        characters: tokens track bytes (a CJK character is three).
        """

        def line(t: Table) -> str:
            note = _clip(t.comment, COMMENT_CHARS)
            return f"{t.qualified}" + (f" ({note})" if note else "")

        def size(s: str) -> int:
            return len(s.encode()) + 2  # with the ", " separator

        lines = [line(t) for t in sorted(self.tables, key=lambda t: t.qualified)]
        if not lines:
            return "no readable tables"
        if len("tables: ") + sum(size(s) for s in lines) <= budget_bytes:
            return "tables: " + ", ".join(lines)
        schemas = sorted({t.schema for t in self.tables})
        head = (
            f"{len(self.tables):,} tables in {', '.join(schemas)}; "
            "the largest are listed, find the others with search_schema: "
        )
        shown: list[str] = []
        used = size(head)
        for t in sorted(self.tables, key=lambda t: -(t.est_rows or 0)):
            s = line(t)
            if used + size(s) > budget_bytes:
                break
            shown.append(s)
            used += size(s)
        return head + ", ".join(shown)


async def load_catalog(db: Database) -> Catalog:
    """Every table and column the account can read, in the configured schemas."""
    schemas = db.default_schemas()
    if db.dialect == "postgres":
        tables_sql, columns_sql = _PG_TABLES, _PG_COLUMNS
    else:
        tables_sql, columns_sql = _MYSQL_TABLES, _MYSQL_COLUMNS
    filt = " AND {col} IN :schemas" if schemas else ""
    params: dict[str, Any] = {"system": list(db.system_schemas)}
    if schemas:
        params["schemas"] = list(schemas)

    async def rows(sql: str, col: str) -> list[Any]:
        stmt = text(sql.replace("{filter}", filt.format(col=col))).bindparams(
            bindparam("system", expanding=True),
            *([bindparam("schemas", expanding=True)] if schemas else []),
        )
        async with db.engine.connect() as conn:
            return list((await conn.execute(stmt, params)).all())

    schema_col = "n.nspname" if db.dialect == "postgres" else "TABLE_SCHEMA"
    by_key: dict[tuple[str, str], Table] = {}
    for schema, name, kind, comment, est in await rows(tables_sql, schema_col):
        if _excluded(db, schema, name):
            continue
        comment = "" if comment in (None, "VIEW") else str(comment)
        est_rows = int(est) if est is not None and int(est) >= 0 else None
        by_key[(schema, name)] = Table(schema, name, kind, comment, est_rows)
    for schema, name, col, typ, nullable, comment in await rows(columns_sql, schema_col):
        t = by_key.get((schema, name))
        if t is not None:
            t.columns.append(Column(col, str(typ), bool(nullable), str(comment or "")))
    return Catalog(list(by_key.values()))


async def describe(db: Database, table: Table) -> str:
    """Columns, keys, indexes and three sample rows of one table, as text."""

    def keys(sync_conn: Any) -> dict[str, Any]:
        insp = inspect(sync_conn)
        out: dict[str, Any] = {}
        for key, fn in (
            ("pk", insp.get_pk_constraint),
            ("fks", insp.get_foreign_keys),
            ("indexes", insp.get_indexes),
        ):
            try:
                out[key] = fn(table.name, schema=table.schema)
            except Exception:  # noqa: BLE001 - views and odd objects lack some of these
                out[key] = None
        return out

    async with db.engine.connect() as conn:
        info = await conn.run_sync(keys)

    lines = [f"## {table.heading()}", "columns:"]
    for c in table.columns:
        null = "" if c.nullable else " not null"
        note = f" — {c.comment}" if c.comment else ""
        lines.append(f"- {c.name} {c.type}{null}{note}")
    pk = (info.get("pk") or {}).get("constrained_columns") or []
    if pk:
        lines.append(f"primary key: ({', '.join(pk)})")
    for fk in info.get("fks") or []:
        ref = ".".join(p for p in (fk.get("referred_schema"), fk.get("referred_table")) if p)
        lines.append(
            f"foreign key: ({', '.join(fk['constrained_columns'])}) → "
            f"{ref}({', '.join(fk['referred_columns'])})"
        )
    for ix in info.get("indexes") or []:
        cols = ", ".join(c for c in ix.get("column_names") or [] if c)
        if cols:
            lines.append(f"index: {ix.get('name')} ({cols}){' unique' if ix.get('unique') else ''}")

    sample = await db.run(
        f"SELECT * FROM {db.quote(table.schema, table.name)}", max_rows=3, checked=False
    )
    if sample.rows:
        lines.append("sample rows:")
        lines.append("\t".join(sample.columns))
        lines.extend("\t".join(_cell(v) for v in r) for r in sample.rows)
    return "\n".join(lines)


def _excluded(db: Database, schema: str, name: str) -> bool:
    return any(
        fnmatch.fnmatchcase(name, p) or fnmatch.fnmatchcase(f"{schema}.{name}", p)
        for p in db.config.exclude_tables
    )


def _clip(s: str, n: int) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _cell(v: Any) -> str:
    return "" if v is None else _clip(str(v), 60)


# PostgreSQL: readable tables, views and materialized views; partitions roll up
# into their parent.
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
