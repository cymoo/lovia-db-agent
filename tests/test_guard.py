"""The statement guard: what passes, what is refused, and how LIMIT is capped."""

from __future__ import annotations

import pytest
from lovia import ToolError

from db_agent.guard import prepare


@pytest.mark.parametrize(
    ("dialect", "sql", "expected"),
    [
        ("postgres", "SELECT 1;", "SELECT 1 LIMIT 5"),
        ("postgres", "SELECT * FROM t LIMIT 3", "SELECT * FROM t LIMIT 3"),  # untouched
        ("postgres", "SELECT * FROM t LIMIT 30", "SELECT * FROM t LIMIT 5"),
        ("postgres", "SELECT * FROM t LIMIT ALL", "SELECT * FROM t LIMIT 5"),
        ("postgres", "SELECT * FROM t OFFSET 10", "SELECT * FROM t LIMIT 5 OFFSET 10"),
        (
            "postgres",
            "SELECT * FROM t FETCH FIRST 2 ROWS ONLY",
            "SELECT * FROM t FETCH FIRST 2 ROWS ONLY",
        ),
        ("postgres", "SELECT * FROM t FETCH FIRST 50 ROWS ONLY", "SELECT * FROM t LIMIT 5"),
        ("mysql", "SELECT * FROM t LIMIT 5, 10", "SELECT * FROM t LIMIT 5 OFFSET 5"),
        # The cap goes on the whole compound query, after ORDER BY — not on a
        # wrapping subquery, where MySQL may drop the ORDER BY.
        (
            "postgres",
            "SELECT a FROM t UNION SELECT b FROM u ORDER BY 1",
            "SELECT a FROM t UNION SELECT b FROM u ORDER BY 1 LIMIT 5",
        ),
        (
            "postgres",
            "WITH x AS (SELECT 1 AS a) SELECT * FROM x ORDER BY a",
            "WITH x AS (SELECT 1 AS a) SELECT * FROM x ORDER BY a LIMIT 5",
        ),
        (
            "postgres",
            "SELECT 1 -- note; with a semicolon",
            "SELECT 1 /* note; with a semicolon */ LIMIT 5",
        ),
        ("postgres", "EXPLAIN SELECT 1", "EXPLAIN SELECT 1"),
        ("postgres", "EXPLAIN (FORMAT JSON) SELECT 1", "EXPLAIN (FORMAT JSON) SELECT 1"),
        ("mysql", "SHOW TABLES", "SHOW TABLES"),
    ],
)
def test_allowed_statements_and_the_limit(dialect: str, sql: str, expected: str) -> None:
    assert prepare(sql, dialect, 5) == expected


def test_no_limit_leaves_the_statement_alone() -> None:
    assert prepare("SELECT * FROM t", "postgres", None) == "SELECT * FROM t"


def test_literals_survive_the_rewrite() -> None:
    out = prepare("SELECT '12:30', 'a%b' FROM t WHERE n LIKE '%x%'", "postgres", 5)
    assert "'12:30'" in out and "'%x%'" in out and "'a%b'" in out


@pytest.mark.parametrize(
    ("dialect", "sql", "match"),
    [
        ("postgres", "DELETE FROM t", "DELETE is not allowed"),
        ("postgres", "UPDATE t SET a = 1", "UPDATE is not allowed"),
        ("postgres", "DROP TABLE t", "DROP is not allowed"),
        ("postgres", "VACUUM", "VACUUM is not allowed"),
        ("postgres", "SET statement_timeout = 0", "not allowed"),
        ("postgres", "WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x", "DELETE"),
        ("postgres", "EXPLAIN ANALYZE DELETE FROM t", "DELETE"),
        ("postgres", "SELECT * INTO new FROM t", "INTO"),
        ("postgres", "SELECT * FROM t FOR UPDATE", "locking"),
        ("postgres", "SELECT 1; DROP TABLE t", "2 statements"),
        ("postgres", "SELECT pg_sleep(10)", "pg_sleep"),
        ("postgres", "SELECT set_config('statement_timeout', '0', false)", "set_config"),
        ("mysql", "SELECT SLEEP(10)", "sleep"),
        ("mysql", "SELECT BENCHMARK(1000000, MD5('x'))", "benchmark"),
        ("postgres", "SELEC 1", "could not parse"),
        ("postgres", "  ;  ", "no SQL statement"),
    ],
)
def test_refused_statements(dialect: str, sql: str, match: str) -> None:
    with pytest.raises(ToolError, match=match) as exc:
        prepare(sql, dialect, 5)
    assert exc.value.hint
