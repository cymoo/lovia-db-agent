"""`SQLDatabase`: the lovia plugin that lets an agent query your databases.

It contributes five tools and a system-prompt section listing the databases
and (within a budget) their tables. Two of the tools explore, for the model
only; two answer, for the user — a table or a chart drawn from the query's
rows, so no number passes through the model's output. A big result is kept
with the chat (lovia's ``put_ui_data``) for the page to draw; the model sees
a preview and per-column stats.

Pools and catalogs live on the plugin object, created on first use: lovia
calls ``setup()`` once per run, and they must outlive runs.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Annotated, Any

from lovia import PluginInstance, ToolError, tool
from lovia.tools.base import Tool
from lovia.web import put_ui_data, ui_result, validate_chart_option

from .catalog import Catalog, describe, load_catalog
from .charts import apply_pivot, check_encode, summarize
from .config import Settings
from .execute import Database
from .stats import column_stats

__all__ = ["SQLDatabase"]

# Far below lovia's default max_tool_output_chars (200K): a truncated UI
# envelope is broken JSON, so the tools size their own output.
OUTPUT_CHARS = 60_000
QUERY_OUTPUT_CHARS = 20_000
CATALOG_TTL_S = 600
MAX_DESCRIBE = 10
# Module-level: the tools are closures, and lovia resolves their (string)
# annotations against this module's globals.
DB_ARG = "The database's name; omit when there is only one."

_DIALECT_NOTES = {
    "postgres": "PostgreSQL — date_trunc('month', t), to_char(t, 'YYYY-MM'), ILIKE, "
    '"double quotes" for identifiers, x::numeric casts',
    "mysql": "MySQL — DATE_FORMAT(t, '%Y-%m'), DATE(t), `backticks` for identifiers, "
    "LIMIT n OFFSET m",
}

_WORKFLOW = """\
How to work:
1. Find tables with search_schema and read them with describe_tables before writing SQL. \
Never guess table or column names; qualify tables with their schema, as listed.
2. Explore with `query` — only you see its rows. Answer with `query_table` (a table for \
the user) or `query_chart` (a chart for the user). Use show_chart only for data no single \
query returns.
3. For relative dates ("this month", "last week"), check today's date first.
4. Do the math in SQL — totals, rankings, growth. For big results you see only a preview \
and per-column stats; the user sees every row.
5. Only read-only statements run: SELECT, WITH … SELECT, EXPLAIN, SHOW.
6. The table or chart is the answer: don't repeat its numbers in text. Say what stands out, \
and how you computed it (filters, definitions)."""


