"""What each dialect sends the server: session settings, statement bounds, URLs."""

from __future__ import annotations

import pytest
from lovia import UserError

from db_agent.config import Connection, Limits
from db_agent.dialects import MYSQL, POSTGRES, _pg_time_zone
from db_agent.execute import Database


def _database(monkeypatch: pytest.MonkeyPatch, url: str) -> Database:
    monkeypatch.setenv("DB_AGENT_DIALECT_URL", url)
    return Database("shop", Connection(url_env="DB_AGENT_DIALECT_URL"), Limits())


@pytest.mark.parametrize(
    ("url", "dialect", "label", "schemas"),
    [
        ("postgresql+asyncpg://u:p@h/shop", POSTGRES, "PostgreSQL 16.4", None),
        ("mysql+asyncmy://u:p@h/shop", MYSQL, "MySQL 16.4", ["shop"]),
    ],
)
def test_the_url_picks_the_dialect(
    monkeypatch: pytest.MonkeyPatch, url: str, dialect: object, label: str, schemas: object
) -> None:
    db = _database(monkeypatch, url)
    db.version = "16.4"
    assert db.dialect is dialect and db.label == label and db.default_schemas() == schemas


@pytest.mark.parametrize(
    ("url", "match"),
    [
        ("mariadb+asyncmy://u:p@h/shop", "MariaDB is not supported"),
        ("sqlite+aiosqlite:///x.db", "unsupported database 'sqlite'"),
    ],
)
def test_unsupported_servers_fail_at_startup(
    monkeypatch: pytest.MonkeyPatch, url: str, match: str
) -> None:
    with pytest.raises(UserError, match=match):
        _database(monkeypatch, url)


def test_postgres_session() -> None:
    args = POSTGRES.session_args(30_000, "+08:00", 10)
    # Only what poolers such as PgBouncer accept at startup; each statement
    # gets its own limit.
    assert args == {"server_settings": {"TimeZone": "UTC-08:00"}, "timeout": 10}
    assert POSTGRES.session_args(30_000, None, 10) == {"timeout": 10}
    assert POSTGRES.bound("SELECT 1", 500) == ("SET LOCAL statement_timeout = 500", "SELECT 1")


def test_mysql_session() -> None:
    args = MYSQL.session_args(30_000, None, 10)
    assert args == {
        "init_command": "SET SESSION transaction_read_only = 1, SESSION max_execution_time = 30000",
        "connect_timeout": 10,
    }
    assert MYSQL.session_args(1, "+08:00", 10)["init_command"].endswith(", time_zone = '+08:00'")
    assert MYSQL.bound("  select * FROM t", 500) == (
        None,
        "  select /*+ MAX_EXECUTION_TIME(500) */ * FROM t",
    )


@pytest.mark.parametrize(
    ("iso", "postgres"),
    [
        ("+08:00", "UTC-08:00"),  # PostgreSQL reads offsets POSIX-style: east is negative
        ("-05:30", "UTC+05:30"),
        ("+0545", "UTC-05:45"),
        ("Asia/Shanghai", "Asia/Shanghai"),
    ],
)
def test_postgres_time_zone_offsets(iso: str, postgres: str) -> None:
    assert _pg_time_zone(iso) == postgres
