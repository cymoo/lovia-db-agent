"""`SQLDatabase`: the lovia plugin that lets an agent query your databases.

It contributes five tools and a system-prompt section listing the databases
and their schemas — as much as a budget allows, most important first. Two of
the tools explore, for the model only; two answer, for the user — a table or
a chart drawn from the query's rows, so no number passes through the model's
output. A big result is kept with the chat (lovia's ``put_ui_data``) for the
page to draw; the model sees a preview and per-column stats.

Pools and catalogs live on the plugin object, created on first use: lovia
calls ``setup()`` once per run, and they must outlive runs.

Every wait inside a tool has a bound tighter than the tool's own timeout, so
the model gets an error it can act on — lovia's timeout is only a backstop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Annotated, Any

from lovia import PluginInstance, RunContext, ToolError, tool
from lovia.tools.base import Tool
from lovia.web import put_ui_data, ui_result, validate_chart_option

from .catalog import (
    CATALOG_TIMEOUT_S,
    INDEX_STATEMENTS,
    SAMPLE_TIMEOUT_S,
    Catalog,
    describe,
    load_catalog,
)
from .charts import apply_split, check_encode, summarize
from .config import Settings
from .convert import cell_text
from .execute import CLEANUP_S, Database, client_wait
from .stats import column_stats

__all__ = ["SQLDatabase"]

logger = logging.getLogger(__name__)

# Far below lovia's default max_tool_output_chars (200K): a truncated UI
# envelope is broken JSON, so the tools size their own output.
OUTPUT_CHARS = 60_000
QUERY_OUTPUT_CHARS = 20_000
QUERY_CELL_CHARS = 200
CATALOG_TTL_S = 600
"""After this a catalog is reloaded in the background; tools keep using the old one."""
DOWN_RETRY_S = 30
"""A database that failed to load isn't tried again for this long."""
MAX_DESCRIBE = 10
DESCRIBE_BUDGET_S = 30
"""Time for describe_tables' indexes, common values and sample rows, all
tables together; columns and keys come from the catalog."""
# Module-level: the tools are closures, and lovia resolves their (string)
# annotations against this module's globals.
DB_ARG = "The database's name; omit when there is only one."

_LEGEND = (
    "In table(…) definitions, `pk` marks the primary key, `a→t` a foreign key to t's "
    "primary key (`a→t.c` to column c), enum(…) the allowed values; key columns omit their type."
)

