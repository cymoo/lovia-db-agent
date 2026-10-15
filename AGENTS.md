# AGENTS.md

只记录影响开发决策的项目约定和非显然陷阱；不重复通用编程常识、目录清单或工具配置。

## 工作流程

- 新任务默认创建 `codex/` 分支，除非用户明确要求在 `main` 工作；现有 PR 的修订沿用其分支。
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
