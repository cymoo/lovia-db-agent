# Upgrading to lovia 0.9.235

[中文](upgrade-lovia-0.9.235-zh.md)

lovia-db-agent now requires `lovia[web]>=0.9.235` (it was `>=0.9.112`). Nothing between 0.9.112
and 0.9.235 breaks this app or its tests: the code is unchanged, and so is `config.yaml`. Two
things do need your attention: the Memory page moved, and `chats.db` can't go back to an older
lovia once the new one opens it.

To upgrade an existing checkout:

```bash
cp .data/chats.db .data/chats.db.bak   # only needed if you may roll back
uv pip install -e ".[dev]" --upgrade-package lovia
```

## Required changes

### Dependency floor

```toml
"lovia[web]>=0.9.235",
```

### Memory moved into Settings (lovia 0.9.204)

The notes the agent keeps (`memory: true`) are now under **Settings → Memory**, opened from the
foot of the sidebar. There is no separate Memory page any more. The README and
`config.example.yaml` say so now; update your own user-facing docs if they point at the old page.

## If you customized the app

- **`chats.db` is one-way.** lovia adds columns and tables to `chats.db` the first time 0.9.235
  opens it (approval grants, archived chats, schedule names, a chat's own model, waiting runs).
  An older lovia then refuses the file with "newer than this lovia knows". Keep the backup above
  if you may roll back.
- **`lovia[web]` installs more.** Since 0.9.199 it includes the `mcp` package, which pulls in
  `jsonschema`, `pyjwt` and a few more. Nothing in this app uses MCP; it is only in the
  environment.
- **New `create_app` options** — `suspend_after`, `question_channel`, `wire_subagents`,
  `workspaces` and others — all have defaults. This app sets none of them.
  - `workspaces=WorkspaceSwitch(...)` (0.9.148) would let each chat pick its own folder and
    permission mode. Without it, `workspace.mode` applies to every chat, as before. If you add
    it, leave `"trusted"` out of `modes` unless users may turn off shell approvals.

## Unchanged, but worth knowing

- **Shell approvals (workspace `mode: coding`).** A command that waits for approval shows a card
  that says what happened. "Always allow" is remembered per chat, with a choice of how broad
  (0.9.135–0.9.141).
- **An unanswered approval suspends its run (0.9.231).** The run stops holding a slot and resumes
  when someone answers, even after a restart. The sidebar marks chats that are waiting on you.
- **Scheduled runs are a page** in the sidebar (0.9.201): name a schedule, preview its next
  times, see the runs it skipped.
- **More per-chat actions.** You can branch a chat (0.9.118), queue a message behind a running
  reply (0.9.120) and archive a chat (0.9.158). They all keep `query_table` / `query_chart`
  results with the chat, as before.
- **The page's look.** The palette is neutral and the sidebar can be resized or collapsed to an
  icon rail (0.9.142–0.9.159). This app has no CSS of its own, so nothing needs adjusting.
- **The CSP `<meta>` in `templates/index.html`** is still the page's only policy, and tables,
  charts and `show_html` frames render under it unchanged.

## Verification

- `pytest` with `DB_AGENT_TEST_PG_URL` set (PostgreSQL 18): 177 passed, 5 skipped (the MySQL
  module).
- `ruff check .`, `ruff format --check .` and `mypy` are clean.
- In the browser, against the demo database and a scripted model: a `query_table` table and a
  `query_chart` bar chart render, the CSP stays `img-src 'self' data: blob:`, the console has no
  errors, and Settings has a Memory tab.
- MySQL was not run locally. CI runs it against MySQL 8.4.

## Rolling back

Set the floor back to `>=0.9.112`, reinstall with `uv pip install "lovia[web]==0.9.112"`, and
restore `chats.db` from the backup.