_WORKFLOW = """\
How to work — aim for a correct answer in as few steps as the question needs:
- Tables listed with their columns are ready to query: write the SQL straight away. For any \
other table, or a definition cut short (…), find it with search_schema and read it with \
describe_tables (keys, indexes, common values, sample rows). Use names exactly as listed, \
qualified with their schema; when unsure of a name or a value's spelling, look it up rather \
than guess.
- Explore with `query` — only you see its rows. Answer with `query_table` (a table for the \
user) or `query_chart` (a chart for the user). Use show_table or show_chart only for data \
no single query returns, and show_html only for what a table or chart can't do (a what-if \
calculator, a custom visual), with data from your queries, never made up.
- Relative dates ("this month", "last week") count from today's date, stated in this prompt. \
If such a query comes back empty, check where the data ends (MAX of the date column) before \
saying there is none, and tell the user.
- Do the math in SQL — totals, rankings, growth; round averages and ratios there too. For big \
results you see only a preview and per-column stats; the user sees every row.
- Only read-only statements run: SELECT, WITH … SELECT, EXPLAIN, SHOW.
- The table or chart is the answer: don't repeat its numbers in text. Say what stands out, \
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
        self._down_until: dict[str, float] = {}
        self._refreshing: dict[str, asyncio.Task[None]] = {}
        self._instructions: tuple[tuple[Any, ...], str] | None = None
        self._tools = self._build_tools()

    async def setup(self, ctx: RunContext[Any]) -> PluginInstance:
        return PluginInstance(tools=list(self._tools), instructions=await self._prompt())

    async def _prompt(self) -> str:
        """The system-prompt section, rebuilt only when a schema changes.

        Byte-stable otherwise — row estimates drift, the text doesn't — so
        the provider's prompt cache stays warm. A database that was down is
        tried again on the next run.
        """
        catalogs: dict[str, Catalog | None] = {}
        for db in self.databases.values():
            try:
                catalogs[db.name] = await self.catalog(db)
            except ToolError:  # a down database mustn't break the run
                catalogs[db.name] = None
        key = tuple((name, cat and cat.fingerprint) for name, cat in catalogs.items())
        if self._instructions is None or self._instructions[0] != key:
            self._instructions = (key, self._build_instructions(catalogs))
        return self._instructions[1]

    async def aclose(self) -> None:
        tasks = list(self._refreshing.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for db in self.databases.values():
            await db.aclose()

    # ---- catalogs -------------------------------------------------------

    async def catalog(self, db: Database) -> Catalog:
        """The database's catalog: loaded once, then refreshed in the background.

        Only the first load waits; a stale catalog is served while the next
        one loads. Raise :class:`ToolError` if the database can't be read.
        """
        cached = self._catalogs.get(db.name)
        if cached is None:
            async with self._locks[db.name]:
                cached = self._catalogs.get(db.name) or await self._load(db)
        elif time.monotonic() - cached.loaded_at >= CATALOG_TTL_S:
            self._refresh(db, cached)
        return cached

    async def _load(self, db: Database) -> Catalog:
        if time.monotonic() < self._down_until.get(db.name, 0):
            raise ToolError(f"{db.name} is unavailable right now", hint=_DOWN_HINT)
        try:
            if not db.version:
                await db.load_version()
            cat = self._catalogs[db.name] = await load_catalog(db)
        except Exception as exc:  # noqa: BLE001 - any failure: the database is down for us
            self._down_until[db.name] = time.monotonic() + DOWN_RETRY_S
            logger.warning("%s: could not load the catalog", db.name, exc_info=exc)
            raise ToolError(
                f"{db.name} is unavailable right now ({type(exc).__name__})", hint=_DOWN_HINT
            ) from None
        return cat

    def _refresh(self, db: Database, stale: Catalog) -> None:
        if db.name in self._refreshing:
            return

        async def reload() -> None:
            try:
                async with self._locks[db.name]:
                    self._catalogs[db.name] = await load_catalog(db)
            except Exception as exc:  # noqa: BLE001 - keep the stale one, try again later
                stale.loaded_at = time.monotonic() - CATALOG_TTL_S + DOWN_RETRY_S
                logger.warning("%s: could not refresh the catalog", db.name, exc_info=exc)

        task = self._refreshing[db.name] = asyncio.create_task(reload())
        task.add_done_callback(lambda _: self._refreshing.pop(db.name, None))

    def _build_instructions(self, catalogs: dict[str, Catalog | None]) -> str:
        budget = self.limits.catalog_budget_tokens * 4 // len(self.databases)
        lines = [
            "## Databases",
            "You answer questions from these read-only databases. Pass a database's "
            "name as `database`" + (" (or omit it: there is only one)." if len(self) == 1 else "."),
            _LEGEND,
            "",
        ]
        for db in self.databases.values():
            cat = catalogs[db.name]
            if cat is None:
                lines.append(f"- {db.name}: unavailable right now")
                continue
            facts = [db.label]
            if db.config.timezone:
                facts.append(f"time zone {db.config.timezone}")
            about = f": {db.config.description}" if db.config.description else ""
            lines.append(f"- {db.name} ({'; '.join(facts)}){about}")
            lines.extend(f"  {line}" for line in cat.overview(budget).splitlines())
        dialects = {db.dialect.name: db.dialect for db in self.databases.values()}
        notes = "; ".join(dialects[d].notes for d in sorted(dialects))
        lines += ["", _WORKFLOW, "Dialect notes: " + notes]
        return "\n".join(lines)

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
        # Backstops, each past the client-side limits of what the tool waits
        # for (each call, plus dropping its connection should it hang).
        stmt = limits.statement_timeout_s
        statement = client_wait(stmt) + CLEANUP_S
        # The first load: the version, then tables, columns and keys.
        catalog = statement + 3 * (client_wait(CATALOG_TIMEOUT_S) + CLEANUP_S)
        # Past the budget only the table being described runs on: its indexes
        # (reflection), its common values and its sample.
        last_table = (
            client_wait(stmt, INDEX_STATEMENTS) + 2 * client_wait(SAMPLE_TIMEOUT_S) + 3 * CLEANUP_S
        )
        margin = 5

        async def search_schema(
            query: Annotated[
                str,
                "Words to look for in table and column names and comments, "
                "e.g. 'refund amount'. '*' lists the largest tables.",
            ],
            database: Annotated[str | None, DB_ARG] = None,
            limit: Annotated[int, "How many tables to return (max 50)."] = 20,
        ) -> str:
            """Find tables by name, column or comment — for tables the system
            prompt doesn't define."""
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
            """Columns (with comments), keys, indexes, common values and sample rows of tables."""
            db = self.database(database)
            if not tables:
                raise ToolError("no tables given", hint="pass names from search_schema")
            cat = await self.catalog(db)
            parts: list[str] = []
            missing: list[str] = []
            deadline = time.monotonic() + DESCRIBE_BUDGET_S
            for ref in tables[:MAX_DESCRIBE]:
                found = cat.find(ref)
                if len(found) == 1:
                    # Room for its common values and its sample at least.
                    full = deadline - time.monotonic() > 2 * SAMPLE_TIMEOUT_S
                    parts.append(await describe(db, cat, found[0], full=full))
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
            return _tsv(
                res.columns,
                res.rows,
                more=res.truncated,
                budget=QUERY_OUTPUT_CHARS,
                elapsed_ms=res.elapsed_ms,
            )

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
            split_by: Annotated[
                str | None,
                "A category column: one series per value, from ONE series template "
                "whose encode names x and y.",
            ] = None,
            database: Annotated[str | None, DB_ARG] = None,
        ) -> str:
            """Run a query and show the user the result as an ECharts chart.

            The rows become dataset.source (first row = column names). For
            `SELECT month, revenue …`:
            {"title": {"text": "Monthly revenue"}, "tooltip": {"trigger": "axis"},
             "xAxis": {"type": "category"}, "yAxis": {},
             "series": [{"type": "line", "encode": {"x": "month", "y": "revenue"}}]}
            Several measures: one series per column, e.g. a second series with
            encode {"x": "month", "y": "orders"}.
            One line per category from `SELECT month, category, revenue …`
            (one row per month and category): split_by="category" with
            "series": [{"type": "line", "encode": {"x": "month", "y": "revenue"}}].
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
            if split_by:
                option, columns, rows = apply_split(
                    option, columns, rows, split_by, max_series=limits.chart_max_series
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
            tool(search_schema, timeout=catalog + margin),
            tool(describe_tables, timeout=catalog + DESCRIBE_BUDGET_S + last_table + margin),
            tool(query, timeout=statement + margin),
            tool(query_table, timeout=statement + margin),
            tool(query_chart, timeout=statement + margin),
        ]


_DOWN_HINT = "retry in a minute; if it keeps failing, tell the user the database is down"


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


def _tsv(
    columns: list[str], rows: list[list[Any]], *, more: bool, budget: int, elapsed_ms: int
) -> str:
    lines = ["\t".join(columns)]
    used = len(lines[0])
    shown = 0
    for r in rows:
        line = "\t".join(cell_text(v, QUERY_CELL_CHARS) for v in r)
        if used + len(line) + 1 > budget:
            break
        lines.append(line)
        used += len(line) + 1
        shown += 1
    took = _duration(elapsed_ms)
    if shown < len(rows) or more:
        lines.append(f"(first {shown} rows, {took}; there are more — filter or aggregate in SQL)")
    else:
        lines.append(f"({shown} row{'s' if shown != 1 else ''}, {took})")
    return "\n".join(lines)


def _duration(ms: int) -> str:
    """``12 ms``, ``3.2s``: how long the statement ran — a slow one is worth
    narrowing before it's run again for the user."""
    return f"{ms} ms" if ms < 1000 else f"{ms / 1000:.1f}s"


def _fit_text(text: str, budget: int) -> str:
    return text if len(text) <= budget else text[: budget - 40] + "\n… (cut: output too long)"
