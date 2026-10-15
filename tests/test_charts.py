"""Chart assembly: split_by, series templates, encode checks, summaries."""

from __future__ import annotations

from typing import Any

import pytest
from lovia import ToolError

from db_agent.charts import apply_split, check_encode, summarize

COLUMNS = ["month", "category", "revenue"]
ROWS = [
    ["2026-01", "数码", 10],
    ["2026-01", "家居", 5],
    ["2026-02", "数码", 12],
    ["2026-03", "家居", 7],
]
LINE = {"type": "line", "encode": {"x": "month", "y": "revenue"}}


def split(
    series: Any,
    rows: list[list[Any]] = ROWS,
    columns: list[str] = COLUMNS,
    split_by: str = "category",
    max_series: int = 5,
    **option: Any,
) -> tuple[dict[str, Any], list[str], list[list[Any]]]:
    return apply_split({**option, "series": series}, columns, rows, split_by, max_series=max_series)


def test_split_makes_wide_rows_and_one_series_per_category() -> None:
    option = {
        "xAxis": {"type": "category"},
        "yAxis": {},
        "series": [{**LINE, "smooth": True}],
    }
    out, columns, rows = apply_split(option, COLUMNS, ROWS, "category", max_series=5)
    assert columns == ["month", "数码", "家居"]
    assert rows == [["2026-01", 10, 5], ["2026-02", 12, None], ["2026-03", None, 7]]
    assert out["series"] == [
        {"type": "line", "smooth": True, "name": "数码", "encode": {"x": "month", "y": "数码"}},
        {"type": "line", "smooth": True, "name": "家居", "encode": {"x": "month", "y": "家居"}},
    ]
    assert option["series"] == [{**LINE, "smooth": True}]  # not mutated


def test_without_encode_three_columns_are_inferred() -> None:
    out, columns, _ = split({"type": "bar", "encode": {}})
    assert columns == ["month", "数码", "家居"]
    assert out["series"][0]["encode"] == {"x": "month", "y": "数码"}
    # A category y axis makes it a horizontal bar.
    out, _, _ = split({"type": "bar"}, yAxis={"type": "category"}, xAxis={})
    assert out["series"][0]["encode"] == {"y": "month", "x": "数码"}


def test_horizontal_bars_come_from_the_encode() -> None:
    # No yAxis type: the numeric column is the value, whichever axis it's on.
    out, columns, _ = split({"type": "bar", "encode": {"y": "month", "x": ["revenue"]}})
    assert columns == ["month", "数码", "家居"]
    assert out["series"][1]["encode"] == {"y": "month", "x": "家居"}


def test_numeric_x_and_y_fall_back_to_the_axes() -> None:
    rows = [[2025, "a", 1], [2026, "a", 2], [2025, "b", 3]]
    cols = ["year", "kind", "n"]
    out, _, wide = split({"type": "bar", "encode": {"x": "year", "y": "n"}}, rows, cols, "kind")
    assert out["series"][0]["encode"] == {"x": "year", "y": "a"}
    assert wide == [[2025, 1, 3], [2026, 2, None]]
    out, _, _ = split(
        {"type": "bar", "encode": {"y": "year", "x": "n"}},
        rows,
        cols,
        "kind",
        yAxis={"type": "category"},
    )
    assert out["series"][0]["encode"] == {"y": "year", "x": "a"}


def test_each_categorys_x_order_is_kept() -> None:
    # ORDER BY category, month: 家居 first, missing February.
    rows = [
        ["家居", "2026-01", 5],
        ["家居", "2026-03", 7],
        ["数码", "2026-01", 10],
        ["数码", "2026-02", 12],
        ["数码", "2026-03", 9],
    ]
    _, _, wide = split(LINE, rows, ["category", "month", "revenue"])
    assert [r[0] for r in wide] == ["2026-01", "2026-02", "2026-03"]
    assert wide[1] == ["2026-02", None, 12]


def test_contradicting_orders_fall_back_to_first_appearance() -> None:
    # Ordered by revenue: each category orders the months its own way.
    rows = [["a", "m1", 3], ["a", "m2", 2], ["b", "m2", 9], ["b", "m1", 1]]
    _, _, wide = split(LINE | {"encode": {"x": "m", "y": "v"}}, rows, ["c", "m", "v"], "c")
    assert [r[0] for r in wide] == ["m1", "m2"]


