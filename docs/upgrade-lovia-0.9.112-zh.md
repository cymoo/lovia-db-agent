# 升级到 lovia 0.9.112

[English](upgrade-lovia-0.9.112.md)

lovia-db-agent 现在要求 `lovia[web]>=0.9.112`（原来是 `>=0.9.96`）。lovia 在 1.0 之前不保留兼容层，
0.9.96 到 0.9.112 之间有一处改动会让本仓库出错：坏的是一个测试，不是应用本身。旧的下限没有上界，
所以 CI 已经装上 0.9.112 并因此失败。

升级已有的检出：

```bash
uv pip install -e ".[dev]" --upgrade-package lovia
```

## 必须改的

### `RunContext.context` 改为 `RunContext.deps`（lovia 0.9.112）

lovia 把调用方随运行传入的对象统一叫作 `deps`。有一个测试手动构造了 `RunContext`：

```python
# tests/test_plugin.py
ctx = RunContext(deps=None, entries=[], agent=None)  # type: ignore[arg-type]
```

应用代码本身没有用到旧名字，其他地方都不用改。

### 依赖下限

```toml
"lovia[web]>=0.9.112",
```

## 自定义时要注意

- **所有携带 deps 的地方都跟着改了名**：`Runner.run(..., deps=...)`（原来是 `context=`）、
  `ctx.deps`（`ctx.context` 这个别名已删除）、`lovia.eval.Case(deps=...)`，以及
  `create_app(run_deps=fn)`（原来是 `run_context=`，配套的 `RunDepsFn`、`lovia.web.run_deps`）。
  旧写法会立即抛出 `TypeError` 或 `AttributeError`，不会被悄悄忽略。
- **页面的客户端上下文保留原来的名字**：`window.lovia.provideContext`、请求里的 `context` 和
  `trigger.client`。这个值由浏览器控制，绝不能用它决定用户能访问什么，访问边界仍由数据库授权保证。
- **`templates/index.html` 里的 CSP `<meta>` 现在是页面唯一的策略。** lovia 从 0.9.105 起不再给聊天页面
  设置 Content-Security-Policy。本应用的 `img-src 'self' data: blob:` 依然阻止模型写的 Markdown
  加载远程图片。
  - 沙箱里的 `show_html` 页面也会继承这条策略。这对它们没有影响，因为这些页面本身已经拦截了外部资源
    和 `fetch`。
  - 如果你要加 `script-src`，必须允许内联脚本，页面依赖它们。
- **欢迎语。** 本应用用英文设置了 `empty_title` / `empty_description`，所以在任何界面语言下都原样显示。
  从 0.9.100 起，不设置它们时，会按用户的语言显示 lovia 自带的欢迎语。

## 无需改动，但值得知道

- **聊天库加了版本号（lovia 0.9.111）。** 首次打开时，lovia 会在 `chats.db` 里加一张小表 `lovia_schema`，
  记录每个存储的表结构版本。旧版 lovia 会忽略这张表，所以回退后仍能打开这个文件。读取已存记录也变宽松了：
  新版本加的字段会被忽略，而不是直接报错。
- **一次回复的中间步骤折叠成一行"活动"（lovia 0.9.102）。** 思考和工具调用会收进一行，可以展开查看。
  `query_table` / `query_chart` 生成的表格和图表不会被折叠，仍然直接显示在回复里。
- **lovia 自带的工具改为用通俗语言描述（lovia 0.9.103）。** 本应用的 `query_table` / `query_chart`
  仍显示工具名和参数；如果需要，可以写一个扩展脚本，用 `lovia.registerToolView` 给它们配上说明。
- **视图（lovia 0.9.106）** 可以让部署者自己的页面替换聊天区域；`lovia web init-ui`（0.9.104）可以生成
  一个界面目录模板。本应用都用不到。
- 阻塞式的 `POST /api/chat` 现在和流式请求走同一个 supervisor（0.9.97）。本应用没有调用这个接口。

## 验证

- 设置 `DB_AGENT_TEST_PG_URL`（PostgreSQL 18）后运行 `pytest`：177 个通过，5 个跳过（MySQL 那一组）。
  不做上述修改、直接用 0.9.112 运行时，失败 1 个，就是那个 `RunContext` 测试。
- `ruff check .`、`ruff format --check .` 和 `mypy` 都没有报错。
- 本地没有运行 MySQL 测试，CI 会在 MySQL 8.4 上运行。

## 回退

把下限改回 `>=0.9.96`，把测试改回 `RunContext(context=None, ...)`，然后用
`uv pip install "lovia[web]==0.9.96"` 重新安装。
