# Upgrading to lovia 0.9.112

[中文](upgrade-lovia-0.9.112-zh.md)

lovia-db-agent now requires `lovia[web]>=0.9.112` (it was `>=0.9.96`). lovia keeps no
compatibility shims before 1.0, and one change between 0.9.96 and 0.9.112 breaks this repository.
It breaks a test, not the app. The old floor had no upper bound, so CI already installed 0.9.112
and failed on it.

To upgrade an existing checkout:

```bash
uv pip install -e ".[dev]" --upgrade-package lovia
```

## Required changes

### `RunContext.context` became `RunContext.deps` (lovia 0.9.112)

lovia now calls the object a caller passes through a run `deps` everywhere. One test builds a
`RunContext` by hand:

```python
# tests/test_plugin.py
ctx = RunContext(deps=None, entries=[], agent=None)  # type: ignore[arg-type]
```

The app itself never used the old name, so nothing else changes.

### Dependency floor

```toml
"lovia[web]>=0.9.112",
```

## If you customized the app

- **The rename reaches everything that carries deps.** It covers `Runner.run(..., deps=...)`
  (was `context=`), `ctx.deps` (the `ctx.context` alias is gone), `lovia.eval.Case(deps=...)`, and
  `create_app(run_deps=fn)` (was `run_context=`, with `RunDepsFn` and `lovia.web.run_deps`).
  An old spelling fails at once with a `TypeError` or `AttributeError`; none of them is silently
  ignored.
- **The page's client context keeps its names.** That covers `window.lovia.provideContext`, the
  request's `context` and `trigger.client`. The browser controls that value, so it must never
  decide what a user may access: database grants still do.
- **The CSP `<meta>` in `templates/index.html` is now the page's only policy.** Since 0.9.105
  lovia's chat page sets no Content-Security-Policy of its own. This app's `img-src 'self' data:
  blob:` still keeps model-written Markdown from loading remote images.
  - The sandboxed `show_html` frames inherit the policy too. That changes nothing for them, because
    they already block subresources and `fetch` on their own.
  - If you add a `script-src`, it must allow inline scripts, which the page relies on.
- **Welcome text.** This app sets `empty_title` / `empty_description` in English, so they show as
  is in every UI language. Since 0.9.100, leaving them unset shows lovia's own welcome in the
  user's language.

## Unchanged, but worth knowing

- **Chat database versioning (lovia 0.9.111).** On first open, lovia adds a small
  `lovia_schema` table to `chats.db` and records each store's schema version there. Older lovia
  ignores the table, so rolling back still opens the file. Stored records now read leniently:
  fields a newer lovia added are ignored rather than rejected.
- **A reply's working steps fold into one activity line (lovia 0.9.102).** Reasoning and tool calls
  collapse into one line you can expand. Tables and charts from `query_table` / `query_chart` stay
  outside the fold, in the reply itself.
- **lovia's own tools read in plain language (lovia 0.9.103).** This app's `query_table` /
  `query_chart` still show their name and arguments. An extension script could label them with
  `lovia.registerToolView`.
- **Views (lovia 0.9.106)** let a deployer's own pages take the chat's place, and
  `lovia web init-ui` (0.9.104) scaffolds a UI directory. This app needs neither.
- Blocking `POST /api/chat` turns now run through the same supervisor as streamed ones (0.9.97).
  The app doesn't call that endpoint.

## Verification

- `pytest` with `DB_AGENT_TEST_PG_URL` set (PostgreSQL 18): 177 passed, 5 skipped (the MySQL
  module). Against 0.9.112 without these changes, 1 test fails: the `RunContext` one.
- `ruff check .`, `ruff format --check .` and `mypy` are clean.
- MySQL was not run locally. CI runs it against MySQL 8.4.

## Rolling back

Set the floor back to `>=0.9.96`, change the test back to `RunContext(context=None, ...)`, then
reinstall with `uv pip install "lovia[web]==0.9.96"`.