class SQLDatabase:
    """Query the configured databases: schema discovery, SQL, tables and charts."""

    name = "sql_database"

    def __init__(self, settings: Settings) -> None:
        self.limits = settings.limits
        self.databases = {
            name: Database(name, conn, settings.limits)
            for name, conn in settings.connections.items()
        }
        self._catalogs: dict[str, Catalog] = {}
        self._locks = {name: asyncio.Lock() for name in self.databases}
        self._instructions: str | None = None
        self._tools = self._build_tools()

    async def setup(self) -> PluginInstance:
        instructions = self._instructions
        if instructions is None:
            instructions, complete = await self._build_instructions()
            # Cached once every catalog loaded: a byte-stable system prompt keeps
            # the provider's prompt cache warm. A database that was down is
            # retried on the next run.
            if complete:
                self._instructions = instructions
        return PluginInstance(tools=list(self._tools), instructions=instructions)

    async def aclose(self) -> None:
        for db in self.databases.values():
            await db.aclose()

    # ---- catalogs -------------------------------------------------------

    async def catalog(self, db: Database) -> Catalog:
        cached = self._catalogs.get(db.name)
        if cached is not None and time.monotonic() - cached.loaded_at < CATALOG_TTL_S:
            return cached
        async with self._locks[db.name]:
            cached = self._catalogs.get(db.name)
            if cached is None or time.monotonic() - cached.loaded_at >= CATALOG_TTL_S:
                if not db.version:
                    await db.load_version()
                cached = self._catalogs[db.name] = await load_catalog(db)
            return cached

    async def _build_instructions(self) -> tuple[str, bool]:
        budget = self.limits.catalog_budget_tokens * 4 // len(self.databases)
        lines = [
            "## Databases",
            "You answer questions from these read-only databases. Pass a database's "
            "name as `database`" + (" (or omit it: there is only one)." if len(self) == 1 else "."),
            "",
        ]
        complete = True
        for db in self.databases.values():
            try:
                cat = await self.catalog(db)
            except Exception as exc:  # noqa: BLE001 - a down database mustn't break the run
                complete = False
                lines.append(f"- {db.name}: unavailable right now ({type(exc).__name__})")
                continue
            facts = [db.label]
            if db.config.timezone:
                facts.append(f"time zone {db.config.timezone}")
            about = f": {db.config.description}" if db.config.description else ""
            lines.append(f"- {db.name} ({'; '.join(facts)}){about}")
            lines.append(f"  {cat.overview(budget)}")
        dialects = sorted({db.dialect for db in self.databases.values()})
        lines += ["", _WORKFLOW, "Dialect notes: " + "; ".join(_DIALECT_NOTES[d] for d in dialects)]
        return "\n".join(lines), complete

    def __len__(self) -> int:
        return len(self.databases)

    def database(self, name: str | None) -> Database:
        if name is None or not name.strip():
            if len(self.databases) == 1:
                return next(iter(self.databases.values()))
            raise ToolError(
                "which database?",
                hint="pass database: one of " + ", ".join(self.databases),
            )
        db = self.databases.get(name.strip())
        if db is None:
            raise ToolError(
                f"no database named {name!r}",
                hint="use one of: " + ", ".join(self.databases),
            )
        return db

    # ---- tools ----------------------------------------------------------

    def _build_tools(self) -> list[Tool]:
        limits = self.limits
        timeout = limits.statement_timeout_s + 15

        async def search_schema(
            query: Annotated[
                str,
                "Words to look for in table and column names and comments, "
                "e.g. 'refund amount'. '*' lists the largest tables.",
            ],
            database: Annotated[str | None, DB_ARG] = None,
            limit: Annotated[int, "How many tables to return (max 50)."] = 20,
        ) -> str:
            """Find tables by name, column or comment. Use it before writing SQL."""
            db = self.database(database)
            hits = (await self.catalog(db)).search(query, max(1, min(limit, 50)))
            if not hits:
                return f"No table in {db.name} matches {query!r}. Try other words, or '*'."
            out = []
            for table, cols in hits:
                out.append(table.heading())
                for c in cols[:8]:
                    note = f" — {c.comment}" if c.comment else ""
                    out.append(f"    {c.name} {c.type}{note}")
            return "\n".join(out)

        async def describe_tables(
            tables: Annotated[
                list[str], "Table names, as listed ('schema.table'); up to 10 at once."
            ],
            database: Annotated[str | None, DB_ARG] = None,
        ) -> str:
            """Columns (with comments), keys, indexes and sample rows of tables."""
            db = self.database(database)
            if not tables:
                raise ToolError("no tables given", hint="pass names from search_schema")
            cat = await self.catalog(db)
            parts: list[str] = []
            missing: list[str] = []
            for ref in tables[:MAX_DESCRIBE]:
                found = cat.find(ref)
                if len(found) == 1:
                    parts.append(await describe(db, found[0]))
                elif found:
                    names = ", ".join(t.qualified for t in found)
                    parts.append(f"## {ref}: ambiguous — use one of {names}")
                else:
                    guesses = [t.qualified for t, _ in cat.search(ref.split(".")[-1], 3)]
                    missing.append(
                        f"{ref}" + (f" (similar: {', '.join(guesses)})" if guesses else "")
                    )
            if missing and not parts:
                raise ToolError(
                    "no such table: " + "; ".join(missing),
                    hint="find the right name with search_schema",
                )
            if missing:
                parts.append("Not found: " + "; ".join(missing))
            if len(tables) > MAX_DESCRIBE:
                parts.append(f"(Described the first {MAX_DESCRIBE}; ask for the rest separately.)")
            return _fit_text("\n\n".join(parts), OUTPUT_CHARS)

        async def query(
            sql: Annotated[str, "One read-only statement in the database's dialect."],
            database: Annotated[str | None, DB_ARG] = None,
        ) -> str:
            """Run a query and see its rows yourself — to explore values, check a
            join, or confirm a definition. The user doesn't see the result; answer
            with query_table or query_chart."""
            db = self.database(database)
            res = await db.run(sql, max_rows=limits.query_rows)
            if not res.columns:
                return "The statement returned no result set."
            return _tsv(res.columns, res.rows, more=res.truncated, budget=QUERY_OUTPUT_CHARS)

        async def query_table(
            sql: Annotated[str, "One read-only statement in the database's dialect."],
            title: Annotated[str, "A short heading for the table, in the user's language."],
            database: Annotated[str | None, DB_ARG] = None,
        ) -> str:
            """Run a query and show the user the result as an interactive table
            (sortable, filterable, CSV download). Select only the columns the user
            needs, with readable aliases, and ORDER BY what matters. For a big result
            you see the first rows and per-column stats; the user sees all of them."""
            db = self.database(database)
            res = await db.run(sql, max_rows=limits.cache_rows)
            props: dict[str, Any] = {
                "title": title.strip(),
                "columns": res.columns,
                "rows": res.rows,
                "row_count": len(res.rows),
            }
            if res.truncated:
                props["truncated"] = True
            if len(res.rows) <= limits.preview_rows and _size(props) <= OUTPUT_CHARS:
                return ui_result("table", props)
            ref = await put_ui_data(props)
            n = len(res.rows)
            props.update(
                stats=column_stats(res.columns, res.rows),
                note=(
                    f"The user sees all {n:,}"
                    + (" (the cap; there were more)" if res.truncated else "")
                    + " rows; you see the first {shown}. Use SQL for totals and rankings."
                ),
            )
            return ui_result("table", _fit_preview(props, limits.preview_rows), data=ref)

        async def query_chart(
            sql: Annotated[str, "One read-only statement; aggregate in SQL."],
            option: Annotated[
                dict[str, Any],
                "An ECharts option WITHOUT data or dataset; series map result "
                "columns by name via encode.",
            ],
            pivot: Annotated[
                dict[str, str] | None,
                "Long → wide: {index, columns, values}; then give ONE series as a "
                "template, repeated per category in the order categories first "
                "appear in the rows.",
            ] = None,
            database: Annotated[str | None, DB_ARG] = None,
        ) -> str:
            """Run a query and show the user the result as an ECharts chart.

            The rows become dataset.source (first row = column names). For
            `SELECT month, revenue …`:
            {"title": {"text": "Monthly revenue"}, "tooltip": {"trigger": "axis"},
             "xAxis": {"type": "category"}, "yAxis": {},
             "series": [{"type": "line", "encode": {"x": "month", "y": "revenue"}}]}
            One line per category from `SELECT month, category, revenue …`: pass
            pivot={"index": "month", "columns": "category", "values": "revenue"}
            and "series": [{"type": "line"}].
            Pie: "series": [{"type": "pie", "encode": {"itemName": "category", "value": "revenue"}}]
            Bar ranking: "yAxis": {"type": "category"}, "xAxis": {}, encode {"y": name, "x": value}.
            """
            db = self.database(database)
            if "dataset" in option:
                raise ToolError(
                    "don't pass a dataset: the query's rows are the data",
                    hint="remove option.dataset and map columns with series[].encode",
                )
            res = await db.run(sql, max_rows=limits.chart_rows)
            if res.truncated:
                raise ToolError(
                    f"the query returned more than {limits.chart_rows:,} rows — too many to chart",
                    hint="aggregate in SQL: a coarser time bucket, or top N plus 'other'",
                )
            if not res.rows:
                raise ToolError(
                    "the query returned no rows", hint="check the filters, e.g. the date range"
                )
            columns, rows = res.columns, res.rows
            if pivot:
                option, columns, rows = apply_pivot(
                    option, columns, rows, pivot, max_series=limits.chart_max_series
                )
            check_encode(option, columns)
            full = {**option, "dataset": {"source": [columns, *rows]}}
            validate_chart_option(full)
            if _size([columns, *rows]) <= limits.chart_inline_bytes:
                return ui_result("chart", full)
            # The preview is the option without its data: a chart frame, should
            # the data be gone. The chart drops the keys meant for the model.
            preview = {
                **option,
                "summary": summarize(columns, rows),
                "note": f"The user sees the chart of all {len(rows):,} rows; you see a summary.",
            }
            return ui_result("chart", preview, data=await put_ui_data(full))

        return [
            tool(search_schema, timeout=timeout),
            tool(describe_tables, timeout=timeout * 2),
            tool(query, timeout=timeout),
            tool(query_table, timeout=timeout),
            tool(query_chart, timeout=timeout),
        ]


