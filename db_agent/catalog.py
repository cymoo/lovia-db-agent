"""What tables and columns a database has: loaded once, searched in memory.

Three queries pull every readable table and column (with comments, row
estimates, enum labels) and every primary and foreign key — cheap even for
thousands of tables. The system prompt gets the schema in tiers, as a budget
allows: the most important tables with their columns, then names, then a
count; `search_schema` finds the rest, so discovery works the same for ten
tables and ten thousand. Only metadata reaches the prompt, never data values.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import math
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any

from lovia import ToolError
from sqlalchemy import inspect

from .convert import cell_text, clip
from .execute import Database

__all__ = [
    "CATALOG_TIMEOUT_S",
    "SAMPLE_TIMEOUT_S",
    "Catalog",
    "Column",
    "ForeignKey",
    "Table",
    "describe",
    "load_catalog",
]

logger = logging.getLogger(__name__)

COMMENT_CHARS = 60
BRIEF_COMMENT_CHARS = 40
"""A column comment in the system prompt's definitions."""
BRIEF_COLUMNS = 40
"""Columns a definition in the system prompt lists; describe_tables has the rest."""
ENUM_LABELS = 10
SHARD_MIN = 3
"""Tables alike but for a numeric suffix that collapse into one entry."""
NAMES_SHARE = 0.4
"""At most this share of the budget is held back for the names of tables
that don't get a definition."""
SAMPLE_ROWS = 3
SAMPLE_CELL_CHARS = 60
COMMON_VALUES = 20
"""A column with at most this many distinct values gets its common values described."""
CATALOG_TIMEOUT_S = 60
"""Reading information_schema on a big MySQL can outlast the statement timeout."""
SAMPLE_TIMEOUT_S = 3
"""A view's sample can cost its whole query; past this it is skipped. Also
the bound on reading a table's statistics."""
INDEX_STATEMENTS = 2
"""What reflecting a table's indexes is bounded like, in statements."""


@dataclass
class Column:
    name: str
    type: str
    nullable: bool
    comment: str = ""
    enum: list[str] = field(default_factory=list)
    """An enum's labels, in order."""


@dataclass(frozen=True)
class ForeignKey:
    columns: tuple[str, ...]
    ref_schema: str
    ref_table: str
    ref_columns: tuple[str, ...]

    @property
    def ref_qualified(self) -> str:
        return f"{self.ref_schema}.{self.ref_table}"


@dataclass
class Table:
    schema: str
    name: str
    kind: str  # "table" | "view"
    comment: str = ""
    est_rows: int | None = None
    columns: list[Column] = field(default_factory=list)
    primary_key: list[str] = field(default_factory=list)
    foreign_keys: list[ForeignKey] = field(default_factory=list)

    @property
    def qualified(self) -> str:
        return f"{self.schema}.{self.name}"

    def heading(self) -> str:
        """``shop.orders (table, ~6,000 rows) — 订单``"""
        rows = f", ~{self.est_rows:,} rows" if self.est_rows is not None else ""
        note = f" — {self.comment}" if self.comment else ""
        return f"{self.qualified} ({self.kind}{rows}){note}"


@dataclass
class _Entry:
    """A table in the overview, or a family of shards shown as its first."""

    tables: list[Table]
    importance: int

    @property
    def first(self) -> Table:
        return self.tables[0]

    @property
    def comment(self) -> str:
        return next((t.comment for t in self.tables if t.comment), "")

    def alike(self) -> str:
        """``17 more alike: events_2025_02 … events_2026_06``"""
        if len(self.tables) == 1:
            return ""
        rest = self.tables[1:]
        span = rest[0].name if len(rest) == 1 else f"{rest[0].name} … {rest[-1].name}"
        return f"{len(rest)} more alike: {span}"


