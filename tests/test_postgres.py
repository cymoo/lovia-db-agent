"""Against a real PostgreSQL loaded with examples/demo_postgres.sql.

    export DB_AGENT_TEST_PG_URL=postgresql+asyncpg://agent_ro:agent_ro@localhost/demo_shop
    pytest tests/test_postgres.py

The tools run through a lovia agent driven by a scripted model, so these
check what the model and the page would actually receive.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from lovia import Runner, ToolError
from lovia.testing import ScriptedProvider, call, text
from lovia.transcript import ToolResultEntry
from lovia.web.components import UI_DATA_SINK
from lovia.web.store import ChatStore

from db_agent.app import build_agent, build_app
from db_agent.config import Connection, Limits, Settings
from db_agent.execute import Database
from db_agent.plugin import SQLDatabase

URL_ENV = "DB_AGENT_TEST_PG_URL"
SID = "chat-1"
pytestmark = pytest.mark.skipif(not os.environ.get(URL_ENV), reason=f"set {URL_ENV}")


def _settings(tmp_path: Path, **limits: Any) -> Settings:
    return Settings(
        connections={
            "shop": Connection(url_env=URL_ENV, description="演示网店", timezone="Asia/Shanghai")
        },
        limits=Limits(**limits),
        data_dir=tmp_path,
    )


async def _run(
    settings: Settings, *calls: Any
) -> tuple[list[ToolResultEntry], ScriptedProvider, ChatStore]:
    provider = ScriptedProvider([*calls, text("done")])
    store = ChatStore.in_memory()
    await store.upsert(SID)
    db = SQLDatabase(settings)
    token = UI_DATA_SINK.set((store, SID))  # what lovia web sets for each run
    try:
        result = await Runner.run(build_agent(settings, db, model=provider), "question")
    finally:
        UI_DATA_SINK.reset(token)
        await db.aclose()
    return [e for e in result.entries if isinstance(e, ToolResultEntry)], provider, store


def _envelope(entry: ToolResultEntry) -> dict[str, Any]:
    assert not entry.is_error, entry.output
    return json.loads(entry.output)["lovia_ui"]


async def _stored(store: ChatStore, env: dict[str, Any]) -> dict[str, Any]:
    """The full props the page fetches for this envelope."""
    return json.loads(await store.get_ui_data(SID, env["data"]) or b"")


async def test_the_system_prompt_lists_the_tables(tmp_path: Path) -> None:
    _, provider, _ = await _run(_settings(tmp_path))
    system = provider.calls[0][0].content
    assert "- shop (PostgreSQL 18" in system or "- shop (PostgreSQL" in system
    assert "time zone Asia/Shanghai" in system and "演示网店" in system
    assert "shop.orders (订单)" in system and "shop.refunds (退款)" in system


async def test_search_and_describe(tmp_path: Path) -> None:
    (search, describe), _, _ = await _run(
        _settings(tmp_path),
        call("search_schema", {"query": "退款"}),
        call("describe_tables", {"tables": ["orders", "nope"]}),
    )
    assert search.output.startswith("shop.refunds (table")
    assert "amount numeric(10,2) — 退款金额（元）" in search.output
    assert "## shop.orders" in describe.output and "primary key: (id)" in describe.output
    assert "foreign key: (customer_id) → shop.customers(id)" in describe.output
    assert "sample rows:" in describe.output and "Not found: nope" in describe.output


async def test_query_is_for_the_model(tmp_path: Path) -> None:
    (out,), _, _ = await _run(
        _settings(tmp_path),
        call(
            "query", {"sql": "SELECT status, count(*) AS n FROM shop.orders GROUP BY 1 ORDER BY 1"}
        ),
    )
    lines = out.output.splitlines()
    assert (
        lines[0] == "status\tn" and lines[1].startswith("cancelled\t") and lines[-1] == "(3 rows)"
    )


async def test_a_small_table_travels_whole(tmp_path: Path) -> None:
    (out,), _, _ = await _run(
        _settings(tmp_path),
        call(
            "query_table",
            {
                "sql": "SELECT category, count(*) AS n FROM shop.products GROUP BY 1 ORDER BY 1",
                "title": "品类",
            },
        ),
    )
    env = _envelope(out)
    assert env["kind"] == "table"
    assert env["props"]["columns"] == ["category", "n"] and len(env["props"]["rows"]) == 4


async def test_a_big_table_sends_a_preview_and_keeps_the_rest(tmp_path: Path) -> None:
    (out,), _, store = await _run(
        _settings(tmp_path, cache_rows=5000),
        call(
            "query_table",
            {"sql": "SELECT id, status, created_at FROM shop.orders ORDER BY id", "title": "订单"},
        ),
    )
    env = _envelope(out)
    props = env["props"]
    assert env["kind"] == "table"
    assert len(props["rows"]) == 20 and props["row_count"] == 5000 and props["truncated"] is True
    assert props["stats"]["id"] == {"min": 1, "max": 5000, "sum": 12502500, "nulls": 0}
    assert "first 20" in props["note"]
    full = await _stored(store, env)
    assert full["title"] == "订单" and "note" not in full  # the model's extras stay in the preview
    assert len(full["rows"]) == 5000 and full["rows"][0][0] == 1
    assert full["rows"][0][2].endswith("+08:00")  # the session's time zone


async def test_a_small_chart_carries_its_data(tmp_path: Path) -> None:
    sql = "SELECT status, count(*) AS n FROM shop.orders GROUP BY 1 ORDER BY 2 DESC"
    option = {"series": [{"type": "pie", "encode": {"itemName": "status", "value": "n"}}]}
    (out,), _, _ = await _run(
        _settings(tmp_path), call("query_chart", {"sql": sql, "option": option})
    )
    env = _envelope(out)
    assert env["kind"] == "chart"
    assert env["props"]["dataset"]["source"][0] == ["status", "n"]
    assert sum(r[1] for r in env["props"]["dataset"]["source"][1:]) == 6000


async def test_a_pivoted_chart(tmp_path: Path) -> None:
    sql = (
        "SELECT to_char(created_at, 'YYYY-MM') AS month, status, count(*) AS n "
        "FROM shop.orders WHERE created_at < '2025-04-01' GROUP BY 1, 2 ORDER BY 1, 2"
    )
    (out,), _, _ = await _run(
        _settings(tmp_path),
        call(
            "query_chart",
            {
                "sql": sql,
                "option": {
                    "xAxis": {"type": "category"},
                    "yAxis": {},
                    "series": [{"type": "bar", "stack": "s"}],
                },
                "pivot": {"index": "month", "columns": "status", "values": "n"},
            },
        ),
    )
    props = _envelope(out)["props"]
    assert props["dataset"]["source"][0] == ["month", "cancelled", "paid", "shipped"]
    assert [s["name"] for s in props["series"]] == ["cancelled", "paid", "shipped"]
    assert all(s["stack"] == "s" for s in props["series"])


async def test_a_big_chart_sends_a_summary(tmp_path: Path) -> None:
    sql = "SELECT created_at::date AS day, count(*) AS n FROM shop.orders GROUP BY 1 ORDER BY 1"
    option = {
        "xAxis": {"type": "category"},
        "yAxis": {},
        "series": [{"type": "line", "encode": {"x": "day", "y": "n"}}],
    }
    (out,), _, store = await _run(
        _settings(tmp_path), call("query_chart", {"sql": sql, "option": option})
    )
    env = _envelope(out)
    assert env["kind"] == "chart"
    summary = env["props"]["summary"]
    assert summary["rows"] > 300 and summary["stats"]["n"]["sum"] == 6000
    assert "dataset" not in env["props"] and env["props"]["series"] == option["series"]
    source = (await _stored(store, env))["dataset"]["source"]
    assert source[0] == ["day", "n"] and len(source) == summary["rows"] + 1


@pytest.mark.parametrize(
    ("name", "args", "match"),
    [
        ("query", {"sql": "DELETE FROM shop.refunds"}, "DELETE is not allowed"),
        ("query", {"sql": "SELECT nope FROM shop.orders"}, "search_schema / describe_tables"),
        ("query", {"sql": "SELECT 1", "database": "other"}, "no database named 'other'"),
        (
            "query_chart",
            {"sql": "SELECT 1 AS a", "option": {"dataset": {}, "series": [{"type": "bar"}]}},
            "don't pass a dataset",
        ),
        (
            "query_chart",
            {"sql": "SELECT 1 AS a", "option": {"series": [{"type": "bar", "encode": {"y": "b"}}]}},
            "the result's columns are: a",
        ),
        (
            "query_chart",
            {"sql": "SELECT 1 AS a WHERE false", "option": {"series": [{"type": "bar"}]}},
            "no rows",
        ),
    ],
)
async def test_errors_reach_the_model_with_a_hint(
    tmp_path: Path, name: str, args: dict, match: str
) -> None:
    (out,), _, _ = await _run(_settings(tmp_path), call(name, args))
    assert out.is_error and match in out.output


async def test_the_session_is_read_only_and_time_boxed(tmp_path: Path) -> None:
    db = Database("shop", Connection(url_env=URL_ENV), Limits(statement_timeout_s=1))
    try:
        with pytest.raises(ToolError, match="shop:") as exc:
            await db.run("DELETE FROM shop.refunds", max_rows=1, checked=False)
        assert "read-only" in (exc.value.hint or "")
        with pytest.raises(ToolError) as exc:
            await db.run(
                "SELECT count(*) FROM shop.order_items a, shop.order_items b, shop.order_items c",
                max_rows=1,
            )
        assert "took too long" in (exc.value.hint or "")
        res = await db.run("SELECT '12:30' AS t, 'a%b' AS p", max_rows=1)
        assert res.rows == [["12:30", "a%b"]]
    finally:
        await db.aclose()


def test_the_app_serves_the_page_and_guards_the_results(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    app = build_app(settings, model=ScriptedProvider([]), token="secret-token")
    with TestClient(app) as client:
        page = client.get("/").text
        assert "img-src 'self' data: blob:" in page and "/ext/" not in page
        # Big results are lovia's component data, behind the app's token.
        assert client.get(f"/api/sessions/{SID}/ui-data/{'A' * 22}").status_code == 401