def _size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False))


def _fit_preview(props: dict[str, Any], preview_rows: int) -> dict[str, Any]:
    """Cut the preview rows until the envelope fits OUTPUT_CHARS."""
    rows = props["rows"]
    k = min(preview_rows, len(rows))
    while True:
        out = {**props, "rows": rows[:k], "note": props["note"].replace("{shown}", str(k))}
        if k <= 1 or _size(out) <= OUTPUT_CHARS:
            return out
        k //= 2


def _tsv(columns: list[str], rows: list[list[Any]], *, more: bool, budget: int) -> str:
    def cell(v: Any) -> str:
        s = (
            ""
            if v is None
            else json.dumps(v, ensure_ascii=False)
            if isinstance(v, (dict, list))
            else str(v)
        )
        s = s.replace("\t", " ").replace("\n", " ")
        return s if len(s) <= 200 else s[:199] + "…"

    lines = ["\t".join(columns)]
    used = len(lines[0])
    shown = 0
    for r in rows:
        line = "\t".join(cell(v) for v in r)
        if used + len(line) + 1 > budget:
            break
        lines.append(line)
        used += len(line) + 1
        shown += 1
    if shown < len(rows) or more:
        lines.append(f"(first {shown} rows; there are more — filter or aggregate in SQL)")
    else:
        lines.append(f"({shown} row{'s' if shown != 1 else ''})")
    return "\n".join(lines)


def _fit_text(text: str, budget: int) -> str:
    return text if len(text) <= budget else text[: budget - 40] + "\n… (cut: output too long)"
