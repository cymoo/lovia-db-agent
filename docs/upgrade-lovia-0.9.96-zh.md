# 升级到 lovia 0.9.96

[English](upgrade-lovia-0.9.96.md)

lovia-db-agent 现在要求 `lovia[web]>=0.9.96`（原来是 `>=0.9.87`）。lovia 在 1.0 之前不保留兼容层，0.9.87 到 0.9.96 之间有两处改动会让本项目直接无法运行。旧的下限没有上界，项目也没有锁文件，所以全新执行 `pip install` 或 `uv pip install -e .` 时已经会装上 0.9.96 并报错；CI 也是这样安装的，同样会失败。

升级已有的检出：

```bash
uv pip install -e ".[dev]" --upgrade-package lovia
```

## 必须改的

### `ChatUI(templates=)` 改为 `ChatUI(assets=)`（lovia 0.9.92）

旧参数会在启动时报 `TypeError: ChatUI.__init__() got an unexpected keyword argument 'templates'`，`build_app()` 和 `lovia-db-agent` 命令都会这样。

```python
# db_agent/app.py, build_app()
ui = ChatUI(
    empty_title="Ask your data",
    empty_description="Questions in plain language; answers as tables and charts.",
    assets=_HERE / "templates",
)
```

`db_agent/templates/index.html` 本身不用改：它仍然继承 `lovia/index.html`，并在 `head` block 里设置图片来源的 CSP。

### `Plugin.setup(self)` 改为 `setup(self, ctx)`（lovia 0.9.90）

lovia 现在会把这次运行的 `RunContext` 传给每个插件的 `setup`。沿用旧签名的话，每次运行 agent 都会报 `TypeError: SQLDatabase.setup() takes 1 positional argument but 2 were given`。

```python
# db_agent/plugin.py
from lovia import PluginInstance, RunContext, ToolError, tool


async def setup(self, ctx: RunContext[Any]) -> PluginInstance:
    return PluginInstance(tools=list(self._tools), instructions=await self._prompt())
```

`SQLDatabase` 用不到 `ctx`；连接池和 catalog 缓存仍然放在插件实例上，和原来一样。

直接调用 `setup()` 的测试要传入一个 context（`tests/test_plugin.py`），写法和 lovia 自己的测试一样：

```python
ctx = RunContext(context=None, entries=[], agent=None)  # type: ignore[arg-type]
first = (await db.setup(ctx)).instructions
```

### 依赖下限

```toml
"lovia[web]>=0.9.96",
```

改完后的代码在 lovia 0.9.92 以前的版本上跑不了（不认识 `assets=`），所以下限必须一起提高。

## 自定义时要注意

- **templates 目录现在是公开的资源目录**：`db_agent/templates/` 里除了 `*.html` 和以点开头的文件，其余文件都会公开在 `/ui/` 下。目前只有 `index.html`；不要在里面放任何不该公开的东西。
- **页面扩展**：`ChatUI` 还接受 `scripts=`、`styles=`、`logo=`、`favicon=`（都相对 `assets` 解析），以及设置界面文案的 `default_lang=`、`messages=`。模板新增了 `sidebar_header`、`sidebar_nav`、`topbar_actions`、`composer_actions`、`welcome` 几个 block。CSP 的 `<meta>` 要继续放在 `head` 里。
- **只依赖 lovia 公开的样式接口**：block 名、`data-part` 属性、设计变量、`data-i18n` key。CSS 类名和元素 id 属于内部实现。lovia 的样式表现在放在 `@layer lovia` 里，不在任何 layer 里的样式总会压过内置样式，不需要 `!important`。工具卡片用 `[data-part="tool"][data-tool="<name>"]` 定位，旧的 `data-tool-name` 属性已经去掉。
- **自己写的插件**也要改成 `setup(self, ctx)`。

## 无需改动，但值得知道

- `put_ui_data`、`ui_result`、`validate_chart_option`、`show_table` / `show_chart` / `show_html`、`UI_DATA_SINK`、`ChatStore.get_ui_data`、`serve`、`generate_token`、`is_loopback` 的用法都没变。
- lovia 0.9.95 第一次打开 `chats.db` 时会加两个可空列（`active_context_json`、`context_json`）。旧版 lovia 查询时写明了列名，回退版本不受影响。
- lovia 0.9.95 新增了 `create_app(run_context=)`，可以按运行给工具一个 context，其中包括扩展脚本用 `window.lovia.provideContext` 发来的页面状态。本项目没有用它。以后可以用它传界面上选中的连接，但这个值由浏览器控制，不能用来决定用户能访问什么——访问边界仍然由数据库权限把守。
- lovia 0.9.96 新增了消息操作（如回退到某条消息），本项目不需要为此做任何事。

## 验证

- 设置 `DB_AGENT_TEST_PG_URL`（PostgreSQL）后跑 `pytest`：177 个通过，5 个跳过（MySQL 模块）。在 0.9.96 上不做这些修改时有 22 个失败。
- `ruff check .`、`ruff format --check .`、`mypy`：通过。
- 本地没有跑 MySQL。`tests/test_mysql.py` 走的是同一条 `Runner` → `setup` 路径，由 CI 在 MySQL 8.4 上验证。

## 跟上 lovia 的节奏

CI 每次安装满足下限的最新 lovia，所以 lovia 一发布破坏性改动，下一次推送 CI 就会变红。这是提前发现问题的信号，不是本仓库的故障。如果更想按自己的节奏升级，可以加锁文件（`uv lock`）或把版本钉死。

## 回退

把下限改回 `>=0.9.87`，把 `assets=` 改回 `templates=`、`setup(self, ctx)` 改回 `setup(self)`（测试也一起改回），然后执行 `uv pip install "lovia[web]==0.9.87"` 重新安装。
