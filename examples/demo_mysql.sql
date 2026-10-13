-- The demo shop for MySQL 8: the same tables as demo_postgres.sql.
--
--   mysql -u root < examples/demo_mysql.sql
--   export DB_DEMO_URL=mysql+asyncmy://agent_ro:agent_ro@localhost:3306/demo_shop
--
-- Re-runnable: it drops and recreates the `demo_shop` database.

DROP DATABASE IF EXISTS demo_shop;
CREATE DATABASE demo_shop CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE demo_shop;
SET SESSION cte_max_recursion_depth = 100000;

CREATE TABLE customers (
    id          INT PRIMARY KEY,
    name        VARCHAR(100) NOT NULL,
    city        VARCHAR(20) NOT NULL COMMENT '所在城市',
    tier        VARCHAR(10) NOT NULL COMMENT '会员等级：bronze / silver / gold',
    created_at  DATETIME NOT NULL
) COMMENT '会员';

CREATE TABLE products (
    id        INT PRIMARY KEY,
    name      VARCHAR(100) NOT NULL,
    category  VARCHAR(20) NOT NULL COMMENT '品类',
    price     DECIMAL(10, 2) NOT NULL COMMENT '标价（元）'
) COMMENT '商品';

CREATE TABLE orders (
    id           BIGINT PRIMARY KEY,
    customer_id  INT NOT NULL,
    status       VARCHAR(10) NOT NULL COMMENT '订单状态：paid / shipped / cancelled',
    created_at   DATETIME NOT NULL COMMENT '下单时间',
    INDEX orders_created_at (created_at),
    FOREIGN KEY (customer_id) REFERENCES customers (id)
) COMMENT '订单';

CREATE TABLE order_items (
    order_id    BIGINT NOT NULL,
    product_id  INT NOT NULL,
    quantity    INT NOT NULL,
    unit_price  DECIMAL(10, 2) NOT NULL COMMENT '成交单价（元）',
    PRIMARY KEY (order_id, product_id),
    FOREIGN KEY (order_id) REFERENCES orders (id),
    FOREIGN KEY (product_id) REFERENCES products (id)
) COMMENT '订单明细';

CREATE TABLE refunds (
    id          INT PRIMARY KEY,
    order_id    BIGINT NOT NULL,
    amount      DECIMAL(10, 2) NOT NULL COMMENT '退款金额（元）',
    reason      VARCHAR(50),
    created_at  DATETIME NOT NULL,
    FOREIGN KEY (order_id) REFERENCES orders (id)
) COMMENT '退款';

INSERT INTO customers
WITH RECURSIVE n (i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 400)
SELECT i,
       CONCAT('customer ', i),
       ELT(1 + FLOOR(RAND(i) * 6), '上海', '北京', '深圳', '杭州', '成都', '武汉'),
       ELT(1 + FLOOR(RAND(i + 7) * 6), 'bronze', 'bronze', 'bronze', 'silver', 'silver', 'gold'),
       TIMESTAMP('2024-01-01') + INTERVAL FLOOR(RAND(i + 13) * 700 * 86400) SECOND
FROM n;

INSERT INTO products
WITH RECURSIVE n (i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 60)
SELECT i,
       CONCAT(ELT(1 + (i - 1) % 10, '手机', '耳机', '平板', '笔记本', '显示器', '键盘', '鼠标', '台灯', '书桌', '椅子'),
              ' ', CHAR(65 + (i - 1) DIV 10)),
       ELT(1 + (i - 1) % 10, '数码', '数码', '数码', '电脑', '电脑', '外设', '外设', '家居', '家居', '家居'),
       ROUND(20 + RAND(i) * 3000, 2)
FROM n;

INSERT INTO orders
WITH RECURSIVE n (i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 6000)
SELECT i,
       1 + FLOOR(RAND(i) * 400),
       ELT(1 + FLOOR(RAND(i + 1) * 5), 'paid', 'shipped', 'shipped', 'shipped', 'cancelled'),
       -- Skewed so that later months have more orders.
       TIMESTAMP('2025-01-01') + INTERVAL FLOOR(POW(RAND(i + 2), 0.8) * 650 * 86400) SECOND
FROM n;

-- One to three products per order.
INSERT INTO order_items
SELECT o.id, p.id, 1 + FLOOR(RAND(o.id * 61 + p.id) * 3), ROUND(p.price * (0.8 + RAND(o.id + p.id) * 0.2), 2)
FROM orders o
JOIN products p ON p.id IN (1 + o.id % 60, 1 + (o.id * 7 + 3) % 60, 1 + (o.id * 13 + 5) % 60)
WHERE p.id = 1 + o.id % 60
   OR (o.id % 3 >= 1 AND p.id = 1 + (o.id * 7 + 3) % 60)
   OR (o.id % 3 = 2 AND p.id = 1 + (o.id * 13 + 5) % 60);

INSERT INTO refunds
SELECT ROW_NUMBER() OVER (ORDER BY o.id),
       o.id,
       ROUND((SELECT SUM(quantity * unit_price) FROM order_items WHERE order_id = o.id) * 0.5, 2),
       ELT(1 + FLOOR(RAND(o.id) * 4), '质量问题', '不想要了', '发错货', NULL),
       o.created_at + INTERVAL 5 DAY
FROM orders o
WHERE o.status = 'shipped' AND RAND(o.id * 3) < 0.06;

ANALYZE TABLE customers, products, orders, order_items, refunds;

-- The agent's account: SELECT on this database only.
CREATE USER IF NOT EXISTS 'agent_ro'@'%' IDENTIFIED BY 'agent_ro';
GRANT SELECT ON demo_shop.* TO 'agent_ro'@'%';
