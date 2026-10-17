"""Assemble the agent and serve it with lovia's chat UI."""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from lovia import Agent, Memory, Skills, model_from_env
from lovia.tools import current_date, now
from lovia.web import SURFACE_NOTE, ChatUI, create_app, show_chart, show_html, show_table
from lovia.workspace import LocalWorkspace, Workspace

from .config import Settings
from .plugin import SQLDatabase

__all__ = ["build_agent", "build_app"]

_HERE = Path(__file__).parent
MEMORY_DRAIN_S = 30
"""How long shutdown waits for the notes of the last answers."""
DEFAULT_INSTRUCTIONS = (
    "You are a data analyst. Answer from the databases below: lead with the answer, "
    "then back it with a table or a chart. Reply in the user's language."
)


_MEMORY_NOTE = (
    "Remember only what the user asks you to keep, or confirms: preferences, business "
    "definitions, corrections. Never query results or personal data from the databases."
)


def build_agent(settings: Settings, db: SQLDatabase, *, model: Any = None) -> Agent[Any]:
    plugins: list[Any] = [db]
    if settings.skills_dir is not None:
        plugins.append(Skills(settings.skills_dir))
    notes = [DEFAULT_INSTRUCTIONS, settings.instructions.strip(), SURFACE_NOTE]
    if settings.memory.enabled:
        # Notes only, written when the model calls remember: no archive of the
        # chats and no automatic curation, either of which would keep answers —
        # query results — where every user's agent reads them. The periodic
        # tidy-up of the notes runs off the answer's path.
        plugins.append(
            Memory(settings.memory_dir, index=None, auto_curate=False, curate_in_background=True)
        )
        notes.append(_MEMORY_NOTE)
    agent: Agent[Any] = Agent(
        name="data-analyst",
        instructions="\n\n".join(p for p in notes if p),
        model=model if model is not None else model_from_env(),
        tools=[show_table, show_chart, show_html, now],
        plugins=plugins,
        workspace=_workspace(settings),
    )
    # Date only: stable across a day, so the prompt prefix stays cacheable.
    agent.instruction(current_date())
    return agent


def _workspace(settings: Settings) -> LocalWorkspace | None:
    if not settings.workspace.enabled:
        return None
    root = settings.workspace_dir
    root.mkdir(parents=True, exist_ok=True)
    # Shell commands get a minimal environment (no database URLs or API keys),
    # and no tool may open a .env file, inside the directory or out.
    return Workspace.local(str(root), mode=settings.workspace.mode, denied_paths=(".env*",))


def build_app(settings: Settings, *, model: Any = None, token: str | None = None) -> FastAPI:
    """The FastAPI app: lovia's chat UI and API, serving this agent."""
    token = token if token is not None else (os.environ.get("LOVIA_WEB_TOKEN") or None)
    db = SQLDatabase(settings)
    agent = build_agent(settings, db, model=model)
    memory = next((p for p in agent.plugins or [] if isinstance(p, Memory)), None)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if memory is not None:
                # A tidy-up of the notes in flight, unless it hangs.
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(memory.drain(), MEMORY_DRAIN_S)
            await db.aclose()

    app = create_app(
        agent,
        db_path=settings.data_dir / "chats.db",
        title=settings.title,
        ui=ChatUI(
            empty_title="Ask your data",
            empty_description="Questions in plain language; answers as tables and charts.",
            templates=_HERE / "templates",
        ),
        token=token,
        lifespan=lifespan,
        max_turns=settings.max_turns,
        followups=settings.followups,
    )
    app.state.sql_database = db
    return app
