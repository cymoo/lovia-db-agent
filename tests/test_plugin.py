"""The plugin without a database: a down database, the prompt's cache, output shapes."""

from __future__ import annotations

from pathlib import Path

import pytest
from lovia import Runner
from lovia.testing import ScriptedProvider, call, text
from lovia.transcript import ToolResultEntry

from db_agent.app import build_agent
from db_agent.catalog import Catalog, Column, Table
from db_agent.config import Connection, Settings
from db_agent.plugin import SQLDatabase, _tsv


@pytest.mark.parametrize(
    "url",
    ["postgresql+asyncpg://u:p@127.0.0.1:9/db", "mysql+asyncmy://u:p@127.0.0.1:9/db"],
    ids=["postgres", "mysql"],
)
async def test_a_down_database_is_reported_and_not_hammered(
    url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Nothing listens on port 9: the connection is refused at once.
    monkeypatch.setenv("DB_AGENT_DOWN_URL", url)
    settings = Settings(
        connections={"down": Connection(url_env="DB_AGENT_DOWN_URL")}, data_dir=tmp_path
    )
    provider = ScriptedProvider(
        [call("search_schema", {"query": "x"}), call("query", {"sql": "SELECT 1"}), text("done")]
    )
    db = SQLDatabase(settings)
    try:
        result = await Runner.run(build_agent(settings, db, model=provider), "question")
    finally:
        await db.aclose()
    assert "- down: unavailable right now" in provider.calls[0][0].content
    search, query = (e for e in result.entries if isinstance(e, ToolResultEntry))
    # The failed load at setup holds off retries: the search fails at once.
    assert search.is_error
    assert search.output.startswith("Tool error: down is unavailable right now\n")
    assert query.is_error and "down is unreachable or busy" in query.output
    assert "retry once" in query.output and "fix the SQL" not in query.output


async def test_the_prompt_is_rebuilt_only_when_the_schema_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DB_AGENT_DOWN_URL", "postgresql+asyncpg://u:p@127.0.0.1:9/db")
    settings = Settings(
        connections={"shop": Connection(url_env="DB_AGENT_DOWN_URL")}, data_dir=tmp_path
    )
    db = SQLDatabase(settings)
    tables = [Table("s", "orders", "table", "订单", 10, [Column("id", "bigint", False)], ["id"])]
    current = Catalog(tables)

    async def catalog(_db: object) -> Catalog:
        return current

    monkeypatch.setattr(db, "catalog", catalog)
    first = (await db.setup()).instructions
    assert "  s.orders(id pk) — 订单" in first
    # A reload with new row estimates: the very same text, for the prompt cache.
    tables[0].est_rows = 10_000
    current = Catalog(tables)
    assert (await db.setup()).instructions is first
    # A new table reaches the prompt.
    current = Catalog([*tables, Table("s", "refunds", "table", "退款", 1)])
    assert "s.refunds() — 退款" in (await db.setup()).instructions
    await db.aclose()


def test_query_output_says_how_long_it_took() -> None:
    assert _tsv(["n"], [[1]], more=False, budget=100, elapsed_ms=12).endswith("(1 row, 12 ms)")
    out = _tsv(["n"], [[1], [2]], more=True, budget=100, elapsed_ms=3210)
    assert out.endswith("(first 2 rows, 3.2s; there are more — filter or aggregate in SQL)")
