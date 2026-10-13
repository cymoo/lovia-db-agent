"""Per-column statistics: what the model gets instead of rows it doesn't see."""

from __future__ import annotations

from typing import Any

__all__ = ["column_stats"]


def column_stats(columns: list[str], rows: list[list[Any]]) -> dict[str, dict[str, Any]]:
    """Per numeric column: min, max, sum and nulls — what the model needs to
    talk honestly about rows it hasn't seen."""
    stats: dict[str, dict[str, Any]] = {}
    for i, name in enumerate(columns):
        values = [r[i] for r in rows]
        nums = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
        if not nums or len(nums) + values.count(None) < len(values):
            continue
        total = sum(nums)
        stats[name] = {
            "min": min(nums),
            "max": max(nums),
            "sum": round(total, 6) if isinstance(total, float) else total,
            "nulls": values.count(None),
        }
    return stats
