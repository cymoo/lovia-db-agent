"""The in-memory catalog: lookup, search, the tiered overview, the fingerprint."""

from __future__ import annotations

import re

import pytest

from db_agent.catalog import Catalog, Column, ForeignKey, Table, _mysql_enum


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


def _fk(col: str, table: str, schema: str = "shop") -> ForeignKey:
    return ForeignKey((col,), schema, table, ("id",))


def _shop() -> Catalog:
    """A shop: keyed tables, an enum, a shard family, a big table nothing
    points at, a view."""
    ts = "timestamp with time zone"
    tables = [
        Table(
            "shop",
            "customers",
            "table",
            "会员",
            400,
            [
                Column("id", "integer", False),
                Column("tier", "shop.tier", False, "会员等级", ["bronze", "silver", "gold"]),
            ],
            ["id"],
        ),
        Table(
            "shop",
            "orders",
            "table",
            "订单",
            6000,
            [
                Column("id", "bigint", False),
                Column("customer_id", "integer", False),
                Column("status", "character varying(10)", False),
                Column("created_at", ts, False, "下单时间"),
            ],
            ["id"],
            [_fk("customer_id", "customers")],
        ),
        Table(
            "shop",
            "order_items",
            "table",
            "",
            12000,
            [
                Column("order_id", "bigint", False),
                Column("sku", "text", False),
                Column("quantity", "integer", False),
            ],
            ["order_id", "sku"],
            [_fk("order_id", "orders"), ForeignKey(("sku",), "shop", "products", ("sku",))],
        ),
        Table(
            "shop",
            "products",
            "table",
            "商品",
            60,
            [Column("id", "integer", False), Column("sku", "text", False)],
            ["id"],
        ),
        Table(
            "shop",
            "refunds",
            "table",
            "退款",
            200,
            [Column("id", "integer", False), Column("order_id", "bigint", False)],
            ["id"],
            [_fk("order_id", "orders")],
        ),
        Table(
            "shop",
            "audit_log",
            "table",
            "",
            3_000_000,
            [Column("id", "bigint", False), Column("payload", "jsonb", True)],
        ),
        Table("shop", "daily_sales", "view", "日销售", None, [Column("day", "date", True)]),
    ]
    tables += [
        Table(
            "shop",
            f"events_2025_{m:02d}",
            "table",
            "埋点",
            50_000,
            [Column("at", ts, False), Column("name", "text", False)],
        )
        for m in (10, 11, 12, 1, 2, 3, 4, 5, 6, 7, 8, 9)
    ]
    return Catalog(tables)


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


# ---- the overview, tier by tier -------------------------------------------

_DEFINITIONS = {
    "customers": "shop.customers(id pk, tier enum(bronze|silver|gold) — 会员等级) — 会员",
    "orders": "shop.orders(id pk, customer_id→customers, status varchar(10), "
    "created_at timestamptz — 下单时间) — 订单",
    "order_items": "shop.order_items(order_id pk→orders, sku pk→products.sku, quantity int)",
    "products": "shop.products(id pk, sku text) — 商品",
    "refunds": "shop.refunds(id pk, order_id→orders) — 退款",
    "audit_log": "shop.audit_log(id bigint, payload jsonb)",
    "daily_sales": "shop.daily_sales(day date) — view; 日销售",
    "events": "shop.events_2025_01(at timestamptz, name text) — 埋点; "
    "11 more alike: events_2025_02 … events_2025_12",
}


def test_a_small_schema_is_all_definitions() -> None:
    assert _shop().overview(10_000) == "\n".join(sorted(_DEFINITIONS.values()))


def test_a_tighter_budget_keeps_definitions_for_the_most_important() -> None:
    # By importance: orders (two tables point at it), then customers (one, and
    # a comment) and the event shards (a comment, 600K rows), then the 3M-row
    # audit_log and products (one pointer, a comment, few rows).
    assert _shop().overview(560) == "\n".join(
        [
            _DEFINITIONS["audit_log"],
            _DEFINITIONS["customers"],
            _DEFINITIONS["events"],
            _DEFINITIONS["orders"],
            "more tables (describe before querying): shop.daily_sales (日销售), "
            "shop.order_items, shop.products (商品), shop.refunds (退款)",
        ]
    )


