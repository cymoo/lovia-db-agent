"""Turn a query result plus a model-written ECharts option into a chart.

The model writes the option without data and maps columns with ``encode``;
the rows become ``dataset.source``. For one series per category, the model
passes ``split_by`` and a single series as a template: long rows
(month, category, revenue) become wide ones (month, A, B, …) and the template
is repeated per category — no need to know the categories up front.
"""

from __future__ import annotations

import copy
import heapq
import itertools
import json
from typing import Any

from lovia import ToolError

from .stats import column_stats

__all__ = ["apply_split", "check_encode", "summarize"]

# Series drawn on x/y axes, one value per x: the ones split_by can repeat.
_SPLITTABLE = {"line", "bar", "scatter", "effectScatter"}
NULL_LABEL = "(null)"


def apply_split(
    option: dict[str, Any],
    columns: list[str],
    rows: list[list[Any]],
    split_by: str,
    *,
    max_series: int,
) -> tuple[dict[str, Any], list[str], list[list[Any]]]:
    """Long → wide rows, and one series per ``split_by`` category from the template.

    The template's encode names the x and y columns; for a horizontal bar,
    the numeric one is the value. Each category's x values keep their order:
    with ``ORDER BY category, month`` the months still come out in order.
    """
    if split_by not in columns:
        raise ToolError(
            f"split_by names the column {split_by!r}, which the query doesn't return",
            hint="the result's columns are: " + ", ".join(columns),
        )
    template = _template(option)
    cx = columns.index(split_by)
    x_name, y_name, swap = _axes(option, template, columns, rows, split_by)
    xx, yx = columns.index(x_name), columns.index(y_name)

    categories: dict[Any, int] = {}  # key → index in labels
    labels: list[Any] = []
    cells: dict[tuple[Any, int], Any] = {}  # (x key, category) → value
    xs: dict[Any, Any] = {}  # x key → x value, in first appearance
    runs: list[list[Any]] = []  # each category's x keys, in row order
    for r in rows:
        ck = _key(r[cx])
        c = categories.get(ck)
        if c is None:
            c = categories[ck] = len(labels)
            labels.append(r[cx])
            runs.append([])
            if len(labels) > max_series:
                raise ToolError(
                    f"{split_by} has more than {max_series} values, and each would be "
                    f"a series; the limit is {max_series}",
                    hint="keep the top categories in SQL and fold the rest into 'other'",
                )
        xk = _key(r[xx])
        if (xk, c) in cells:
            hint = f"GROUP BY {x_name}, {split_by} and aggregate {y_name} in SQL"
            if template["type"] in ("scatter", "effectScatter"):
                hint += "; to plot every raw point, drop split_by: they make one series"
            raise ToolError(
                f"more than one row has {x_name} = {r[xx]!r} and {split_by} = {r[cx]!r}; "
                "split_by needs one row per pair",
                hint=hint,
            )
        cells[xk, c] = r[yx]
        xs.setdefault(xk, r[xx])
        runs[c].append(xk)

    # The legend toggles series by name: 1 and "1", or NULL and "(null)",
    # need names of their own. Dataset columns also can't reuse the x column.
    series_names = _distinct([NULL_LABEL if c is None else str(c) for c in labels], taken=set())
    names = _distinct(series_names, taken={x_name})
    order = _merge_orders(list(xs), runs)
    wide = [[xs[k], *(cells.get((k, c)) for c in range(len(labels)))] for k in order]
    out = copy.deepcopy(option)
    out["series"] = [
        {
            **copy.deepcopy(template),
            "name": series_name,
            "encode": {"y": x_name, "x": name} if swap else {"x": x_name, "y": name},
        }
        for series_name, name in zip(series_names, names, strict=True)
    ]
    _type_axes(out, template, horizontal=swap)
    return out, [x_name, *names], wide


