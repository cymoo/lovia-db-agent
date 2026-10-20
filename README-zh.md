# lovia-db-agent

用自然语言提问你的 PostgreSQL / MySQL 数据库。agent 自己找表、写只读 SQL，用**交互表格**和
**ECharts 图表**回答——数据直接来自查询结果，模型不会转写任何一个数字。基于
[lovia](https://github.com/cymoo/lovia) 及其 Web UI。

[English](README.md)

## 特点

- **不依赖表的数量。** 系统提示里直接放表结构，按 token 预算放。小库整个放进去——列、主外键、
  枚举值、注释——agent 可以直接写 SQL；大库里被引用最多的表带上列，更多的表只列名字，其余用
  `search_schema` 按表名、列名、注释搜索（中文注释也能搜）。`log_202401`、`log_202402`……
  这样的分表只算一张。从 10 张表到 1 万张表是同一个算法，中间没有断崖。
- **不依赖结果的大小。** 大结果只让模型看 20 行预览和每列的统计值，用户的表格里能翻看全部
  （最多 1 万行），支持排序、筛选、复制和 CSV。点很多的图表也一样。
- **用 SQL 出图。** 模型只写图表结构，数据由查询结果填入；`split_by` 能把
  `(month, category, revenue)` 这样的长表变成每个品类一条线。
- **多重只读。** 只读账号（需要你来配，见下文）；每条查询都带语句超时；SQL 检查拒绝写操作，
  并给最外层加 `LIMIT`。MySQL 的会话本身也是只读的。
- **业务口径**写成普通的 `SKILL.md` 文件，agent 需要时会去读。
- **记忆和工作区。** 你要求或确认后，agent 跨对话记住偏好和口径（可在“记忆”页查看和修改），
  并在一个文件目录里读写文件（页面的文件面板）。它也能自己画表格，或用 `show_html`
  做交互页面，比如假设分析计算器。

## 快速开始（演示库）

```bash
git clone https://github.com/cymoo/lovia-db-agent && cd lovia-db-agent
uv venv && uv pip install -e .            # 或 pip install -e .

createdb demo_shop && psql demo_shop -f examples/demo_postgres.sql
cp config.example.yaml config.yaml
cp .env.example .env                       # 填 LOVIA_MODEL（及 API key）和 DB_DEMO_URL

.venv/bin/lovia-db-agent check             # 逐个连接数据库，报告看到了什么
.venv/bin/lovia-db-agent                   # → http://127.0.0.1:8000
```

可以试试："每个月的销售额走势"、"退款最多的商品 Top 10"、"最近的订单"。
MySQL：`mysql -u root < examples/demo_mysql.sql`，连接串用 `mysql+asyncmy://`。
支持 PostgreSQL 11 及以上、MySQL 8。不支持 MariaDB：它没有 `max_execution_time`，无法给每条查询限时。

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
memory: true                  # 跨对话的记忆，存于 <data_dir>/memory
workspace:
  mode: coding                # 文件在 <data_dir>/workspace；shell 命令需要你批准
```

相对路径以配置文件所在目录为准。`.env` 从运行命令的目录读取。

| 命令 | 作用 |
| --- | --- |
| `lovia-db-agent [serve]` | 启动聊天界面。参数 `--config`、`--host`、`--port`（或环境变量 `DB_AGENT_CONFIG` / `DB_AGENT_HOST` / `DB_AGENT_PORT`） |
| `lovia-db-agent check` | 按 agent 的方式连接每个数据库，报告版本、能读到多少张表、会话能否写入；出错时给出错误和提示 |

在本机以外提供服务需要登录令牌：设置 `LOVIA_WEB_TOKEN`，否则启动时自动生成并打印带
`/?token=...` 的链接。也可以放在你自己的鉴权后面。

记忆是所有用户共用的一份笔记，工作区是所有对话共用的一个目录。记忆里只有 agent 被要求记住的内容：
对话不会被归档或自动提取事实，所以查询结果不会进入记忆。shell 命令只拿到最小的环境变量
（没有数据库连接串和 API key），任何工具都不能打开 `.env` 文件。

## 数据库侧准备

这里才是真正的安全边界，请认真做：

1. 专用账号，**只授 SELECT**，最好连只读副本。
2. 用**视图**把敏感列挡在外面：建一个 schema（MySQL 用一个 database），只放 agent 可以看的
   视图，只把它授权给这个账号。这样表发现、查询、样例数据看到的都是同一份内容。
   在 SQL 层打码是可以绕过的（`SELECT phone AS p`），所以 agent 不假装做这件事。
3. PostgreSQL：`ALTER ROLE agent_ro SET default_transaction_read_only = on;`
   应用连接时不发送会话参数（PgBouncer 等连接池会拒绝；每条语句用 `SET LOCAL` 限时），
   所以会话只读靠的就是这条角色设置。`lovia-db-agent check` 会在会话可写时提醒。

## 工作方式

| 工具 | 给谁看 | 返回 |
| --- | --- | --- |
| `search_schema` | 模型 | 匹配的表和列 |
| `describe_tables` | 模型 | 列、主外键、索引、常见值、3 行样例 |
| `query` | 模型 | 最多 50 行文本，以及耗时 |
| `query_table` | 用户 | 表格（`ui_result("table")`；结果大时模型只看预览） |
| `query_chart` | 用户 | 由查询结果绘制的 ECharts 图（`split_by` 按类别拆成多条序列） |

另外还有 lovia 自带的 `show_table` 和 `show_chart`（用于单个查询拿不到的数据）、
`show_html`（沙箱中运行、不能联网的交互页面）、`now`，工作区的文件和 shell 工具，以及记忆的 `remember` / `forget`。

表结构用几条批量的系统表查询读出（表、列、主外键），和表的数量无关，每 10 分钟在后台刷新。
系统提示里只有元数据，没有任何数据值；只在表结构变化时才变（行数估计会变，文本不变），
所以模型服务商的提示缓存一直有效。大小由 `limits.catalog_budget_tokens`（默认 6000）控制。
`describe_tables` 的常见值来自 PostgreSQL 的统计信息（`pg_stats`），描述一张表不会扫表；
MySQL 的枚举值本就在列类型里，其余看样例行。

大结果不进模型上下文：lovia 的 `put_ui_data` 把完整数据随聊天保存（删除聊天时一并删除），
模型只看预览和各列统计，页面用 lovia 内置的表格和图表画出全部数据，聊天里和导出的 HTML 中都是。

所有等待都有上限：建立连接、等连接池、每条语句。数据库慢或挂掉时，模型收到的是带提示的错误，
而不是一直卡住。`describe_tables` 对每张表取样都带 `LIMIT` 和 3 秒上限，时间不够时只放弃索引、常见值和
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

CI 会在 PostgreSQL 16、PostgreSQL 11（支持的最低版本）和 MySQL 8.4 容器上跑集成测试。

从 lovia 0.9.87 及更早版本升级，见 [docs/upgrade-lovia-0.9.96-zh.md](docs/upgrade-lovia-0.9.96-zh.md)。

## 许可证

MIT
