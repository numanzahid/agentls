# agentls

A terminal browser and manager for the local sessions of
[Claude Code](https://docs.claude.com/en/docs/claude-code), OpenAI Codex CLI and Cursor CLI.

* One list of every local session from all three agents, with search, filters and sorting.
* Details and conversation preview, rename, archive, and a restorable trash.
* Open, resume or fork a session in its native CLI.
* Stop running sessions and force-delete them, always behind a confirmation.
* Headless `--json` / table commands for scripts.

It is a standalone Python package. If an [ai-bridge](https://github.com/numanzahid/ai-bridge) MCP
server runs on the same account, the Origin column shows which sessions came through MCP; nothing
else depends on it.

```
 agentls — local agent sessions
 Search title, project, ID, model…  ( / )   All agents ▼  Any origin ▼  Any status ▼  All projects ▼
    Agent   Title                  Last Model           Updated  │ Title       Parser refactor
  ● claude  Fix the login bug      claude-sonnet-4-6     just now │ Last Model  gpt-5.4
    codex   Parser refactor        gpt-5.4               16m      │ Session     codex:01a1…
    cursor  Design chat            —                     1h       │ Last 40 messages …
 1 selected / 99 visible · sort updated↓ · 1 archived hidden (A) · provenance ok
 q Quit  ? Help  / Search  o Open  n Rename  a Archive  d Delete…  t Trash  s Sort  p Details  r Refresh
```

## Install

Requires Linux and Python 3.12+. Installs into its own virtualenv; no `sudo`, no global packages.

```bash
git clone https://github.com/numanzahid/agentls.git && cd agentls
bash install.sh --dry-run   # preview
bash install.sh             # install or update (git pull first to update)
```

`--status`, `--rollback` and `--uninstall` manage an existing install. See
[docs/install.md](docs/install.md).

## Use

```bash
agentls                                   # open the browser (prints help when not on a terminal)
agentls sessions list --agent codex --search refactor
agentls sessions inspect claude:1b2c3d4e --history 10
agentls sessions delete claude:<UUID> --dry-run
agentls --help                            # every command; each subcommand has --help
```

In the browser, press `?` for keys.

## Documentation

| Topic | |
| --- | --- |
| [Usage](docs/usage.md) | browser keys, layout, columns, headless commands |
| [Opening sessions](docs/opening.md) | native launch, shell integration, settings |
| [Removing sessions](docs/removal.md) | archive, trash, stop, force delete and their safety checks |
| [MCP origin and configuration](docs/mcp.md) | the Origin column, environment variables |
| [Install details](docs/install.md) | what `install.sh` does, rollback, uninstall |
| [Development](docs/development.md) | tests and the release process |

## Limitations

Linux only (activity detection uses `/proc`). The trash must be on the same filesystem as the
session stores. Cursor CLI has no native fork.

MIT licensed; see `LICENSE`.
