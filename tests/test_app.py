"""Assembly without a database: settings, the agent's tools and plugins, the CLI."""

from __future__ import annotations

from pathlib import Path

import pytest
from lovia import Memory, UserError
from lovia.workspace import LocalWorkspace

from db_agent import __main__ as cli
from db_agent.app import build_agent
from db_agent.check import check
from db_agent.config import Settings, load_settings
from db_agent.execute import Database
from db_agent.plugin import SQLDatabase

URL_ENV = "DB_AGENT_APP_URL"


@pytest.fixture(autouse=True)
def _url(monkeypatch: pytest.MonkeyPatch) -> None:
    # Nothing listens on port 9: nothing here connects.
    monkeypatch.setenv(URL_ENV, "postgresql+asyncpg://u:p@127.0.0.1:9/db")


def _settings(tmp_path: Path, **extra: object) -> Settings:
    return Settings.model_validate(
        {"connections": {"shop": {"url_env": URL_ENV}}, "data_dir": tmp_path, **extra}
    )


def test_the_agent_has_memory_a_workspace_and_the_show_tools(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    agent = build_agent(settings, SQLDatabase(settings), model="openai:x")
    names = {t.name for t in agent.tools}
    assert {"show_table", "show_chart", "show_html", "now"} <= names
    (memory,) = [p for p in agent.plugins if isinstance(p, Memory)]
    assert Path(memory.root) == tmp_path / "memory"
    # Notes only: chats — and the query results in them — are neither
    # archived nor mined for facts.
    assert memory.index is None and not memory.auto_curate
    assert isinstance(agent.workspace, LocalWorkspace)
    assert Path(agent.workspace.root) == tmp_path / "workspace"
    assert (tmp_path / "workspace").is_dir()


def test_memory_and_workspace_turn_off_with_false(tmp_path: Path) -> None:
    settings = _settings(tmp_path, memory=False, workspace=False)
    agent = build_agent(settings, SQLDatabase(settings), model="openai:x")
    assert not any(isinstance(p, Memory) for p in agent.plugins)
    assert agent.workspace is None and not (tmp_path / "workspace").exists()


def test_relative_directories_resolve_against_the_config_file(tmp_path: Path) -> None:
    (tmp_path / "config.yaml").write_text(
        f"connections:\n  shop:\n    url_env: {URL_ENV}\n"
        "memory:\n  dir: notes\n"
        "workspace:\n  dir: files\n  mode: readonly\n",
        encoding="utf-8",
    )
    settings = load_settings(tmp_path / "config.yaml")
    assert settings.data_dir == tmp_path / ".data"
    assert settings.memory_dir == tmp_path / "notes"
    assert settings.workspace_dir == tmp_path / "files"
    assert settings.workspace.mode == "readonly"


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("connections:\n  shop: {}\n", r"connections\.shop\.url_env: Field required"),
        (
            f"connections:\n  shop:\n    url_env: {URL_ENV}\nworkspace:\n  mode: root\n",
            r"workspace\.mode: Input should be",
        ),
        ("connections: [\n", "is not valid YAML"),
    ],
)
def test_config_errors_say_where(tmp_path: Path, text: str, match: str) -> None:
    (tmp_path / "config.yaml").write_text(text, encoding="utf-8")
    with pytest.raises(UserError, match=match):
        load_settings(tmp_path / "config.yaml")


async def test_check_reports_each_problem(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOVIA_MODEL", raising=False)
    monkeypatch.delenv("DB_AGENT_APP_MISSING", raising=False)
    settings = Settings.model_validate(
        {
            "connections": {
                "down": {"url_env": URL_ENV},
                "unset": {"url_env": "DB_AGENT_APP_MISSING"},
            },
            "data_dir": tmp_path,
        }
    )
    lines: list[str] = []
    assert not await check(settings, lines.append)
    out = "\n".join(lines)
    assert "model      not set" in out
    assert "down       FAILED: down is unreachable or busy" in out
    assert "unset      FAILED: DB_AGENT_APP_MISSING is not set" in out


@pytest.mark.parametrize(
    "url", ["postgresql+asyncpg://u:s3cret@h:abc/db", "s3cret not a url"], ids=["port", "garbage"]
)
async def test_check_goes_on_past_a_malformed_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    monkeypatch.setenv("DB_AGENT_APP_BAD", url)
    settings = Settings.model_validate(
        {
            "connections": {"bad": {"url_env": "DB_AGENT_APP_BAD"}, "down": {"url_env": URL_ENV}},
            "data_dir": tmp_path,
        }
    )
    lines: list[str] = []
    assert not await check(settings, lines.append)
    out = "\n".join(lines)
    assert "bad        FAILED: DB_AGENT_APP_BAD is not a valid database URL" in out
    assert "s3cret" not in out and "down       FAILED" in out


async def test_check_survives_an_error_without_a_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fine(self: Database, sql: str, **_: object) -> None:
        return None

    async def slow(self: Database) -> str:
        raise TimeoutError()  # what a client-side timeout raises: no message

    monkeypatch.setattr(Database, "run", fine)
    monkeypatch.setattr(Database, "load_version", slow)
    lines: list[str] = []
    assert not await check(_settings(tmp_path), lines.append)
    assert lines[-1].startswith(
        "shop       FAILED: connected, but reading the catalog failed: TimeoutError\n"
    )


def test_the_cli_serves_by_default_and_reads_env_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("DB_AGENT_PORT=8123\n", encoding="utf-8")
    (tmp_path / "config.yaml").write_text(
        f"connections:\n  shop:\n    url_env: {URL_ENV}\nmemory: false\n", encoding="utf-8"
    )
    monkeypatch.setenv("LOVIA_MODEL", "openai:x")
    # Set, then unset: monkeypatch then removes what .env loads, after the test.
    monkeypatch.setenv("DB_AGENT_PORT", "1")
    monkeypatch.delenv("DB_AGENT_PORT")
    monkeypatch.delenv("LOVIA_WEB_TOKEN", raising=False)
    served: dict[str, object] = {}

    def fake_serve(app: object, *, host: str, port: int) -> None:
        served.update(host=host, port=port, token=app.state.lovia_serving.token)  # type: ignore[attr-defined]

    monkeypatch.setattr(cli, "serve", fake_serve)
    cli.main([])
    assert served == {"host": "127.0.0.1", "port": 8123, "token": None}
    # Beyond loopback, a token is generated rather than the app refused.
    cli.main(["--host", "0.0.0.0"])
    assert isinstance(served["token"], str) and served["token"]


def test_cli_errors_are_one_message_not_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc:
        cli.main(["check", "--config", "missing.yaml"])
    assert str(exc.value.code).startswith("error: config file not found: missing.yaml")