def _type_axes(option: dict[str, Any], template: dict[str, Any], *, horizontal: bool) -> None:
    """Give the series' axes the types its orientation needs, where unset.

    ECharts defaults xAxis to category and yAxis to value: a horizontal bar
    needs both the other way round.
    """
    if template.get("coordinateSystem", "cartesian2d") != "cartesian2d":
        return
    kinds = {"x": "value", "y": "category"} if horizontal else {"x": "category", "y": "value"}
    for dim, kind in kinds.items():
        if option.get(f"{dim}Axis") is None and template.get(f"{dim}AxisIndex", 0) == 0:
            option[f"{dim}Axis"] = {}
        axis = _axis(option, template, dim)
        if axis is not None:
            axis.setdefault("type", kind)


def _axis(option: dict[str, Any], template: dict[str, Any], dim: str) -> dict[str, Any] | None:
    """The x or y axis the series is drawn on, by its xAxisIndex / yAxisIndex."""
    axes = option.get(f"{dim}Axis")
    i = template.get(f"{dim}AxisIndex", 0)
    if isinstance(axes, dict):
        axes = [axes]
    if not isinstance(axes, list) or not isinstance(i, int) or not 0 <= i < len(axes):
        return None
    axis = axes[i]
    return axis if isinstance(axis, dict) else None


def _horizontal(option: dict[str, Any], template: dict[str, Any]) -> bool:
    """When the data can't tell, a category y axis makes bars horizontal."""
    axis = _axis(option, template, "y")
    return axis is not None and axis.get("type") == "category"


def _template(option: dict[str, Any]) -> dict[str, Any]:
    series = option.get("series")
    templates = [series] if isinstance(series, dict) else series
    if not isinstance(templates, list) or not templates:
        raise ToolError(
            "with split_by, give one series as the template",
            hint='e.g. "series": [{"type": "line", "encode": {"x": "month", "y": "revenue"}}]'
            " — it is repeated for each category",
        )
    if any(isinstance(s, dict) and s.get("type") in ("pie", "funnel") for s in templates):
        raise ToolError(
            "split_by doesn't apply to a pie or funnel: it has one series, one slice per row",
            hint="drop split_by and map the category with encode, e.g. "
            '{"type": "pie", "encode": {"itemName": "category", "value": "revenue"}}',
        )
    if len(templates) != 1:
        raise ToolError(
            f"with split_by, give exactly one series, not {len(templates)}: split_by "
            "repeats that one series for each category",
            hint="for several measures (e.g. revenue and orders), drop split_by and give "
            'one series per column with encode, e.g. [{"type": "line", "encode": '
            '{"x": "month", "y": "revenue"}}, {"type": "line", "encode": '
            '{"x": "month", "y": "orders"}}]',
        )
    template = templates[0]
    kind = template.get("type") if isinstance(template, dict) else None
    if not isinstance(template, dict) or kind not in _SPLITTABLE:
        raise ToolError(
            f"split_by repeats a line, bar or scatter series, not {kind!r}",
            hint="drop split_by, or use a line, bar or scatter series",
        )
    return template


