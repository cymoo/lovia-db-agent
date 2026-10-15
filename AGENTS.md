# AGENTS.md

## Project overview

`lovia-db-agent` is a natural-language data analysis app for PostgreSQL and MySQL.
Users ask questions in chat; the agent discovers schemas, generates read-only SQL,
and presents query results directly as interactive tables or ECharts charts.

The app uses Python 3.10+, lovia's Agent, plugins and chat UI, and FastAPI.
Database access uses SQLAlchemy async engines with asyncpg / asyncmy; sqlglot checks SQL.
YAML configures named connections, query limits and business skill directories;
connection URLs come from environment variables.

The central `SQLDatabase` plugin exposes `search_schema`, `describe_tables` and `query`
for model exploration, and `query_table` and `query_chart` for user-facing results.
Large results stay in chat storage; the model receives only previews or summaries.

## Code entry points

Paths are relative to the repository root. Start with the relevant entry points and tests.

| Task | Main entry points |
| --- | --- |
| Startup, configuration, agent assembly | `db_agent/__main__.py`, `db_agent/config.py`, `db_agent/app.py` |
| Tool interfaces, prompts, result envelopes | `SQLDatabase` in `db_agent/plugin.py` |
| Schema discovery, search, descriptions | `db_agent/catalog.py` |
| SQL checks, execution, pools | `db_agent/guard.py`, `db_agent/execute.py` |
| Database differences: session setup, timeouts, catalog SQL, error hints, prompt notes | `Dialect` in `db_agent/dialects.py`; other modules never branch on the dialect |
| Data conversion, chart pivots, result statistics | `db_agent/convert.py`, `db_agent/charts.py`, `db_agent/stats.py` |
| Page customization and image-source CSP | `db_agent/templates/index.html`; lovia provides the page and components, with no separate frontend build |

Tests live in `tests/`; `test_postgres.py` and `test_mysql.py` cover end-to-end tool calls
against real databases.

## Workflow

- Start new tasks on a separate branch unless the user explicitly requests work on `main`.
  Reuse the existing branch for PR revisions. Name branches by change type and purpose,
  such as `feat/query-export`, `fix/mysql-timeout` or `docs/agent-guide`, without agent or tool names.
- For complex tasks, perform an adversarial review after implementation and address the findings.
- Open a PR after implementation, verification and necessary documentation updates.
  Fetch review feedback and fix actionable findings.
- Merge only after review feedback is addressed and required checks pass.

## Project constraints

- `schemas` and `exclude_tables` only control metadata discovery, not SQL access.
  Database grants and views enforce the access boundary.
- Table and chart data comes directly from query results; the model defines presentation structure.
  Reuse lovia's UI and `put_ui_data`. Large-result storage requires a chat context;
  a bare `Runner.run` without that context returns only the preview.
- `SQLDatabase.setup()` runs on every agent run. Keep connection pools and catalog caches
  on the plugin instance rather than recreating them for each run.
- `skills/` holds business metric definitions used by the app at runtime.
  Changes to business definitions belong there.

## Validation and pitfalls

Development commands are in [README.md](README.md#development).
The check definitions are in [.github/workflows/ci.yml](.github/workflows/ci.yml).

- Without `DB_AGENT_TEST_PG_URL` / `DB_AGENT_TEST_MYSQL_URL`, the corresponding integration
  test module is skipped entirely. Validate shared database changes against both databases;
  passing unit tests alone is not full integration coverage.
- Agent integration tests use `lovia.testing.ScriptedProvider` and need no real model API key.
  For full UI result storage tests, follow the `ChatStore` / `UI_DATA_SINK` setup in
  `tests/test_postgres.py`.
- Demo SQL drops and recreates PostgreSQL's `shop` schema or MySQL's `demo_shop` database.
  Run it only against dedicated demo or test databases.

## Documentation and releases

- Keep `README.md` and `README-zh.md` in sync for user-visible changes; update the relevant
  example files when configuration changes.
- When bumping the version, update both `pyproject.toml` and `db_agent/__init__.py`,
  and update the changelog according to release conventions. Documentation-only changes need no bump.
