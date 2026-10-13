"""The in-memory catalog: lookup, search, and the budgeted overview."""

from __future__ import annotations

from db_agent.catalog import Catalog, Column, Table


def _catalog() -> Catalog:
    return Catalog(
        [
            Table(
                "shop",
                "orders",
                "table",
                "订单",
                6000,
                [
                    Column("id", "bigint", False),
                    Column("created_at", "timestamptz", False, "下单时间"),
                ],
            ),
            Table(
                "shop",
                "refunds",
                "table",
                "退款",
                200,
                [
                    Column("order_id", "bigint", False),
                    Column("amount", "numeric", False, "退款金额"),
                ],
            ),
            Table("audit", "orders", "view", "", None, [Column("id", "bigint", True)]),
        ]
    )


def test_find_by_name_or_qualified_name() -> None:
    cat = _catalog()
    assert [t.qualified for t in cat.find("shop.orders")] == ["shop.orders"]
    assert [t.qualified for t in cat.find("REFUNDS")] == ["shop.refunds"]
    assert len(cat.find("orders")) == 2  # ambiguous across schemas
    assert cat.find("nope") == []


def test_search_matches_names_columns_and_comments() -> None:
    cat = _catalog()
    hits = cat.search("退款", 5)
    assert [t.qualified for t, _ in hits] == ["shop.refunds"]
    assert [c.name for c in hits[0][1]] == ["amount"]  # the column comment matched too
    assert [t.qualified for t, _ in cat.search("下单时间", 5)] == ["shop.orders"]
    assert cat.search("zzz", 5) == []


def test_search_star_lists_the_largest_tables() -> None:
    assert [t.qualified for t, _ in _catalog().search("*", 2)] == ["shop.orders", "shop.refunds"]


def test_overview_lists_everything_within_budget() -> None:
    text = _catalog().overview(1000)
    assert text == "tables: audit.orders, shop.orders (订单), shop.refunds (退款)"


def test_overview_over_budget_keeps_the_largest_and_points_to_search() -> None:
    cat = Catalog([Table("s", f"t{i:03d}", "table", "表", i) for i in range(200)])
    text = cat.overview(300)
    assert text.startswith("200 tables in s;") and "search_schema" in text
    assert "s.t199 (表), s.t198 (表)" in text and "s.t000" not in text  # largest first
    assert len(text.encode()) <= 300
