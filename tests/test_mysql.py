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

URL_ENV = "DB_AGENT_TEST_MYSQL_URL"
pytestmark = pytest.mark.skipif(not os.environ.get(URL_ENV), reason=f"set {URL_ENV}")


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        connections={"shop": Connection(url_env=URL_ENV, timezone="+08:00")}, data_dir=tmp_path
    )


async def _run(settings: Settings, *calls: Any) -> tuple[list[ToolResultEntry], ScriptedProvider]:
    provider = ScriptedProvider([*calls, text("done")])
    db = SQLDatabase(settings)
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
    # Outside a lovia web run there is no chat to keep the rows in: the
    # envelope is just the preview.
    assert env["kind"] == "table" and env["props"]["row_count"] == 6000 and "data" not in env
    props = json.loads(chart.output)["lovia_ui"]["props"]
    header = props["dataset"]["source"][0]
    # Categories come in the order they first appear in the rows.
    assert header[0] == "month" and sorted(header[1:]) == ["cancelled", "paid", "shipped"]
    assert [s["name"] for s in props["series"]] == header[1:]


async def test_the_session_is_read_only_and_literals_pass_through() -> None:
    db = Database("shop", Connection(url_env=URL_ENV), Limits(statement_timeout_s=1))
    try:
        with pytest.raises(ToolError) as exc:
            await db.run("DELETE FROM demo_shop.refunds", max_rows=1, checked=False)
        # Refused by the grants (1142) or the read-only session (1792): either
        # way the hint tells the model this database only reads.
        assert "read" in (exc.value.hint or "") and "fix the SQL" not in (exc.value.hint or "")
        res = await db.run("SELECT '12:30' AS t, 'a%b' AS p, 'x' LIKE '%x%' AS l", max_rows=1)
        assert res.rows == [["12:30", "a%b", 1]]
        # init_command set the session up.
        res = await db.run(
            "SELECT @@SESSION.transaction_read_only, @@SESSION.max_execution_time", max_rows=1
        )
        assert res.rows == [[1, 1000]]
        # A pairwise comparison over 12,000 × 12,000 rows: no shortcut, well over 1 s.
        with pytest.raises(ToolError) as exc:
            await db.run(
                "SELECT SUM(a.quantity * b.quantity) FROM demo_shop.order_items a "
                "JOIN demo_shop.order_items b ON a.unit_price > b.unit_price",
                max_rows=1,
            )
        assert "took too long" in (exc.value.hint or "")
    finally:
        await db.aclose()


async def test_our_own_time_limits_stay_with_their_statement() -> None:
    db = Database("shop", Connection(url_env=URL_ENV), Limits(statement_timeout_s=30))
    try:
        # The optimizer hint cuts this one statement short...
        with pytest.raises(ToolError) as exc:
            await db.run(
                "SELECT SUM(a.quantity * b.quantity) FROM demo_shop.order_items a "
                "JOIN demo_shop.order_items b ON a.unit_price > b.unit_price",
                max_rows=1,
                checked=False,
                timeout_s=0.05,
            )
        assert "took too long" in (exc.value.hint or "")
        # ...and leaves the session's limit alone.
        res = await db.run("SELECT @@SESSION.max_execution_time", max_rows=1)
        assert res.rows == [[30000]]
        res = await db.run("SHOW VARIABLES LIKE 'max_execution_time'", max_rows=5)
        assert res.rows == [["max_execution_time", "30000"]]
    finally:
        await db.aclose()
