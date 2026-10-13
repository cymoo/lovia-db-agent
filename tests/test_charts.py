"""Chart assembly: pivoting, series templates, encode checks, summaries."""

from __future__ import annotations

import pytest
from lovia import ToolError

from db_agent.charts import apply_pivot, check_encode, summarize

COLUMNS = ["month", "category", "revenue"]
ROWS = [
    ["2026-01", "数码", 10],
    ["2026-01", "家居", 5],
    ["2026-02", "数码", 12],
    ["2026-03", "家居", 7],
]
PIVOT = {"index": "month", "columns": "category", "values": "revenue"}


def test_pivot_makes_wide_rows_and_one_series_per_category() -> None:
    option = {
        "xAxis": {"type": "category"},
        "yAxis": {},
        "series": [{"type": "line", "smooth": True}],
    }
    out, columns, rows = apply_pivot(option, COLUMNS, ROWS, PIVOT, max_series=5)
    assert columns == ["month", "数码", "家居"]
    assert rows == [["2026-01", 10, 5], ["2026-02", 12, None], ["2026-03", None, 7]]
    assert out["series"] == [
        {"type": "line", "smooth": True, "name": "数码", "encode": {"x": "month", "y": "数码"}},
        {"type": "line", "smooth": True, "name": "家居", "encode": {"x": "month", "y": "家居"}},
    ]
    assert option["series"] == [{"type": "line", "smooth": True}]  # not mutated


def test_pivot_on_a_category_y_axis_swaps_the_encode() -> None:
    option = {"yAxis": {"type": "category"}, "xAxis": {}, "series": {"type": "bar"}}
    out, _, _ = apply_pivot(option, COLUMNS, ROWS, PIVOT, max_series=5)
    assert out["series"][0]["encode"] == {"y": "month", "x": "数码"}


@pytest.mark.parametrize(
    ("pivot", "option", "match"),
    [
        ({"index": "month"}, {"series": [{"type": "line"}]}, "exactly: index"),
        ({**PIVOT, "values": "nope"}, {"series": [{"type": "line"}]}, "doesn't return"),
        (PIVOT, {"series": [{"type": "line"}, {"type": "bar"}]}, "exactly one series"),
    ],
)
def test_pivot_errors(pivot: dict, option: dict, match: str) -> None:
    with pytest.raises(ToolError, match=match):
        apply_pivot(option, COLUMNS, ROWS, pivot, max_series=5)


def test_pivot_caps_the_number_of_series() -> None:
    with pytest.raises(ToolError, match="the limit is 1") as exc:
        apply_pivot({"series": [{"type": "line"}]}, COLUMNS, ROWS, PIVOT, max_series=1)
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