def _axes(
    option: dict[str, Any],
    template: dict[str, Any],
    columns: list[str],
    rows: list[list[Any]],
    split_by: str,
) -> tuple[str, str, bool]:
    """(x column, value column, horizontal?) for the template."""
    hint = (
        'give the series an encode naming the x and y columns, e.g. {"x": "month", '
        '"y": "revenue"} ({"y": "region", "x": "revenue"} for horizontal bars)'
    )
    encode = template.get("encode")
    if not encode:
        rest = [c for c in columns if c != split_by]
        if len(rest) != 2:
            raise ToolError(
                f"the series has no encode, and with {len(columns)} columns it can't be "
                "inferred: which is x and which is y?",
                hint=hint,
            )
        a, b = (_numeric(rows, columns.index(c)) for c in rest)
        if a is None and b is None:
            why = f"{rest[0]} and {rest[1]} are all NULL"
        elif a == b:
            why = (
                f"both {rest[0]} and {rest[1]} are numbers"
                if a
                else f"neither {rest[0]} nor {rest[1]} is a number"
            )
        else:
            # The number is the value; an all-NULL column is the value too
            # (an x of only NULLs makes no axis).
            value = rest[0] if a is None or (a and b is False) else rest[1]
            (x,) = [c for c in rest if c != value]
            return x, value, _horizontal(option, template)
        raise ToolError(f"the series has no encode, and it can't be inferred: {why}", hint=hint)
    x_ref, y_ref = (_one(encode.get(k)) if isinstance(encode, dict) else None for k in "xy")
    if x_ref is None or y_ref is None:
        raise ToolError(
            f"with split_by, the series' encode needs one x and one y column, not {encode!r}",
            hint=hint + "; for several measures, drop split_by and give one series per column",
        )
    for ref in (x_ref, y_ref):
        if ref not in columns:
            raise ToolError(
                f"encode names the column {ref!r}, which the query doesn't return",
                hint="the result's columns are: " + ", ".join(columns),
            )
        if ref == split_by:
            raise ToolError(
                f"encode names {split_by!r}, the split_by column",
                hint="split_by makes one series per category; encode the other two "
                "columns as x and y",
            )
    if x_ref == y_ref:
        raise ToolError(f"encode names {x_ref!r} as both x and y", hint=hint)
    x_num, y_num = (_numeric(rows, columns.index(c)) for c in (x_ref, y_ref))
    if x_num is not None and y_num is not None and x_num != y_num:
        swap = x_num  # the number is the value
    else:  # both numbers, neither, or a column of NULLs: only the axes can tell
        swap = _horizontal(option, template)
    return (y_ref, x_ref, True) if swap else (x_ref, y_ref, False)


def _one(ref: Any) -> str | None:
    if isinstance(ref, list) and len(ref) == 1:
        ref = ref[0]
    return ref if isinstance(ref, str) else None


def _numeric(rows: list[list[Any]], i: int) -> bool | None:
    """Every value a number (or NULL); None when all are NULL — unknown."""
    seen = False
    for r in rows:
        v = r[i]
        if v is None:
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return False
        seen = True
    return True if seen else None


def _key(v: Any) -> Any:
    """A dict key for a cell: JSON arrays and objects aren't hashable."""
    if isinstance(v, (list, dict)):
        return ("json", json.dumps(v, sort_keys=True, ensure_ascii=False))
    return v


def _distinct(bases: list[str], *, taken: set[str]) -> list[str]:
    """The names, made distinct from each other and from ``taken``: a name
    already used gets a suffix, ``"1 (2)"``."""
    names: list[str] = []
    used = set(taken)
    for base in bases:
        name, n = base, 2
        while name in used:
            name, n = f"{base} ({n})", n + 1
        used.add(name)
        names.append(name)
    return names


def _merge_orders(first_seen: list[Any], runs: list[list[Any]]) -> list[Any]:
    """One x order that keeps every category's own order.

    A topological sort of "a comes before b in some category", ties broken
    by first appearance; on a contradiction (a cycle), first appearance.
    """
    rank = {k: i for i, k in enumerate(first_seen)}
    after: dict[Any, set[Any]] = {k: set() for k in first_seen}
    indegree = dict.fromkeys(first_seen, 0)
    for run in runs:
        for a, b in itertools.pairwise(run):
            if b not in after[a]:
                after[a].add(b)
                indegree[b] += 1
    ready = [rank[k] for k, d in indegree.items() if d == 0]
    heapq.heapify(ready)
    order: list[Any] = []
    while ready:
        k = first_seen[heapq.heappop(ready)]
        order.append(k)
        for b in after[k]:
            indegree[b] -= 1
            if indegree[b] == 0:
                heapq.heappush(ready, rank[b])
    return order if len(order) == len(first_seen) else first_seen


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
