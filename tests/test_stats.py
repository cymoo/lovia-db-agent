"""Per-column statistics for big results."""

from __future__ import annotations

from db_agent.stats import column_stats


def test_column_stats_only_for_numeric_columns() -> None:
    stats = column_stats(["n", "s", "mixed"], [[1, "a", 1], [2.5, "b", "x"], [None, None, 2]])
    assert stats == {"n": {"min": 1, "max": 2.5, "sum": 3.5, "nulls": 1}}
