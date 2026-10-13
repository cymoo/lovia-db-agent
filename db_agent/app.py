"""Assemble the agent and serve it with lovia's chat UI."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from lovia import Agent, Skills, model_from_env
from lovia.tools import current_date, now
from lovia.web import SURFACE_NOTE, ChatUI, create_app, show_chart

from .config import Settings
from .plugin import SQLDatabase

__all__ = ["build_agent", "build_app"]

_HERE = Path(__file__).parent
DEFAULT_INSTRUCTIONS = (
    "You are a data analyst. Answer from the databases below: lead with the answer, "
    "then back it with a table or a chart. Reply in the user's language."
)


def build_agent(settings: Settings, db: SQLDatabase, *, model: Any = None) -> Agent[Any]:
    plugins: list[Any] = [db]
    if settings.skills_dir is not None:
        plugins.append(Skills(settings.skills_dir))
    agent: Agent[Any] = Agent(
        name="data-analyst",
        instructions="\n\n".join(
            p for p in (DEFAULT_INSTRUCTIONS, settings.instructions.strip(), SURFACE_NOTE) if p
        ),
        model=model if model is not None else model_from_env(),
        tools=[show_chart, now],
        plugins=plugins,
    )
    # Date only: stable across a day, so the prompt prefix stays cacheable.
    agent.instruction(current_date())
    return agent


def build_app(settings: Settings, *, model: Any = None, token: str | None = None) -> FastAPI:
    """The FastAPI app: lovia's chat UI and API, serving this agent."""
    token = token if token is not None else (os.environ.get("LOVIA_WEB_TOKEN") or None)
    db = SQLDatabase(settings)
    agent = build_agent(settings, db, model=model)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await db.aclose()

    app = create_app(
        agent,
        db_path=settings.data_dir / "chats.db",
        title="Data analyst",
        ui=ChatUI(
            empty_title="Ask your data",
            empty_description="Questions in plain language; answers as tables and charts.",
            templates=_HERE / "templates",
        ),
        token=token,
        lifespan=lifespan,
        max_turns=30,
    )
    app.state.sql_database = db
    return app
