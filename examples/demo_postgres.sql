-- A small demo database for lovia-db-agent: a shop with customers, products,
-- orders, order items, refunds and site visits over 2025-2026, plus a
-- SELECT-only role.
--
--   createdb demo_shop
--   psql demo_shop -f examples/demo_postgres.sql
--   export DB_DEMO_URL=postgresql+asyncpg://agent_ro:agent_ro@localhost/demo_shop
--
-- Re-runnable: it drops and recreates the `shop` schema.

DROP SCHEMA IF EXISTS shop CASCADE;
CREATE SCHEMA shop;
COMMENT ON SCHEMA shop IS '演示用网店数据';

SELECT setseed(0.42);

CREATE TYPE shop.customer_tier AS ENUM ('bronze', 'silver', 'gold');

CREATE TABLE shop.customers (
    id          integer PRIMARY KEY,
    name        text NOT NULL,
    city        text NOT NULL,
    tier        shop.customer_tier NOT NULL,
    created_at  timestamptz NOT NULL
);
COMMENT ON TABLE shop.customers IS '会员';
COMMENT ON COLUMN shop.customers.city IS '所在城市';
COMMENT ON COLUMN shop.customers.tier IS '会员等级';

CREATE TABLE shop.products (
    id        integer PRIMARY KEY,
    name      text NOT NULL,
    category  text NOT NULL,
    price     numeric(10, 2) NOT NULL
);
COMMENT ON TABLE shop.products IS '商品';
COMMENT ON COLUMN shop.products.category IS '品类';
COMMENT ON COLUMN shop.products.price IS '标价（元）';

CREATE TABLE shop.orders (
    id           bigint PRIMARY KEY,
    customer_id  integer NOT NULL REFERENCES shop.customers (id),
    status       text NOT NULL,
    created_at   timestamptz NOT NULL
);
COMMENT ON TABLE shop.orders IS '订单';
COMMENT ON COLUMN shop.orders.status IS '订单状态：paid / shipped / cancelled';
COMMENT ON COLUMN shop.orders.created_at IS '下单时间';
CREATE INDEX orders_created_at ON shop.orders (created_at);

CREATE TABLE shop.order_items (
    order_id    bigint NOT NULL REFERENCES shop.orders (id),
    product_id  integer NOT NULL REFERENCES shop.products (id),
    quantity    integer NOT NULL,
    unit_price  numeric(10, 2) NOT NULL,
    PRIMARY KEY (order_id, product_id)
);
COMMENT ON TABLE shop.order_items IS '订单明细';
COMMENT ON COLUMN shop.order_items.unit_price IS '成交单价（元）';

CREATE TABLE shop.refunds (
    id          integer PRIMARY KEY,
    order_id    bigint NOT NULL REFERENCES shop.orders (id),
    amount      numeric(10, 2) NOT NULL,
    reason      text,
    created_at  timestamptz NOT NULL
);
COMMENT ON TABLE shop.refunds IS '退款';
COMMENT ON COLUMN shop.refunds.amount IS '退款金额（元）';

-- Partitioned by year, 2026 again by half-year: every partition rolls up into
-- one table for the agent.
CREATE TABLE shop.visits (
    customer_id  integer REFERENCES shop.customers (id),
    page         text NOT NULL,
    visited_at   timestamptz NOT NULL
) PARTITION BY RANGE (visited_at);
CREATE TABLE shop.visits_2025 PARTITION OF shop.visits
    FOR VALUES FROM ('2025-01-01') TO ('2026-01-01');
CREATE TABLE shop.visits_2026 PARTITION OF shop.visits
    FOR VALUES FROM ('2026-01-01') TO ('2027-01-01') PARTITION BY RANGE (visited_at);
CREATE TABLE shop.visits_2026_h1 PARTITION OF shop.visits_2026
    FOR VALUES FROM ('2026-01-01') TO ('2026-07-01');
CREATE TABLE shop.visits_2026_h2 PARTITION OF shop.visits_2026
    FOR VALUES FROM ('2026-07-01') TO ('2027-01-01');
COMMENT ON TABLE shop.visits IS '访问记录';
COMMENT ON COLUMN shop.visits.customer_id IS '会员；空为未登录访客';
COMMENT ON COLUMN shop.visits.page IS '页面：home / product / cart / checkout';

INSERT INTO shop.customers
SELECT i,
       'customer ' || i,
       (ARRAY['上海', '北京', '深圳', '杭州', '成都', '武汉'])[1 + floor(random() * 6)::int],
       (ARRAY['bronze', 'bronze', 'bronze', 'silver', 'silver', 'gold'])[1 + floor(random() * 6)::int]::shop.customer_tier,
       timestamptz '2024-01-01' + random() * interval '700 days'
FROM generate_series(1, 400) AS i;

INSERT INTO shop.products
SELECT i,
       (ARRAY['手机', '耳机', '平板', '笔记本', '显示器', '键盘', '鼠标', '台灯', '书桌', '椅子'])[1 + (i - 1) % 10]
         || ' ' || chr(65 + (i - 1) / 10),
       (ARRAY['数码', '数码', '数码', '电脑', '电脑', '外设', '外设', '家居', '家居', '家居'])[1 + (i - 1) % 10],
       round((20 + random() * 3000)::numeric, 2)
FROM generate_series(1, 60) AS i;

INSERT INTO shop.orders
SELECT i,
       1 + floor(random() * 400)::int,
       (ARRAY['paid', 'shipped', 'shipped', 'shipped', 'cancelled'])[1 + floor(random() * 5)::int],
       -- Skewed so that later months have more orders.
       timestamptz '2025-01-01'
         + (power(random(), 0.8) * 650) * interval '1 day'
         + random() * interval '1 day'
FROM generate_series(1, 6000) AS i;

-- One to three products per order; `0 * o.id` makes the subquery run per order.
INSERT INTO shop.order_items
SELECT o.id, p.id, 1 + floor(random() * 3)::int, round(p.price * (0.8 + random() * 0.2)::numeric, 2)
FROM shop.orders o
CROSS JOIN LATERAL (
    SELECT id, price FROM shop.products
    ORDER BY random() + 0 * o.id
    LIMIT 1 + o.id % 3
) p;

INSERT INTO shop.refunds
SELECT row_number() OVER (),
       o.id,
       round((SELECT sum(quantity * unit_price) FROM shop.order_items WHERE order_id = o.id) * 0.5, 2),
       (ARRAY['质量问题', '不想要了', '发错货', NULL])[1 + floor(random() * 4)::int],
       o.created_at + interval '5 days'
FROM shop.orders o
WHERE o.status = 'shipped' AND random() < 0.06
  AND EXISTS (SELECT 1 FROM shop.order_items WHERE order_id = o.id);

INSERT INTO shop.visits
SELECT CASE WHEN random() < 0.7 THEN 1 + floor(random() * 400)::int END,
       (ARRAY['home', 'home', 'home', 'product', 'product', 'cart', 'checkout'])[1 + floor(random() * 7)::int],
       timestamptz '2025-01-01' + random() * interval '650 days'
FROM generate_series(1, 20000);

ANALYZE shop.customers, shop.products, shop.orders, shop.order_items, shop.refunds, shop.visits;

-- The agent's account: SELECT on this schema, read-only by default.
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'agent_ro') THEN
        CREATE ROLE agent_ro LOGIN PASSWORD 'agent_ro';
    END IF;
END $$;
ALTER ROLE agent_ro SET default_transaction_read_only = on;
GRANT USAGE ON SCHEMA shop TO agent_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA shop TO agent_ro;
