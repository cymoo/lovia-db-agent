# 升级到 lovia 0.9.235

[English](upgrade-lovia-0.9.235.md)

lovia-db-agent 现在要求 `lovia[web]>=0.9.235`（原来是 `>=0.9.112`）。0.9.112 到 0.9.235 之间没有改动
影响本应用或它的测试：代码和 `config.yaml` 都不用改。需要留意的有两点：记忆页挪了位置；`chats.db` 被新版
打开后，旧版 lovia 就打不开了。

升级已有的检出：

```bash
cp .data/chats.db .data/chats.db.bak   # 只有可能回退时才需要
uv pip install -e ".[dev]" --upgrade-package lovia
```

## 必须改的

### 依赖下限

```toml
"lovia[web]>=0.9.235",
```

### 记忆移进了“设置”（lovia 0.9.204）

agent 记下的笔记（`memory: true`）现在在侧栏底部的 **设置 → 记忆** 里，不再有单独的记忆页。README 和
`config.example.yaml` 已经改了；你自己写给用户的说明如果提到旧的记忆页，也要改。

## 自定义时要注意

- **`chats.db` 只能往前升。** 0.9.235 第一次打开 `chats.db` 时会加列和表（审批授权、归档、定时任务名称、
  对话自己的模型、等待中的运行）。之后旧版 lovia 打开它会报 “newer than this lovia knows”。可能回退的话，
  按上面先备份。
- **`lovia[web]` 装的东西多了。** 从 0.9.199 起它包含 `mcp` 包，会带上 `jsonschema`、`pyjwt` 等几个依赖。
  本应用不用 MCP，这些只是装在环境里。
- **`create_app` 新增的参数**（`suspend_after`、`question_channel`、`wire_subagents`、`workspaces` 等）
  都有默认值，本应用一个都没设。
  - `workspaces=WorkspaceSwitch(...)`（0.9.148）可以让每个对话自选文件夹和权限模式。不传时，
    `workspace.mode` 对所有对话生效，和以前一样。如果要加，除非允许用户关掉 shell 审批，否则别把
    `"trusted"` 放进 `modes`。

## 无需改动，但值得知道

- **shell 审批（工作区 `mode: coding`）。** 等待审批的命令会显示一张卡片，写明结果；“始终允许”按对话
  记住，并且可以选范围（0.9.135–0.9.141）。
- **没人回答的审批会挂起运行（0.9.231）。** 运行不再占着名额，有人回答后继续，重启后也一样。侧栏会标出
  在等你处理的对话。
- **定时任务成了侧栏里的一个页面**（0.9.201）：可以给任务命名、预览接下来的执行时间、查看跳过的运行。
- **对话多了几个操作。** 可以分支对话（0.9.118）、在回复进行中排队一条消息（0.9.120）、归档对话
  （0.9.158）。`query_table` / `query_chart` 的结果照旧跟着对话保存。
- **页面外观。** 配色改成了中性色，侧栏可以拖动调宽，也可以收成图标栏（0.9.142–0.9.159）。本应用没有
  自己的 CSS，不用调整。
- **`templates/index.html` 里的 CSP `<meta>`** 仍是页面唯一的策略；表格、图表和 `show_html` 框架在它下面
  照常显示。

## 验证

- 设置 `DB_AGENT_TEST_PG_URL`（PostgreSQL 18）跑 `pytest`：177 个通过，5 个跳过（MySQL 模块）。
- `ruff check .`、`ruff format --check .` 和 `mypy` 都没有问题。
- 浏览器里连演示库、用脚本模型：`query_table` 的表格和 `query_chart` 的柱状图都正常显示，CSP 仍是
  `img-src 'self' data: blob:`，控制台没有报错，“设置”里有“记忆”标签。
- 本地没跑 MySQL，CI 会对 MySQL 8.4 跑。

## 回退

把下限改回 `>=0.9.112`，用 `uv pip install "lovia[web]==0.9.112"` 重装，再从备份恢复 `chats.db`。