@dataclass
class Catalog:
    tables: list[Table]
    loaded_at: float = field(default_factory=time.monotonic)

    def __post_init__(self) -> None:
        self._by_key = {(t.schema, t.name): t for t in self.tables}
        self._referrers: dict[tuple[str, str], list[tuple[Table, ForeignKey]]] = {}
        for t in self.tables:
            for fk in t.foreign_keys:
                key = (fk.ref_schema, fk.ref_table)
                self._referrers.setdefault(key, []).append((t, fk))

    def find(self, ref: str) -> list[Table]:
        """Tables named ``ref`` — ``name`` or ``schema.name``, any case."""
        want = ref.strip().strip('"`').lower()
        exact = [t for t in self.tables if t.qualified.lower() == want]
        return exact or [t for t in self.tables if t.name.lower() == want]

    def referrers(self, table: Table) -> list[tuple[Table, ForeignKey]]:
        """The foreign keys that point at ``table``."""
        return self._referrers.get((table.schema, table.name), [])

    @cached_property
    def fingerprint(self) -> str:
        """A hash of everything the overview shows except row estimates, which
        drift: a new fingerprint means the schema changed."""
        shape = [
            [
                t.qualified,
                t.kind,
                t.comment,
                [[c.name, c.type, c.nullable, c.comment, c.enum] for c in t.columns],
                t.primary_key,
                sorted(
                    [list(fk.columns), fk.ref_qualified, list(fk.ref_columns)]
                    for fk in t.foreign_keys
                ),
            ]
            for t in sorted(self.tables, key=lambda t: t.qualified)
        ]
        raw = json.dumps(shape, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

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
        """The schema for the system prompt, one line per entry, in about
        ``budget_bytes`` of UTF-8 (bytes, not characters: tokens track bytes,
        and a CJK character is three).

        One greedy pass in order of importance (foreign keys pointing at a
        table, a comment, the order of magnitude of its rows): first come
        definitions with columns and keys, as many as fit while room is kept
        for the names of the rest; then bare names; then a count of what's
        left. A small schema is all definitions; a big one degrades smoothly.
        Shard families (same columns, names differing in a numeric suffix)
        are one entry. Row estimates count only by order of magnitude, so the
        text stays put while the data grows.
        """
        if not self.tables:
            return "no readable tables"
        ranked = sorted(self._entries(), key=lambda e: (-e.importance, e.first.qualified))
        n = len(ranked)
        defs = [self._definition(e) for e in ranked]
        names = [self._name(e) for e in ranked]
        line = _line_bytes
        item = _item_bytes
        tail_reserve = line(_tail(n, everything=False))
        # Definitions for the k most important, the largest k that leaves room
        # for the names of the others (up to NAMES_SHARE of the budget).
        names_after = [0] * (n + 1)
        for i in range(n - 1, -1, -1):
            names_after[i] = names_after[i + 1] + item(names[i])
        reserve_cap = int(budget_bytes * NAMES_SHARE)
        k, used = 0, 0
        for i in range(n):
            rest = 0
            if i + 1 < n:
                rest = line(_NAMES_HEAD) + min(names_after[i + 1], reserve_cap) + tail_reserve
            if used + line(defs[i]) + rest > budget_bytes:
                break
            used += line(defs[i])
            k = i + 1
        shown: list[str] = []
        if k < n:
            used += line(_NAMES_HEAD)
            for i in range(k, n):
                if used + item(names[i]) + tail_reserve > budget_bytes:
                    break
                used += item(names[i])
                shown.append(names[i])
        m = k + len(shown)
        lines = sorted(defs[:k])
        if shown:
            head = _NAMES_HEAD if k else _ONLY_NAMES_HEAD
            lines.append(head + ", ".join(sorted(shown)))
        if m < n:
            lines.append(_tail(sum(len(e.tables) for e in ranked[m:]), everything=m == 0))
        return "\n".join(lines)

    def _entries(self) -> list[_Entry]:
        families: dict[tuple[Any, ...], list[Table]] = {}
        for t in self.tables:
            m = _SHARD.match(t.name)
            if m is None:
                families[("table", t.schema, t.name)] = [t]
                continue
            # Alike in everything the overview could show of any member, but the
            # digits ("2025年1月订单" and "2025年2月订单" are alike): only the
            # first is shown, so whatever tells them apart — a column's unit,
            # an enum label, a key's target column — must keep them apart.
            shape = (
                t.kind,
                _digitless(t.comment),
                tuple(
                    (c.name, c.type, c.nullable, _digitless(c.comment), tuple(c.enum))
                    for c in t.columns
                ),
                tuple(t.primary_key),
                tuple(
                    sorted((fk.columns, fk.ref_qualified, fk.ref_columns) for fk in t.foreign_keys)
                ),
            )
            families.setdefault(("family", t.schema, m.group(1), shape), []).append(t)
        entries: list[_Entry] = []
        for members in families.values():
            members.sort(key=_natural)
            groups = [members] if len(members) >= SHARD_MIN else [[t] for t in members]
            entries.extend(_Entry(g, self._importance(g)) for g in groups)
        return entries

    def _importance(self, tables: list[Table]) -> int:
        own = {(t.schema, t.name) for t in tables}
        referrers = {
            (r.schema, r.name)
            for t in tables
            for r, _ in self.referrers(t)
            if (r.schema, r.name) not in own
        }
        rows = sum(t.est_rows or 0 for t in tables)
        has_comment = any(t.comment for t in tables)
        return 3 * len(referrers) + 2 * has_comment + int(math.log10(rows + 1))

    def _definition(self, e: _Entry) -> str:
        """``shop.orders(id pk, customer_id→customers, status enum(paid|shipped), …) — 订单``"""
        t = e.first
        pk = set(t.primary_key)
        refs: dict[str, str] = {}
        for fk in t.foreign_keys:
            ref = fk.ref_table if fk.ref_schema == t.schema else fk.ref_qualified
            target = self._by_key.get((fk.ref_schema, fk.ref_table))
            # To the target's whole primary key: the table says enough.
            whole = (
                len(fk.columns) == 1
                and target is not None
                and target.primary_key == list(fk.ref_columns)
            )
            for col, ref_col in zip(fk.columns, fk.ref_columns, strict=False):
                refs.setdefault(col, ref if whole else f"{ref}.{ref_col}")
        parts = []
        for c in t.columns[:BRIEF_COLUMNS]:
            s = c.name
            if c.name in pk:
                s += " pk"
            if c.name in refs:
                s += "→" + refs[c.name]
            if c.name not in pk and c.name not in refs:
                s += " " + _brief_type(c)
            if c.comment:
                s += " — " + _line(c.comment, BRIEF_COMMENT_CHARS)
            parts.append(s)
        if len(t.columns) > BRIEF_COLUMNS:
            parts.append(f"… {len(t.columns) - BRIEF_COLUMNS} more")
        notes = [n for n in (_line(e.comment, COMMENT_CHARS), e.alike()) if n]
        if t.kind == "view":
            notes.insert(0, "view")
        return f"{t.qualified}({', '.join(parts)})" + (f" — {'; '.join(notes)}" if notes else "")

    def _name(self, e: _Entry) -> str:
        """``shop.refunds (退款)``"""
        notes = [n for n in (_line(e.comment, COMMENT_CHARS), e.alike()) if n]
        return e.first.qualified + (f" ({'; '.join(notes)})" if notes else "")


_NAMES_HEAD = "more tables (describe before querying): "
_ONLY_NAMES_HEAD = "tables (describe before querying): "
_SHARD = re.compile(r"^(.*?[A-Za-z_])\d+(?:_\d+)*$")
"""A name ending in a numeric suffix: group 1 is the stem (``events_`` of
``events_2025_01``)."""


def _tail(count: int, *, everything: bool) -> str:
    more = "" if everything else " more"
    return f"{count:,}{more} tables — find them with search_schema"


def _line_bytes(s: str) -> int:
    return len(s.encode()) + 3  # with the newline and indent the prompt adds


def _item_bytes(s: str) -> int:
    return len(s.encode()) + 2  # with the ", " separator


def _digitless(text: str) -> str:
    return re.sub(r"\d+", "#", text)


def _natural(t: Table) -> list[Any]:
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", t.name)]


_TYPE_ABBREV = [
    (re.compile(r"^timestamp(\(\d+\))? with time zone$"), r"timestamptz\1"),
    (re.compile(r"^timestamp(\(\d+\))? without time zone$"), r"timestamp\1"),
    (re.compile(r"^time(\(\d+\))? with time zone$"), r"timetz\1"),
    (re.compile(r"^time(\(\d+\))? without time zone$"), r"time\1"),
    (re.compile(r"^character varying"), "varchar"),
    (re.compile(r"^character\b"), "char"),
    (re.compile(r"^double precision$"), "float8"),
    (re.compile(r"^integer$"), "int"),
    (re.compile(r"^boolean$"), "bool"),
]


def _brief_type(c: Column) -> str:
    if c.enum:
        labels = [_line(v, 20) for v in c.enum[:ENUM_LABELS]]
        more = f"|…{len(c.enum) - ENUM_LABELS} more" if len(c.enum) > ENUM_LABELS else ""
        return f"enum({'|'.join(labels)}{more})"
    for pattern, short in _TYPE_ABBREV:
        typ, hits = pattern.subn(short, c.type)
        if hits:
            return typ
    return c.type


async def load_catalog(db: Database) -> Catalog:
    """Every table and column the account can read, in the configured schemas,
    with primary and foreign keys and enum labels."""
    dialect = db.dialect
    schemas = db.default_schemas()
    filt = f" AND {dialect.schema_column} IN :schemas" if schemas else ""
    params: dict[str, Any] = {"system": list(dialect.system_schemas)}
    if schemas:
        params["schemas"] = list(schemas)

    async def rows(sql: str) -> Sequence[Any]:
        return await db.fetch(
            sql.replace("{filter}", filt),
            params,
            expanding=params.keys(),
            timeout_s=CATALOG_TIMEOUT_S,
        )

    by_key: dict[tuple[str, str], Table] = {}
    for schema, name, kind, comment, est in await rows(dialect.tables_sql):
        if _excluded(db, schema, name):
            continue
        comment = "" if comment in (None, "VIEW") else str(comment)
        est_rows = int(est) if est is not None and int(est) >= 0 else None
        by_key[(schema, name)] = Table(schema, name, kind, comment, est_rows)
    for schema, name, col, typ, nullable, comment, labels in await rows(dialect.columns_sql):
        t = by_key.get((schema, name))
        if t is not None:
            typ = str(typ)
            enum = [str(v) for v in labels] if labels else _mysql_enum(typ)
            t.columns.append(Column(col, typ, bool(nullable), str(comment or ""), enum))
    try:
        keys = await rows(dialect.keys_sql)
    except Exception as exc:  # noqa: BLE001 - keys help; tables and columns are what's needed
        logger.warning("%s: could not read the keys", db.name, exc_info=exc)
        keys = []
    fks: dict[tuple[str, str, str], tuple[list[str], str, str, list[str]]] = {}
    for schema, name, con, kind, col, ref_schema, ref_table, ref_col in keys:
        t = by_key.get((schema, name))
        if t is None:
            continue
        if kind == "p":
            t.primary_key.append(col)
        else:
            cols, _, _, ref_cols = fks.setdefault(
                (schema, name, con), ([], ref_schema, ref_table, [])
            )
            cols.append(col)
            ref_cols.append(ref_col)
    for (schema, name, _), (cols, ref_schema, ref_table, ref_cols) in fks.items():
        by_key[(schema, name)].foreign_keys.append(
            ForeignKey(tuple(cols), ref_schema, ref_table, tuple(ref_cols))
        )
    return Catalog(list(by_key.values()))


def _mysql_enum(typ: str) -> list[str]:
    """The labels of a type spelled ``enum('a','b''c')`` (MySQL's), for a
    catalog that lists none of its own."""
    if not typ.lower().startswith("enum("):
        return []
    return [v.replace("''", "'") for v in re.findall(r"'((?:[^']|'')*)'", typ)]


async def describe(db: Database, cat: Catalog, table: Table, *, full: bool = True) -> str:
    """Columns, keys, indexes, common values and a few rows of one table, as text.

    Never fails: columns and keys are known already, and what can't be read
    is left out or noted. Without ``full`` (out of time), only what's known.
    """

    def indexes(sync_conn: Any) -> Any:
        try:
            return inspect(sync_conn).get_indexes(table.name, schema=table.schema)
        except Exception:  # noqa: BLE001 - views and odd objects have none
            return None

    lines = [f"## {table.heading()}", "columns:"]
    for c in table.columns:
        null = "" if c.nullable else " not null"
        labels = ""
        if c.enum and not c.type.lower().startswith("enum("):
            labels = f" (enum: {' | '.join(c.enum)})"
        note = f" — {c.comment}" if c.comment else ""
        lines.append(f"- {c.name} {c.type}{null}{labels}{note}")
    if table.primary_key:
        lines.append(f"primary key: ({', '.join(table.primary_key)})")
    for fk in table.foreign_keys:
        lines.append(
            f"foreign key: ({', '.join(fk.columns)}) → "
            f"{fk.ref_qualified}({', '.join(fk.ref_columns)})"
        )
    for r, fk in cat.referrers(table):
        lines.append(f"referenced by: {r.qualified}({', '.join(fk.columns)})")
    if not full:
        lines.append(
            "indexes, common values and sample rows: skipped "
            "(out of time; describe it again on its own)"
        )
        return "\n".join(lines)

    try:
        found = await db.reflect(indexes, statements=INDEX_STATEMENTS)
    except Exception:  # noqa: BLE001 - unreachable now; the sample will say so
        found = None
    for ix in found or []:
        cols = ", ".join(c for c in ix.get("column_names") or [] if c)
        if cols:
            lines.append(f"index: {ix.get('name')} ({cols}){' unique' if ix.get('unique') else ''}")

    common = await _common_values(db, table)
    if common:
        lines.append("common values (from the planner's statistics; share of rows):")
        lines.extend(common)

    try:
        # LIMIT, not just fetching fewer: both drivers read a whole result.
        rows = await db.run(
            f"SELECT * FROM {db.quote(table.schema, table.name)} LIMIT {SAMPLE_ROWS}",
            max_rows=SAMPLE_ROWS,
            checked=False,
            timeout_s=SAMPLE_TIMEOUT_S,
        )
    except ToolError as exc:
        lines.append(f"sample rows: unavailable ({exc.args[0]})")
    else:
        if rows.rows:
            lines.append("sample rows:")
            lines.append("\t".join(rows.columns))
            lines.extend("\t".join(cell_text(v, SAMPLE_CELL_CHARS) for v in r) for r in rows.rows)
    return "\n".join(lines)


async def _common_values(db: Database, table: Table) -> list[str]:
    """``- status: shipped 60%, paid 20%, cancelled 20%`` for each low-cardinality
    text, enum or boolean column, from statistics the database already keeps
    (PostgreSQL's pg_stats): no table is scanned. A dialect without such
    statistics gets nothing — the sample and a GROUP BY serve."""
    sql = db.dialect.common_values_sql
    names = [c.name for c in table.columns if c.enum or _CATEGORICAL.match(c.type)]
    if sql is None or not names:
        return []
    try:
        stats = await db.fetch(
            sql,
            {"schema": table.schema, "table": table.name, "columns": names},
            expanding=["columns"],
            timeout_s=SAMPLE_TIMEOUT_S,
        )
    except Exception:  # noqa: BLE001 - statistics are a nicety
        return []
    out = []
    for name, values, freqs, n_distinct in stats:
        # n_distinct < 0 is a share of the rows; 0 is unknown.
        distinct = n_distinct if n_distinct > 0 else -n_distinct * (table.est_rows or 0)
        if not values or not 0 < distinct <= COMMON_VALUES:
            continue
        pairs = zip(values[:COMMON_VALUES], freqs or [], strict=False)
        shown = ", ".join(f"{_line(str(v), 40)} {f:.0%}" for v, f in pairs)
        out.append(f"- {name}: {shown}")
    return out


_CATEGORICAL = re.compile(r"^(text|citext|character|char|varchar|bpchar|boolean|bool)\b", re.I)


def _excluded(db: Database, schema: str, name: str) -> bool:
    return any(
        fnmatch.fnmatchcase(name, p) or fnmatch.fnmatchcase(f"{schema}.{name}", p)
        for p in db.config.exclude_tables
    )


def _line(s: str, n: int) -> str:
    """``s`` on one line, at most ``n`` characters: a comment or label can't
    break the overview's line structure."""
    return clip(" ".join(s.split()), n)
