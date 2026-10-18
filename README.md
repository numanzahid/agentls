# ai-bridge-cli

A terminal browser and manager for the local sessions of
[Claude Code](https://docs.claude.com/en/docs/claude-code), OpenAI Codex CLI and Cursor CLI.

* One list of every local session from all three agents, with search, filters and sorting.
* Details and conversation preview, rename, archive, and a restorable trash.
* Open, resume or fork a session in its native CLI.
* Stop running sessions and force-delete them, always behind a confirmation.
* Headless `--json` / table commands for scripts.

It is a standalone Python package and works without the ai-bridge server. If an
[ai-bridge](https://github.com/numanzahid/ai-bridge) MCP server runs on the same account, it also
shows which sessions came through MCP and can manage bridge jobs and tasks.

```
 ai-bridge — local agent sessions
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
git clone https://github.com/numanzahid/ai-bridge-cli.git && cd ai-bridge-cli
bash install.sh --dry-run   # preview
bash install.sh             # install or update (git pull first to update)
```

`--status`, `--rollback` and `--uninstall` manage an existing install. See
[docs/install.md](docs/install.md).

## Use

```bash
ai-bridge                                   # open the browser (prints help when not on a terminal)
ai-bridge sessions list --agent codex --search refactor
ai-bridge sessions inspect claude:1b2c3d4e --history 10
ai-bridge sessions delete claude:<UUID> --dry-run
ai-bridge --help                            # every command; each subcommand has --help
```

`ai-bridge` and `ai-bridge-cli` are the same program. In the browser, press `?` for keys.

## Documentation

| Topic | |
| --- | --- |
| [Usage](docs/usage.md) | browser keys, layout, columns, headless commands |
| [Opening sessions](docs/opening.md) | native launch, shell integration, settings |
| [Removing sessions](docs/removal.md) | archive, trash, stop, force delete and their safety checks |
| [ai-bridge integration](docs/bridge.md) | MCP origin, bridge commands, configuration |
| [Install details](docs/install.md) | what `install.sh` does, rollback, uninstall |
| [Development](docs/development.md) | tests and the release process |

## Limitations

Linux only (activity detection uses `/proc`). The trash must be on the same filesystem as the
session stores. Cursor CLI has no native fork.

The scanners, the Cursor store reader and parts of the terminal output were adapted from the
MIT-licensed ai-bridge server. MIT licensed; see `LICENSE`.
