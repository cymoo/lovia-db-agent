# lovia-db-agent

用自然语言提问你的 PostgreSQL / MySQL 数据库。agent 自己找表、写只读 SQL，用**交互表格**和
**ECharts 图表**回答——数据直接来自查询结果，模型不会转写任何一个数字。基于
[lovia](https://github.com/cymoo/lovia) 及其 Web UI。

[English](README.md)

## 特点

- **不依赖表的数量。** 系统提示里按 token 预算放尽可能多的表，其余用 `search_schema` 按表名、
  列名、注释搜索（中文注释也能搜）。10 张表和 1 万张表是同一套流程。
- **不依赖结果的大小。** 大结果只让模型看 20 行预览和每列的统计值，用户的表格里能翻看全部
  （最多 1 万行），支持排序、筛选、复制和 CSV。点很多的图表也一样。
- **用 SQL 出图。** 模型只写图表结构，数据由查询结果填入；`pivot` 能把
  `(month, category, revenue)` 这样的长表变成每个品类一条线。
- **三重只读。** 只读账号（需要你来配，见下文）；每个连接都是只读会话并带语句超时；
  SQL 检查拒绝写操作，并给最外层加 `LIMIT`。
- **业务口径**写成普通的 `SKILL.md` 文件，agent 需要时会去读。

## 快速开始（演示库）

```bash
git clone https://github.com/cymoo/lovia-db-agent && cd lovia-db-agent
uv venv && uv pip install -e .            # 或 pip install -e .

createdb demo_shop && psql demo_shop -f examples/demo_postgres.sql
cp config.example.yaml config.yaml
cp .env.example .env                       # 填 LOVIA_MODEL（及 API key）和 DB_DEMO_URL

.venv/bin/lovia-db-agent                   # → http://127.0.0.1:8000
```

可以试试："每个月的销售额走势"、"退款最多的商品 Top 10"、"最近的订单"。
MySQL：`mysql -u root < examples/demo_mysql.sql`，连接串用 `mysql+asyncmy://`。

## 配置

`config.yaml`（见 [config.example.yaml](config.example.yaml)）给每个连接起名字，连接串放在
环境变量里：配置文件里没有密码，模型也看不到。

```yaml
connections:
  sales:
    url_env: DB_SALES_URL     # postgresql+asyncpg://... | mysql+asyncmy://...
    description: 订单、商品、门店
    timezone: Asia/Shanghai
instructions: 用中文回答。
skills_dir: skills
```

在本机以外提供服务时需要设置 `LOVIA_WEB_TOKEN`（或放在你自己的鉴权后面）；
`--host` / `--port` / `--config` 是命令行参数。

## 数据库侧准备

这里才是真正的安全边界，请认真做：

1. 专用账号，**只授 SELECT**，最好连只读副本。
2. 用**视图**把敏感列挡在外面：建一个 schema（MySQL 用一个 database），只放 agent 可以看的
   视图，只把它授权给这个账号。这样表发现、查询、样例数据看到的都是同一份内容。
   在 SQL 层打码是可以绕过的（`SELECT phone AS p`），所以 agent 不假装做这件事。
3. PostgreSQL：`ALTER ROLE agent_ro SET default_transaction_read_only = on;`

## 工作方式

| 工具 | 给谁看 | 返回 |
| --- | --- | --- |
| `search_schema` | 模型 | 匹配的表和列 |
| `describe_tables` | 模型 | 列、主外键、索引、3 行样例 |
| `query` | 模型 | 最多 50 行文本 |
| `query_table` | 用户 | 表格（`ui_result("table")`；结果大时模型只看预览） |
| `query_chart` | 用户 | 由查询结果绘制的 ECharts 图（`pivot` 按类别拆成多条序列） |

另外还有 lovia 自带的 `show_chart`（用于单个查询拿不到的数据）和 `now`。

大结果不进模型上下文：lovia 的 `put_ui_data` 把完整数据随聊天保存（删除聊天时一并删除），
模型只看预览和各列统计，页面用 lovia 内置的表格和图表画出全部数据，聊天里和导出的 HTML 中都是。

所有等待都有上限：建立连接、等连接池、每条语句。数据库慢或挂掉时，模型收到的是带提示的错误，
而不是一直卡住。`describe_tables` 对每张表取样都带 `LIMIT` 和 3 秒上限，时间不够时只放弃主外键和
样例，不会让整次调用失败。Python 表示不了的值（PostgreSQL 的 `infinity`、公元前日期、一万年以后）
以文本返回。

页面带有 Content-Security-Policy，不加载外部图片：即使数据库里埋了恶意文本，也没法让回复
借图片地址把数据带出去。

## 开发

```bash
uv pip install -e ".[dev]"
pytest                                     # 单元测试
DB_AGENT_TEST_PG_URL=postgresql+asyncpg://agent_ro:agent_ro@localhost/demo_shop pytest
DB_AGENT_TEST_MYSQL_URL=mysql+asyncmy://agent_ro:agent_ro@127.0.0.1:3306/demo_shop pytest
ruff check . && ruff format --check . && mypy
```

CI 会在 PostgreSQL 16 和 MySQL 8.4 容器上跑集成测试。

## 许可证

MIT