def test_category_names_are_distinct_and_null_is_labeled() -> None:
    rows = [["2026-01", "month", 1], ["2026-01", None, 2], ["2026-01", "(null)", 3]]
    out, columns, wide = split(LINE, rows)
    assert columns == ["month", "month (2)", "(null)", "(null) (2)"]
    assert wide == [["2026-01", 1, 2, 3]]
    # The legend shows the categories as they are.
    assert [s["name"] for s in out["series"]] == ["month", "(null)", "(null)"]
    assert [s["encode"]["y"] for s in out["series"]] == columns[1:]
    check_encode(out, columns)


def test_duplicate_pairs_are_refused() -> None:
    rows = [*ROWS, ["2026-01", "数码", 3]]
    with pytest.raises(ToolError, match="more than one row") as exc:
        split(LINE, rows)
    assert "GROUP BY month, category" in exc.value.hint
    with pytest.raises(ToolError, match="more than one row") as exc:
        split({"type": "scatter", "encode": {"x": "month", "y": "revenue"}}, rows)
    assert "drop split_by" in exc.value.hint


@pytest.mark.parametrize(
    ("split_by", "series", "match", "hint"),
    [
        ("nope", LINE, "'nope'", "month, category, revenue"),
        ("category", [LINE, LINE], "exactly one series, not 2", "drop split_by"),
        ("category", [], "one series as the template", "repeated"),
        ("category", {"type": "pie"}, "pie", "itemName"),
        ("category", [{"type": "pie"}, {"type": "pie"}], "pie", "itemName"),
        ("category", {"type": "radar"}, "'radar'", "line, bar or scatter"),
        ("category", {"type": "line", "encode": {"x": "month"}}, "one x and one y", "encode"),
        ("category", {"type": "line", "encode": {"x": "month", "y": "nope"}}, "'nope'", "month"),
        (
            "category",
            {"type": "line", "encode": {"x": "month", "y": ["revenue", "month"]}},
            "one x and one y",
            "several measures",
        ),
        (
            "category",
            {"type": "line", "encode": {"x": "category", "y": "revenue"}},
            "split_by column",
            "other two",
        ),
        ("category", {"type": "line", "encode": {"x": "month", "y": "month"}}, "both", "encode"),
    ],
)
def test_split_errors(split_by: str, series: Any, match: str, hint: str) -> None:
    with pytest.raises(ToolError, match=match) as exc:
        split(series, split_by=split_by)
    assert hint in exc.value.hint


@pytest.mark.parametrize(
    ("columns", "rows", "match"),
    [
        (["month", "category", "revenue", "orders"], [["m", "a", 1, 2]], "with 4 columns"),
        (["month", "category", "revenue"], [["m", "a", "1"]], "neither month nor revenue"),
        (["year", "category", "revenue"], [[2026, "a", 1]], "both year and revenue"),
    ],
)
def test_encode_is_inferred_only_when_unambiguous(
    columns: list[str], rows: list[list[Any]], match: str
) -> None:
    with pytest.raises(ToolError, match=match) as exc:
        split({"type": "line"}, rows, columns)
    assert '"x": "month"' in exc.value.hint


def test_split_caps_the_number_of_series() -> None:
    with pytest.raises(ToolError, match="the limit is 1") as exc:
        split(LINE, max_series=1)
    assert "other" in exc.value.hint


def test_check_encode_names_real_columns() -> None:
    check_encode({"series": [{"encode": {"x": "month", "y": ["revenue"]}}]}, COLUMNS)
    with pytest.raises(ToolError, match="'sales'") as exc:
        check_encode({"series": {"encode": {"x": "month", "y": "sales"}}}, COLUMNS)
    assert "month, category, revenue" in exc.value.hint


def test_summarize() -> None:
    s = summarize(COLUMNS, ROWS)
    assert s["rows"] == 4
    assert s["stats"] == {"revenue": {"min": 5, "max": 12, "sum": 34, "nulls": 0}}
    assert s["first_row"] == ROWS[0] and s["last_row"] == ROWS[-1]
