"""The plugin without a database: what the model gets when one is down."""

from __future__ import annotations

from pathlib import Path

import pytest
from lovia import Runner
from lovia.testing import ScriptedProvider, call, text
from lovia.transcript import ToolResultEntry

from db_agent.app import build_agent
from db_agent.config import Connection, Settings
from db_agent.plugin import SQLDatabase


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
        connections={"down": Connection(url_env="DB_AGENT_DOWN_URL")},
        data_dir=tmp_path,
        memory=False,
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
