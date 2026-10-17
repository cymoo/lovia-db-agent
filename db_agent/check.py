"""``lovia-db-agent check``: try the configuration before serving it.

Connects to every database the way the agent will, and reports what the
agent would see — or the error and a hint — without starting a model.
"""

from __future__ import annotations

from collections.abc import Callable

import sqlalchemy.exc as sa_exc
from lovia import ToolError, UserError, model_from_env

from .catalog import load_catalog
from .config import Settings
from .execute import Database, error_code

__all__ = ["check"]


async def check(settings: Settings, out: Callable[[str], None] = print) -> bool:
    """Print what works and what doesn't; True when everything does."""
    ok = True
    model = model_from_env(required=False)
    if model is None:
        ok = False
    out(f"model      {model or 'not set: put LOVIA_MODEL in the environment (or .env)'}")
    out(f"chats      {settings.data_dir / 'chats.db'}")
    memory = settings.memory_dir if settings.memory.enabled else "off"
    out(f"memory     {memory}")
    workspace = settings.workspace
    out(
        f"workspace  {settings.workspace_dir} ({workspace.mode})"
        if workspace.enabled
        else "workspace  off"
    )
    if settings.skills_dir is not None:
        skills = sorted(p.parent.name for p in settings.skills_dir.glob("*/SKILL.md"))
        out(f"skills     {', '.join(skills) or f'none found in {settings.skills_dir}'}")
    for name, conn in settings.connections.items():
        try:
            db = Database(name, conn, settings.limits)
        except UserError as exc:  # no URL, or an unsupported server
            ok = False
            out(f"{name:<10} FAILED: {exc}")
            continue
        try:
            out(f"{name:<10} {await _probe(db)}")
        except ToolError as exc:
            ok = False
            out(f"{name:<10} FAILED: {exc}")
        finally:
            await db.aclose()
    return ok


async def _probe(db: Database) -> str:
    await db.run("SELECT 1", max_rows=1)  # a ToolError with a hint, as the model gets it
    try:
        await db.load_version()
        catalog = await load_catalog(db)
        read_only = (await db.fetch(db.dialect.read_only_sql))[0][0]
    except Exception as exc:  # noqa: BLE001 - connected, but the catalog failed
        raise ToolError(
            f"connected to {db.label}, but reading the catalog failed: {db.error_text(exc)}",
            hint=_catalog_hint(db, exc),
        ) from None
    views = sum(t.kind == "view" for t in catalog.tables)
    facts = [db.label, f"{len(catalog.tables) - views} tables, {views} views"]
    if not catalog.tables:
        facts.append(
            "the account can read none: grant it SELECT (and USAGE on the schemas), "
            "or check `schemas` in the config"
        )
    if str(read_only).lower() not in ("on", "1"):
        facts.append(
            "sessions can write: make sure the account has only SELECT grants "
            "(see the README's 'Prepare the database')"
        )
    return " · ".join(facts)


def _catalog_hint(db: Database, exc: Exception) -> str:
    code = error_code(exc) if isinstance(exc, sa_exc.DBAPIError) else ""
    if code in db.dialect.denied_codes:
        return "grant the account SELECT on the tables, and USAGE on their schemas"
    if code in db.dialect.missing_codes:
        return (
            "the server lacks something the catalog query uses: an older version or a "
            "compatible fork. Please report it, with what SELECT version() returns"
        )
    return "check the account's grants, and report what SELECT version() returns"
