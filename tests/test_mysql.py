"""Against a real MySQL 8 loaded with examples/demo_mysql.sql.

export DB_AGENT_TEST_MYSQL_URL=mysql+asyncmy://agent_ro:agent_ro@127.0.0.1:3306/demo_shop
pytest tests/test_mysql.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from lovia import Runner, ToolError
from lovia.testing import ScriptedProvider, call, text
from lovia.transcript import ToolResultEntry

from db_agent.app import build_agent
from db_agent.config import Connection, Limits, Settings
from db_agent.execute import Database
from db_agent.plugin import SQLDatabase
from db_agent.results import ResultCache

URL_ENV = "DB_AGENT_TEST_MYSQL_URL"
pytestmark = pytest.mark.skipif(not os.environ.get(URL_ENV), reason=f"set {URL_ENV}")


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        connections={"shop": Connection(url_env=URL_ENV, timezone="+08:00")}, data_dir=tmp_path
    )


async def _run(settings: Settings, *calls: Any) -> tuple[list[ToolResultEntry], ScriptedProvider]:
    provider = ScriptedProvider([*calls, text("done")])
    cache = ResultCache(settings.data_dir / "results.db", ttl_days=1, max_mb=50)
    db = SQLDatabase(settings, cache)
    try:
        result = await Runner.run(build_agent(settings, db, model=provider), "question")
    finally:
        await db.aclose()
    return [e for e in result.entries if isinstance(e, ToolResultEntry)], provider


async def test_catalog_search_and_describe(tmp_path: Path) -> None:
    (search, describe), provider = await _run(
        _settings(tmp_path),
        call("search_schema", {"query": "退款"}),
        call("describe_tables", {"tables": ["orders"]}),
    )
    system = provider.calls[0][0].content
    assert "- shop (MySQL 8" in system and "demo_shop.orders (订单)" in system
    assert search.output.startswith("demo_shop.refunds (table")
    assert "primary key: (id)" in describe.output and "sample rows:" in describe.output


async def test_tables_and_a_pivoted_chart(tmp_path: Path) -> None:
    sql = (
        "SELECT DATE_FORMAT(created_at, '%Y-%m') AS month, status, COUNT(*) AS n "
        "FROM demo_shop.orders WHERE created_at < '2025-04-01' GROUP BY 1, 2 ORDER BY 1, 2"
    )
    (table, chart), _ = await _run(
        _settings(tmp_path),
        call(
            "query_table",
            {"sql": "SELECT id, status FROM demo_shop.orders ORDER BY id", "title": "t"},
        ),
        call(
            "query_chart",
            {
                "sql": sql,
                "option": {"xAxis": {"type": "category"}, "yAxis": {}, "series": [{"type": "bar"}]},
                "pivot": {"index": "month", "columns": "status", "values": "n"},
            },
        ),
    )
    env = json.loads(table.output)["lovia_ui"]
    assert env["kind"] == "db.table" and env["props"]["row_count"] == 6000
    props = json.loads(chart.output)["lovia_ui"]["props"]
    assert props["dataset"]["source"][0] == ["month", "cancelled", "paid", "shipped"]


async def test_the_session_is_read_only_and_literals_pass_through() -> None:
    db = Database("shop", Connection(url_env=URL_ENV), Limits(statement_timeout_s=1))
    try:
        with pytest.raises(ToolError) as exc:
            await db.run("DELETE FROM demo_shop.refunds", max_rows=1, checked=False)
        assert "read-only" in (exc.value.hint or "") or "can't read" in (exc.value.hint or "")
        res = await db.run("SELECT '12:30' AS t, 'a%b' AS p, 'x' LIKE '%x%' AS l", max_rows=1)
        assert res.rows == [["12:30", "a%b", 1]]
        with pytest.raises(ToolError) as exc:
            await db.run(
                "SELECT COUNT(*) FROM demo_shop.order_items a, demo_shop.order_items b, "
                "demo_shop.order_items c",
                max_rows=1,
            )
        assert "took too long" in (exc.value.hint or "")
    finally:
        await db.aclose()
