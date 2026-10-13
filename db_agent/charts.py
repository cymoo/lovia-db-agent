"""Turn a query result plus a model-written ECharts option into a chart.

The model writes the option without data and maps columns with ``encode``;
the rows become ``dataset.source``. For one series per category, the model
passes ``pivot`` and a single series as a template: long rows
(month, category, revenue) become wide ones (month, A, B, …) and the template
is repeated per category — no need to know the categories up front.
"""

from __future__ import annotations

import copy
from typing import Any

from lovia import ToolError

from .stats import column_stats

__all__ = ["apply_pivot", "check_encode", "summarize"]


def apply_pivot(
    option: dict[str, Any],
    columns: list[str],
    rows: list[list[Any]],
    pivot: dict[str, str],
    *,
    max_series: int,
) -> tuple[dict[str, Any], list[str], list[list[Any]]]:
    """Long → wide rows, and one series per category from the template."""
    keys = {"index", "columns", "values"}
    if set(pivot) != keys:
        raise ToolError(
            "pivot needs exactly: index, columns, values",
            hint='e.g. {"index": "month", "columns": "category", "values": "revenue"}',
        )
    try:
        ix, cx, vx = (columns.index(pivot[k]) for k in ("index", "columns", "values"))
    except ValueError:
        raise ToolError(
            f"pivot names a column the query doesn't return: {pivot}",
            hint="the result's columns are: " + ", ".join(columns),
        ) from None

    categories: list[Any] = []
    table: dict[Any, dict[Any, Any]] = {}
    for r in rows:
        key, cat = r[ix], r[cx]
        if cat not in categories:
            categories.append(cat)
        table.setdefault(key, {})[cat] = r[vx]
    if len(categories) > max_series:
        raise ToolError(
            f"{len(categories)} categories would make as many series; the limit is {max_series}",
            hint="keep the top categories in SQL and fold the rest into 'other'",
        )

    series = option.get("series")
    templates = [series] if isinstance(series, dict) else series
    if not isinstance(templates, list) or len(templates) != 1 or not isinstance(templates[0], dict):
        raise ToolError(
            "with pivot, give exactly one series as the template",
            hint='e.g. "series": [{"type": "line"}] — it is repeated for each category',
        )
    template = templates[0]
    names = [str(c) for c in categories]
    horizontal = _axis_type(option.get("yAxis")) == "category"
    index = columns[ix]
    out = copy.deepcopy(option)
    out["series"] = [
        {
            **copy.deepcopy(template),
            "name": name,
            "encode": {"y": index, "x": name} if horizontal else {"x": index, "y": name},
        }
        for name in names
    ]
    wide = [[key, *(vals.get(c) for c in categories)] for key, vals in table.items()]
    return out, [index, *names], wide


def check_encode(option: dict[str, Any], columns: list[str]) -> None:
    """Every column a series' ``encode`` names must be in the result."""
    series = option.get("series")
    for s in [series] if isinstance(series, dict) else series or []:
        encode = s.get("encode") if isinstance(s, dict) else None
        if not isinstance(encode, dict):
            continue
        for value in encode.values():
            for ref in value if isinstance(value, list) else [value]:
                if isinstance(ref, str) and ref not in columns:
                    raise ToolError(
                        f"encode names the column {ref!r}, which the query doesn't return",
                        hint="the result's columns are: " + ", ".join(columns),
                    )


def summarize(columns: list[str], rows: list[list[Any]]) -> dict[str, Any]:
    """What the model gets instead of a big chart's data."""
    return {
        "rows": len(rows),
        "columns": columns,
        "stats": column_stats(columns, rows),
        "first_row": rows[0] if rows else None,
        "last_row": rows[-1] if rows else None,
    }


def _axis_type(axis: Any) -> Any:
    first = axis[0] if isinstance(axis, list) and axis else axis
    return first.get("type") if isinstance(first, dict) else None
