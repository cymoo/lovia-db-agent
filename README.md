# lovia-db-agent

Ask your PostgreSQL or MySQL database questions in plain language. The agent
finds the tables, writes read-only SQL, and answers with **interactive tables**
and **ECharts charts** drawn straight from the query results — the model never
copies a number. Built on [lovia](https://github.com/cymoo/lovia) and its web UI.

[中文说明](README-zh.md)

## What you get

- **Any schema size.** The system prompt carries the schema itself, within a
  token budget. A small database arrives whole — columns, keys, enum labels,
  comments — so the agent writes SQL straight away. In a bigger one the tables
  that others point at come with their columns, more get just a name, and
  `search_schema` finds the rest by name, column or comment (comments in any
  language). Shards like `log_202401`, `log_202402`, … count once. One
  algorithm from ten tables to ten thousand, with no cliff in between.
- **Any result size.** A big result shows the model 20 rows plus per-column stats,
  while the user's table pages through all of them (up to 10,000), with sort,
  filter, copy and CSV. Charts with lots of points do the same.
- **Charts from SQL.** The model writes the chart's shape; the rows fill it in.
  `split_by` turns `(month, category, revenue)` rows into one line per category.
- **Read-only, in layers.** A read-only account (your job — see below), a
  statement timeout on every query, and a SQL check that refuses writes and
  caps the outermost `LIMIT`. MySQL sessions are read-only too.
- **Business definitions** as plain `SKILL.md` files the agent reads when needed.
- **Memory and a workspace.** The agent keeps notes across chats when you ask
  or confirm (preferences, definitions; editable on the Memory page) and works
  in a directory of files, shown in the Files panel. It can also show its own tables, and
  interactive pages (`show_html`) such as a what-if calculator.

## Quick start (demo database)

```bash
git clone https://github.com/cymoo/lovia-db-agent && cd lovia-db-agent
uv venv && uv pip install -e .            # or: pip install -e .

createdb demo_shop && psql demo_shop -f examples/demo_postgres.sql
cp config.example.yaml config.yaml
cp .env.example .env                       # set LOVIA_MODEL (+ API key) and DB_DEMO_URL

.venv/bin/lovia-db-agent check             # connects to each database, reports what it sees
.venv/bin/lovia-db-agent                   # → http://127.0.0.1:8000
```

Try: "每个月的销售额走势", "退款最多的商品 Top 10", "最近的订单".
MySQL: `mysql -u root < examples/demo_mysql.sql` and a `mysql+asyncmy://` URL.
Supported: PostgreSQL 11 or later, MySQL 8. MariaDB is not supported: it lacks
`max_execution_time`, which bounds every query.

## Configuration

`config.yaml` (see [config.example.yaml](config.example.yaml)) names the
connections; their URLs come from environment variables, so the file holds no
secrets and the model never sees one.

```yaml
connections:
  sales:
    url_env: DB_SALES_URL     # postgresql+asyncpg://... | mysql+asyncmy://...
    description: Orders, products, stores
    timezone: Asia/Shanghai
instructions: Reply in Chinese.
skills_dir: skills
memory: true                  # notes across chats, in <data_dir>/memory
workspace:
  mode: coding                # files in <data_dir>/workspace; shell commands need approval
```

Relative paths resolve against the config file. `.env` is read from the
directory you run in.

| Command | Does |
| --- | --- |
| `lovia-db-agent [serve]` | Serve the chat UI. `--config`, `--host`, `--port` (or `DB_AGENT_CONFIG` / `DB_AGENT_HOST` / `DB_AGENT_PORT`) |
| `lovia-db-agent check` | Connect to every database as the agent will; report its version, how many tables it can read and whether sessions can write, or the error with a hint |

Serving beyond localhost needs a login token: set `LOVIA_WEB_TOKEN`, or one is
generated and its `/?token=...` link printed at startup. Or put the app
behind your own auth.

Memory is one set of notes for everyone using the app, and the workspace one
directory for every chat. Memory holds only what the agent is told to
remember: chats aren't archived or mined for facts, so query results don't end
up there. Shell commands run with a minimal environment (no database URLs or
API keys), and no tool may open a `.env` file.

## Prepare the database

This is the security boundary, so do it properly:

1. A dedicated account with **SELECT only**, ideally on a read replica.
2. Keep sensitive columns out with **views**: create a schema (MySQL: a database)
   holding views of just what the agent may see, and grant only that. Discovery,
   queries and sample rows then all see the same thing. Column masking in SQL can
   be bypassed (`SELECT phone AS p`), so the agent doesn't pretend to do it.
3. PostgreSQL: `ALTER ROLE agent_ro SET default_transaction_read_only = on;`
   The app sends no session settings at connect time (poolers such as
   PgBouncer refuse them; each statement gets its time limit with
   `SET LOCAL`), so the role is what makes sessions read-only.
   `lovia-db-agent check` warns when they can write.

## How it works

| Tool | For | Returns |
| --- | --- | --- |
| `search_schema` | the model | matching tables and columns |
| `describe_tables` | the model | columns, keys, indexes, common values, 3 sample rows |
| `query` | the model | up to 50 rows, as text, and how long it took |
| `query_table` | the user | a table (`ui_result("table")`; when big, the model gets a preview) |
| `query_chart` | the user | an ECharts chart from the rows (`split_by` for one series per category) |

Plus lovia's `show_table` and `show_chart` (for data no single query returns),
`show_html` (an interactive page, sandboxed with no network), `now`, the
workspace's file and shell tools, and memory's `remember` / `forget`.

The schema comes from a few bulk catalog queries — tables, columns, keys —
however many tables there are, refreshed every 10 minutes in the background.
The system prompt holds only metadata, never a data value, and changes only
when the schema does (row counts drift, the text doesn't), so the model
provider's prompt cache stays warm. `limits.catalog_budget_tokens` (default
6000) sets its size. Common values in `describe_tables` come from PostgreSQL's
planner statistics (`pg_stats`), so describing a table never scans it; MySQL
lists enum labels in the column type, and the sample rows do the rest.

A big result stays out of the model's context: lovia's `put_ui_data` keeps
the full rows with the chat (deleted with it), the model reads a preview with
per-column stats, and the page draws every row with lovia's built-in table and
chart, in the chat and in its HTML export.

Every wait has a limit — connecting, a pool slot, each statement — so a slow
or down database reaches the model as an error with a hint, not a hang.
`describe_tables` samples each table with a `LIMIT` and a 3-second cap, and
gives up indexes, common values and samples rather than the whole call when
time runs short.
Values Python can't hold, like PostgreSQL's `infinity`, BC dates or year 10000,
arrive as text.

A Content-Security-Policy keeps the page from loading remote images, so text
planted in the database can't make a reply leak data through an image URL.

## Development

```bash
uv pip install -e ".[dev]"
pytest                                     # unit tests
DB_AGENT_TEST_PG_URL=postgresql+asyncpg://agent_ro:agent_ro@localhost/demo_shop pytest
DB_AGENT_TEST_MYSQL_URL=mysql+asyncmy://agent_ro:agent_ro@127.0.0.1:3306/demo_shop pytest
ruff check . && ruff format --check . && mypy
```

CI runs the integration tests against PostgreSQL 16, PostgreSQL 11 (the oldest
supported) and MySQL 8.4 containers.

Upgrading from lovia 0.9.87 or earlier: see
[docs/upgrade-lovia-0.9.96.md](docs/upgrade-lovia-0.9.96.md).

## License

MIT
