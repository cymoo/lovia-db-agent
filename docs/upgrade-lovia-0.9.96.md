# Upgrading to lovia 0.9.96

[中文](upgrade-lovia-0.9.96-zh.md)

lovia-db-agent now requires `lovia[web]>=0.9.96` (it was `>=0.9.87`). lovia keeps no
compatibility shims before 1.0, and two changes between 0.9.87 and 0.9.96 stop this app
outright. Because the old floor had no upper bound and the project has no lock file, a fresh
`pip install` / `uv pip install -e .` already picked up 0.9.96 and failed. CI installs the same
way, so it was failing too.

To upgrade an existing checkout:

```bash
uv pip install -e ".[dev]" --upgrade-package lovia
```

## Required changes

### `ChatUI(templates=)` became `ChatUI(assets=)` (lovia 0.9.92)

The old argument fails at startup with
`TypeError: ChatUI.__init__() got an unexpected keyword argument 'templates'`, from both
`build_app()` and the `lovia-db-agent` command.

```python
# db_agent/app.py
ui=ChatUI(
    empty_title="Ask your data",
    empty_description="Questions in plain language; answers as tables and charts.",
    assets=_HERE / "templates",
),
```

`db_agent/templates/index.html` itself is unchanged: it still extends `lovia/index.html` and
puts the image-source CSP in the `head` block.

### `Plugin.setup(self)` became `setup(self, ctx)` (lovia 0.9.90)

lovia now passes the run's `RunContext` to every plugin's `setup`. With the old signature,
every agent run fails with
`TypeError: SQLDatabase.setup() takes 1 positional argument but 2 were given`.

```python
# db_agent/plugin.py
from lovia import PluginInstance, RunContext, ToolError, tool

async def setup(self, ctx: RunContext[Any]) -> PluginInstance:
    return PluginInstance(tools=list(self._tools), instructions=await self._prompt())
```

`SQLDatabase` doesn't use `ctx`. Connection pools and catalog caches stay on the plugin
instance, as before.

Tests that call `setup()` directly now pass a context (`tests/test_plugin.py`), the same way
lovia's own tests do:

```python
ctx = RunContext(context=None, entries=[], agent=None)  # type: ignore[arg-type]
first = (await db.setup(ctx)).instructions
```

### Dependency floor

```toml
"lovia[web]>=0.9.96",
```

The code no longer runs on lovia older than 0.9.92 (`assets=`), so the floor has to rise
along with it.

## If you customized the app

- **The templates directory is now a public assets directory.** Every file in
  `db_agent/templates/` except `*.html` and dotfiles is served under `/ui/`. Today it holds only
  `index.html`. Don't keep anything private there.
- **Page extensions.** `ChatUI` also takes `scripts=`, `styles=`, `logo=` and `favicon=`, resolved
  inside `assets`, plus `default_lang=` and `messages=` for UI strings. The template gains the
  blocks `sidebar_header`, `sidebar_nav`, `topbar_actions`, `composer_actions` and `welcome`.
  Keep the CSP `<meta>` in `head`.
- **Stick to lovia's public styling contract**: block names, `data-part` attributes, design
  variables and `data-i18n` keys. Class names and element ids are internal. lovia's stylesheet now
  sits in `@layer lovia`, so any unlayered rule of yours wins without `!important`. Tool cards are
  `[data-part="tool"][data-tool="<name>"]`. The old `data-tool-name` attribute is gone.
- **Plugins of your own** need the new `setup(self, ctx)` signature too.

## Unchanged, but worth knowing

- `put_ui_data`, `ui_result`, `validate_chart_option`, `show_table` / `show_chart` / `show_html`,
  `UI_DATA_SINK`, `ChatStore.get_ui_data`, `serve`, `generate_token` and `is_loopback` work as
  before.
- On first open, lovia 0.9.95 adds two nullable columns to `chats.db` (`active_context_json`,
  `context_json`). Older lovia versions select columns by name, so rolling back still works.
- lovia 0.9.95 added `create_app(run_context=)`, which hands tools a per-run context, including
  UI state an extension sends with `window.lovia.provideContext`. This app doesn't use it. It
  could carry, say, a connection picked in the UI. The browser controls that value, though, so
  it must never decide what a user may access: database grants still do.
- lovia 0.9.96 adds message actions, such as rewinding to a message. They need nothing from this
  app.

## Verification

- `pytest` with `DB_AGENT_TEST_PG_URL` set (PostgreSQL): 177 passed, 5 skipped (the MySQL module).
  Against 0.9.96 without these changes: 22 failed.
- `ruff check .`, `ruff format --check .`, `mypy`: clean.
- MySQL was not run locally. `tests/test_mysql.py` goes through the same `Runner` → `setup` path,
  and CI runs it against MySQL 8.4.

## Staying ahead of lovia

CI installs the newest lovia that satisfies the floor, so a breaking lovia release turns CI red
on the next push. That is an early warning, not a failure of this repository. If you'd rather
upgrade on your own schedule, add a lock file (`uv lock`) or pin an exact version.

## Rolling back

Set the floor back to `>=0.9.87`, revert `assets=` to `templates=` and `setup(self, ctx)` to
`setup(self)` (and the test), then reinstall with `uv pip install "lovia[web]==0.9.87"`.
