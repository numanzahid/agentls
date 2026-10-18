# MCP origin and configuration

## Origin

The Origin column (and `sessions list --origin`) shows whether a session came through an
[ai-bridge](https://github.com/numanzahid/ai-bridge) MCP server on this account. Without one,
Origin shows **Direct** and everything else works the same.

It is read only from the server's provenance records,
`$AI_BRIDGE_STATE_DIR/management/provenance/<sha256(agent + ":" + session_id)>.json`, and its
agent job records (`$AI_BRIDGE_STATE_DIR/jobs/`). This tool never writes there, never runs the
server's code, and never restarts or reconfigures it.

| Origin | Meaning |
| --- | --- |
| MCP | a valid record for this exact agent and session; private, owned by you, no symlinks |
| Direct | proven not MCP: no ai-bridge on the account at all, or no record for a session created after recording began, with no MCP job referencing it and every job record readable |
| Unknown | anything else, such as sessions older than provenance recording or records that fail a check |

A headless or exec launch mode is never treated as MCP evidence.

The same job records also keep sessions safe: a session that an unfinished MCP job may still be
using shows as active or uncertain, and stopping, archiving or deleting it is refused. Stop such
jobs through the MCP server.

## Configuration

| Variable | Default | Used for |
| --- | --- | --- |
| `CLAUDE_CONFIG_DIR` | `~/.claude` | Claude Code store |
| `CODEX_HOME` | `~/.codex` | Codex store; passed to `codex` |
| `AGENTLS_CURSOR_HOME` | `~/.cursor` | Cursor CLI store (resume needs the default) |
| `AGENTLS_{CLAUDE,CODEX,CURSOR}_BIN` | `PATH` | native CLI binaries |
| `AGENTLS_STATE_DIR` | `~/.local/state/agentls` | aliases and archive flags |
| `AGENTLS_DATA_DIR` | `~/.local/share/agentls` | install and trash |
| `AI_BRIDGE_STATE_DIR` | `~/.local/state/ai-bridge` | the MCP server's jobs and provenance (read-only) |
| `AGENTLS_MCP_ROOT` | `~/.local/share/ai-bridge` | the MCP server's install; its absence proves Direct |

Settings live in `~/.config/agentls/settings.json` (`$XDG_CONFIG_HOME`).

Native stores are only read here (Cursor's SQLite is opened read-only). Titles, paths and messages
are treated as untrusted: control characters are escaped and nothing is rendered as markup.
