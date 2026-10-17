"""Settings: the named database connections and the agent's limits.

Loaded from a YAML file. Connection URLs never appear in it — each
connection names the environment variable that holds its URL, so the file can
be committed and the model never sees a credential.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from lovia import UserError
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

__all__ = [
    "Connection",
    "Limits",
    "MemorySettings",
    "Settings",
    "WorkspaceSettings",
    "load_settings",
]

_NAME_RE = re.compile(r"[a-z][a-z0-9_]{0,31}")
# A time zone as both PostgreSQL and MySQL spell it: a name or an offset.
_TZ_RE = re.compile(r"[A-Za-z0-9_/+\-:]{1,64}")


class Connection(BaseModel):
    """One database the agent may query."""

    url_env: str
    """Environment variable holding the SQLAlchemy URL, e.g.
    ``postgresql+asyncpg://agent_ro:***@host/db`` or ``mysql+asyncmy://…``."""
    description: str = ""
    """What the data is about, in a line — the model reads it."""
    schemas: list[str] | None = None
    """Schemas (MySQL: databases) the agent discovers. ``None``: every schema the
    account can read (PostgreSQL), or the URL's database (MySQL). Scope for
    discovery, not a security boundary — grant only what the agent may see."""
    exclude_tables: list[str] = Field(default_factory=list)
    """Glob patterns of tables to leave out of discovery, e.g. ``tmp_*``."""
    timezone: str | None = None
    """Session time zone, e.g. ``Asia/Shanghai`` or ``+08:00``: what "today"
    and date truncation mean in the database."""

    @field_validator("timezone")
    @classmethod
    def _check_timezone(cls, v: str | None) -> str | None:
        if v is not None and not _TZ_RE.fullmatch(v):
            raise ValueError(f"not a time zone name or offset: {v!r}")
        return v

    def url(self) -> str:
        value = os.environ.get(self.url_env, "").strip()
        if not value:
            raise UserError(
                f"{self.url_env} is not set",
                hint=f"put the connection URL in the environment (or .env): {self.url_env}=...",
            )
        return value


class Limits(BaseModel):
    statement_timeout_s: float = Field(15, gt=0)
    """Server-side cap on one statement."""
    query_rows: int = Field(50, ge=1)
    """Rows the model sees from `query`, its exploration tool."""
    preview_rows: int = Field(20, ge=1)
    """Rows of a `query_table` result the model sees; the user sees them all."""
    cache_rows: int = Field(10_000, ge=1)
    """Most rows a `query_table` result keeps for the user."""
    chart_rows: int = Field(5_000, ge=1)
    """Most rows a `query_chart` query may return."""
    chart_max_series: int = Field(20, ge=1)
    chart_inline_bytes: int = Field(4096, ge=0)
    """Chart data up to this size travels in the tool output (the model sees
    it); bigger data is kept with the chat and the model gets a summary."""
    catalog_budget_tokens: int = Field(2000, ge=0)
    """How much of the system prompt the table list may take, all databases together."""


class _Switch(BaseModel):
    """A feature that ``true`` / ``false`` alone turns on or off."""

    enabled: bool = True

    @model_validator(mode="before")
    @classmethod
    def _from_bool(cls, v: Any) -> Any:
        if isinstance(v, bool):
            return {"enabled": v}
        return {} if v is None else v


class MemorySettings(_Switch):
    """Notes the agent keeps across chats: the user's preferences, confirmed
    definitions. Shared by everyone using this app; the page can edit them."""

    dir: Path | None = None
    """Default: ``<data_dir>/memory``."""


class WorkspaceSettings(_Switch):
    """A directory the agent reads and writes files in, shown in the page's
    Files panel. Shared by every chat."""

    dir: Path | None = None
    """Default: ``<data_dir>/workspace``; created if missing."""
    mode: Literal["readonly", "coding", "trusted"] = "coding"
    """readonly: read files only. coding: write inside the directory; shell
    commands and reads outside it wait for the user's approval. trusted:
    shell commands run unasked."""


class Settings(BaseModel):
    connections: dict[str, Connection]
    limits: Limits = Field(default_factory=Limits)
    instructions: str = ""
    """Extra instructions for the agent: language, tone, house rules."""
    data_dir: Path = Path(".data")
    """Where chats live (big results are kept with their chat)."""
    skills_dir: Path | None = None
    """A directory of SKILL.md files: business definitions, join paths, gotchas."""
    memory: MemorySettings = Field(default_factory=MemorySettings)
    workspace: WorkspaceSettings = Field(default_factory=WorkspaceSettings)
    title: str = "Data analyst"
    """The page's title."""
    followups: bool = False
    """Suggest follow-up questions after each answer (one more model call per answer)."""
    max_turns: int = Field(30, ge=1)
    """Most model turns in one answer."""

    @property
    def memory_dir(self) -> Path:
        return self.memory.dir or self.data_dir / "memory"

    @property
    def workspace_dir(self) -> Path:
        return self.workspace.dir or self.data_dir / "workspace"

    @field_validator("connections")
    @classmethod
    def _check_names(cls, v: dict[str, Connection]) -> dict[str, Connection]:
        if not v:
            raise ValueError("configure at least one connection")
        for name in v:
            if not _NAME_RE.fullmatch(name):
                raise ValueError(
                    f"connection name {name!r}: use 1-32 chars of [a-z0-9_], starting with a letter"
                )
        return v


def load_settings(path: str | Path) -> Settings:
    """Read settings from a YAML file; relative directories resolve against it."""
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        raise UserError(
            f"config file not found: {path}",
            hint="copy config.example.yaml to config.yaml and edit it",
        ) from None
    except yaml.YAMLError as exc:
        raise UserError(f"{path} is not valid YAML: {exc}") from None
    try:
        settings = Settings.model_validate(raw)
    except ValidationError as exc:
        raise UserError(f"{path}: {_problems(exc)}", hint="see config.example.yaml") from None
    base = path.resolve().parent
    updates: dict[str, Any] = {"data_dir": base / settings.data_dir}
    if settings.skills_dir is not None:
        updates["skills_dir"] = base / settings.skills_dir
    for name in ("memory", "workspace"):
        section = getattr(settings, name)
        if section.dir is not None:
            updates[name] = section.model_copy(update={"dir": base / section.dir})
    return settings.model_copy(update=updates)


def _problems(exc: ValidationError) -> str:
    """Pydantic's errors as one line each: ``connections.shop.url_env: Field required``."""
    lines = []
    for err in exc.errors():
        where = ".".join(str(p) for p in err["loc"]) or "(top level)"
        lines.append(f"{where}: {err['msg']}")
    return "; ".join(lines)
