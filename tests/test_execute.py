"""Error hints key on driver codes — servers localize their messages."""

from __future__ import annotations

import pytest
import sqlalchemy.exc as sa_exc

from db_agent.config import Connection, Limits
from db_agent.dialects import MYSQL, POSTGRES
from db_agent.execute import Database, _unreachable, error_code


class _MySQLError(Exception):
    sqlstate = "42000"  # coarse: syntax errors and denied commands share it


class _PostgresError(Exception):
    sqlstate = "25006"


def test_mysql_errno_wins_over_its_coarse_sqlstate() -> None:
    exc = sa_exc.DBAPIError("DELETE …", None, _MySQLError(1142, "DELETE command denied"))
    assert error_code(exc) == "1142" and "only read" in MYSQL.hint(error_code(exc))


def test_postgres_sqlstate() -> None:
    exc = sa_exc.DBAPIError("DELETE …", None, _PostgresError("只读事务中不能执行 DELETE"))
    assert error_code(exc) == "25006" and "read-only" in POSTGRES.hint(error_code(exc))


def test_unknown_errors_get_a_generic_hint() -> None:
    exc = sa_exc.DBAPIError("…", None, Exception("boom"))
    assert error_code(exc) == "" and POSTGRES.hint("") == "fix the SQL and try again"
    # Each dialect reads only its own codes.
    assert POSTGRES.hint("1142") == MYSQL.hint("25006") == "fix the SQL and try again"


class _ConnectError(Exception):
    sqlstate = "08006"


def test_connection_failures_are_not_the_sqls_fault() -> None:
    refused = sa_exc.OperationalError("…", None, _MySQLError(2003, "Can't connect to MySQL"))
    gone = sa_exc.OperationalError("…", None, _ConnectError("connection failure"))
    syntax = sa_exc.ProgrammingError("…", None, _MySQLError(1064, "You have an error"))
    assert _unreachable(refused, MYSQL) and _unreachable(gone, POSTGRES)
    assert not _unreachable(syntax, MYSQL)
    assert not _unreachable(refused, POSTGRES)  # each dialect reads only its own codes


def test_scrubbing_takes_whole_words(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_AGENT_SCRUB_URL", "postgresql+asyncpg://postgres:s3cret@db.internal/x")
    db = Database("x", Connection(url_env="DB_AGENT_SCRUB_URL"), Limits())
    message = (
        "(sqlalchemy.dialects.postgresql.asyncpg.Error) role postgres denied; "
        '用户postgres认证失败; host db.internal:5432; password "s3cret"'
    )
    assert db.scrub(message) == (
        "(sqlalchemy.dialects.postgresql.asyncpg.Error) role *** denied; "
        '用户***认证失败; host ***:5432; password "***"'
    )


def test_names_are_scrubbed_before_sentence_punctuation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_AGENT_SCRUB_URL", "postgresql+asyncpg://agent:s3cret@db.internal/x")
    db = Database("x", Connection(url_env="DB_AGENT_SCRUB_URL"), Limits())
    assert db.scrub("password s3cret. host db.internal. user agent.") == (
        "password ***. host ***. user ***."
    )
    # The password goes even inside a longer word; names only when whole.
    assert db.scrub("xs3cretx db.internal.example agent.name agents") == (
        "x***x db.internal.example agent.name agents"
    )


def test_messages_are_scrubbed_before_they_are_cut(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_AGENT_SCRUB_URL", "postgresql+asyncpg://agent:s3cret@db.internal/x")
    db = Database("x", Connection(url_env="DB_AGENT_SCRUB_URL"), Limits())
    # The password straddles each cut: 300 characters for other errors, 500
    # for the driver's.
    other = db.error_text(ValueError("x" * 296 + " s3cret end"))
    driver = db.error_text(
        sa_exc.ProgrammingError("SELECT 1", {}, Exception("y" * 496 + " s3cret"))
    )
    for text in (other, driver):
        assert "s3c" not in text and "***" in text
    assert "s3c" not in str(db._tool_error(ValueError("z" * 296 + " s3cret end")))
