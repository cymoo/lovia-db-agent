---
name: shop-metrics
description: 演示网店（shop 库）的指标口径：销售额、净销售额、订单数、客单价、退款率，以及订单/明细/退款的关联方式。回答任何销售类问题前先读。
---

# 演示网店的指标口径

这是一个示例 skill：把你们自己的业务定义写成这样的文件，agent 会在需要时读取。

## 表之间的关系

- `shop.orders` 一行一个订单；`shop.order_items` 一行一个订单里的一种商品（`order_id` → `orders.id`）。
- `shop.refunds.order_id` → `orders.id`；一个订单最多一笔退款。
- 金额都在 `order_items` 上：`quantity * unit_price`。`products.price` 是标价，不是成交价。

## 指标

| 指标 | 定义 |
| --- | --- |
| 销售额（GMV） | `sum(order_items.quantity * order_items.unit_price)`，**排除** `orders.status = 'cancelled'` |
| 净销售额 | 销售额 − `sum(refunds.amount)`（按退款发生时间 `refunds.created_at` 归属） |
| 订单数 | `count(DISTINCT orders.id)`，排除已取消 |
| 客单价 | 销售额 / 订单数 |
| 退款率 | 有退款的订单数 / 已发货（`shipped`）订单数 |

## 注意

- 时间口径默认用下单时间 `orders.created_at`；"本月""上周"按会话时区（Asia/Shanghai）计算。
- 用户没说清楚"销售额"含不含退款时，用上面的默认口径，并在回答里写明。