def test_a_small_budget_lists_names_then_a_count() -> None:
    assert _shop().overview(200) == "\n".join(
        [
            "tables (describe before querying): shop.customers (会员), shop.orders (订单)",
            "17 more tables — find them with search_schema",
        ]
    )


def test_no_budget_leaves_a_count() -> None:
    assert _shop().overview(0) == "19 tables — find them with search_schema"
    assert Catalog([]).overview(1000) == "no readable tables"


def test_the_overview_degrades_smoothly() -> None:
    # Many tables: the budget is respected, and more budget never means fewer
    # definitions — no cliff where a schema stops "fitting".
    tables = [
        Table(
            "s",
            f"t{i:03d}x",  # no numeric suffix: not shards
            "table",
            f"表{i}",
            10 ** (i % 7),
            [Column(f"c{j}", "integer", False) for j in range(6)],
            ["c0"],
            [_fk("c1", f"t{i // 10:03d}x", "s")] if i >= 10 else [],
        )
        for i in range(300)
    ]
    cat = Catalog(tables)
    defined = 0
    for budget in range(500, 40_000, 500):
        text = cat.overview(budget)
        lines = text.splitlines()
        assert sum(len(s.encode()) + 3 for s in lines) <= budget
        now = sum(1 for s in lines if "(c0 pk" in s)
        named = sum(s.count(" (表") for s in lines if s.startswith("more tables"))
        tail = re.fullmatch(r"(\d+) more tables — find them with search_schema", lines[-1])
        assert now + named + (int(tail.group(1)) if tail else 0) == 300  # every table counted
        assert now >= defined
        defined = now
    assert defined == 300  # everything fits in the end


def test_shards_need_the_same_columns_and_a_few_members() -> None:
    def shard(name: str, col: str = "a") -> Table:
        return Table("s", name, "table", "", 10, [Column(col, "integer", False)])

    cat = Catalog([shard("log_1"), shard("log_2"), shard("log_3"), shard("log_4", "b")])
    assert cat.overview(10_000).splitlines() == [
        "s.log_1(a int) — 2 more alike: log_2 … log_3",
        "s.log_4(b int)",
    ]
    pair = Catalog([shard("v1"), shard("v2")])
    assert pair.overview(10_000).splitlines() == ["s.v1(a int)", "s.v2(a int)"]


def test_long_definitions_are_cut_short() -> None:
    wide = Table("s", "wide", "table", "", 1, [Column(f"c{i}", "int", True) for i in range(45)])
    labels = [f"v{i}" for i in range(12)]
    many = Table("s", "e", "table", "", 1, [Column("kind", "s.kind", False, "", labels)])
    text = Catalog([wide, many]).overview(10_000)
    assert "c39 int, … 5 more)" in text and "c40" not in text
    assert "kind enum(v0|v1|v2|v3|v4|v5|v6|v7|v8|v9|…2 more)" in text


def test_the_fingerprint_follows_the_schema_not_the_row_counts() -> None:
    before = _shop().fingerprint
    grown = _shop()
    grown.tables[0].est_rows = 10**6
    assert Catalog(grown.tables).fingerprint == before
    changed = _shop()
    changed.tables[0].columns[0].type = "bigint"
    assert Catalog(changed.tables).fingerprint != before
    keyed = _shop()
    keyed.tables[-1].primary_key = ["at"]
    assert Catalog(keyed.tables).fingerprint != before


def test_referrers() -> None:
    cat = _shop()
    orders = cat.find("orders")[0]
    assert sorted(t.name for t, _ in cat.referrers(orders)) == ["order_items", "refunds"]


@pytest.mark.parametrize(
    ("typ", "labels"),
    [
        ("enum('paid','shipped')", ["paid", "shipped"]),
        ("enum('it''s','a,b')", ["it's", "a,b"]),
        ("varchar(10)", []),
    ],
)
def test_mysql_enum_labels(typ: str, labels: list[str]) -> None:
    assert _mysql_enum(typ) == labels
