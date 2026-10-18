# ai-bridge integration

Optional. Without an ai-bridge server on the account, Origin shows **Direct** and the bridge
commands report that the bridge is unavailable; everything else works. Nothing from `ai_bridge` is
imported, and the server is never restarted or reconfigured.

## Origin

Read only from the server's provenance records,
`$AI_BRIDGE_STATE_DIR/management/provenance/<sha256(agent + ":" + session_id)>.json`.
This tool never writes there.

| Origin | Meaning |
| --- | --- |
| MCP | a valid record for this exact agent and session; private, owned by you, no symlinks |
| Direct | proven not MCP: no ai-bridge on the account at all, or no record for a session created after recording began, with no bridge job referencing it and every job record readable |
| Unknown | anything else, such as sessions older than provenance recording or records that fail a check |

A headless or exec launch mode is never treated as MCP evidence.

## Bridge commands

Need the server installed with versioned releases (`~/.local/share/ai-bridge/current`).

| Command | How it runs |
| --- | --- |
| `agents status [--active]`, `agents inspect JOB`, `tasks list`, `tasks inspect ID` | read-only; job status is derived in memory and never written back |
| `agents stop JOB`, `tasks create`, `tasks cleanup ID --dry-run/--yes`, `sessions continue SEL PROMPT`, `sessions start --agent A PROMPT` | the installed server's own code in a subprocess, using its verified active release and virtualenv |
| `bridge status [--probe]` | where everything is read from and whether the runtime works |

`sessions continue/start` accept `--cwd --task-id --permission --model --timeout --wait SECONDS`.

## Configuration

| Variable | Default | Used for |
| --- | --- | --- |
| `CLAUDE_CONFIG_DIR` | `~/.claude` | Claude Code store |
| `CODEX_HOME` | `~/.codex` | Codex store; passed to `codex` |
| `AI_BRIDGE_CURSOR_HOME` | `~/.cursor` | Cursor CLI store (resume needs the default) |
| `AI_BRIDGE_{CLAUDE,CODEX,CURSOR}_BIN` | `PATH` | native CLI binaries |
| `AI_BRIDGE_CLI_STATE_DIR` | `~/.local/state/ai-bridge-cli` | aliases and archive flags |
| `AI_BRIDGE_CLI_DATA_DIR` | `~/.local/share/ai-bridge-cli` | trash |
| `AI_BRIDGE_STATE_DIR` | `~/.local/state/ai-bridge` | bridge jobs and provenance (read-only) |
| `AI_BRIDGE_TASKS_DIR` | `~/.ai-bridge-tasks` | bridge task workspaces (read-only) |
| `AI_BRIDGE_CLI_BRIDGE_ROOT` | `~/.local/share/ai-bridge` | server releases |

Native stores are only read here (Cursor's SQLite is opened read-only). Titles, paths and messages
are treated as untrusted: control characters are escaped and nothing is rendered as markup.
