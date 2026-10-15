# AGENTS.md

## 项目概览

`lovia-db-agent` 是面向 PostgreSQL / MySQL 的自然语言数据分析应用。用户通过聊天提问，
Agent 发现表结构、生成只读 SQL，再将查询结果直接呈现为交互表格或 ECharts 图表。

项目使用 Python 3.10+，基于 lovia 的 Agent、插件和聊天 UI，Web 应用为 FastAPI。
数据库层使用 SQLAlchemy 异步引擎及 asyncpg / asyncmy，SQL 检查使用 sqlglot。
连接名称、查询限制和业务技能目录通过 YAML 配置，连接 URL 从环境变量读取。

核心是 `SQLDatabase` 插件：`search_schema`、`describe_tables`、`query` 供模型探索，
`query_table`、`query_chart` 将结果展示给用户。大结果由聊天存储保存，模型只接收预览或摘要。

## 按任务定位代码

以下路径均相对于仓库根目录；从相关入口及其测试开始阅读。

| 任务 | 主要入口 |
| --- | --- |
| 启动、配置、Agent 组装 | `db_agent/__main__.py`、`db_agent/config.py`、`db_agent/app.py` |
| 工具接口、提示词、结果封装 | `db_agent/plugin.py` 中的 `SQLDatabase` |
| 表结构发现、搜索、描述 | `db_agent/catalog.py` |
| SQL 检查、执行、连接池和数据库差异 | `db_agent/guard.py`、`db_agent/execute.py` |
| 数据转换、图表 pivot、结果统计 | `db_agent/convert.py`、`db_agent/charts.py`、`db_agent/stats.py` |
| 页面定制与图片来源 CSP | `db_agent/templates/index.html`；页面主体和组件由 lovia 提供，无独立前端构建 |

测试位于 `tests/`，其中 `test_postgres.py` 和 `test_mysql.py` 覆盖真实数据库上的端到端工具调用。

## 工作流程

- 新任务默认创建独立分支，除非用户明确要求在 `main` 工作；现有 PR 的修订沿用其分支。
  分支名按改动类型和内容命名，例如 `feat/query-export`、`fix/mysql-timeout`、`docs/agent-guide`，
  不绑定具体 Agent 或工具名称。
- 复杂任务实现后做对抗性审查，并处理发现的问题。
- 实现、验证及必要文档更新完成后创建 PR；获取审查反馈并修复可操作的问题。
- 只有审查反馈已处理且必需检查通过后才能合并。

## 项目约束

- `schemas`、`exclude_tables` 仅控制元数据发现，不能限制 SQL 访问；权限边界在数据库授权和视图。
- 表格、图表的数据直接来自查询结果，模型只定义展示结构；复用 lovia 的 UI 和 `put_ui_data`。
  大结果存储依赖聊天上下文，裸 `Runner.run` 没有该上下文时只返回预览。
- `SQLDatabase.setup()` 每次运行都会调用；连接池和目录缓存保留在插件实例上，不能随每次运行重建。
- `skills/` 存放应用运行时的业务指标定义，业务口径变更应落在这里。

## 验证与易踩坑点

开发命令见 [README.md](README.md#development)，检查项以 [.github/workflows/ci.yml](.github/workflows/ci.yml) 为准。

- 未设置 `DB_AGENT_TEST_PG_URL` / `DB_AGENT_TEST_MYSQL_URL` 时，对应集成测试整模块跳过。
  修改共享数据库行为时需验证两种数据库，不能把只有单元测试通过当成完整验证。
- Agent 集成测试使用 `lovia.testing.ScriptedProvider`，无需真实模型密钥。
  测试完整 UI 结果存储时，参照 `tests/test_postgres.py` 中的 `ChatStore` / `UI_DATA_SINK` 设置。
- 演示 SQL 会删除并重建 PostgreSQL 的 `shop` schema 或 MySQL 的 `demo_shop` database，
  仅用于专用演示/测试数据库。

## 文档与发布

- 用户可见变化同步更新 `README.md` 和 `README-zh.md`；配置变化同步对应示例文件。
- 需要升版时同步 `pyproject.toml` 和 `db_agent/__init__.py`，并按发布约定更新变更记录。
  纯文档修改无需升版。
