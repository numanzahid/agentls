# ai-bridge-cli

A terminal browser and manager for the **native local sessions** of
[Claude Code](https://docs.claude.com/en/docs/claude-code), OpenAI Codex CLI and Cursor CLI,
with optional integration with an [ai-bridge](https://github.com/numanzahid/ai-bridge) MCP server
on the same account.

* One scrollable list of every local session from all three agents, whether you started it
  directly in a terminal or through the bridge.
* Search, filter by agent / origin / project / status, sort by updated / title / agent / project.
* Multi-select, details and conversation preview, rename (display alias), archive / unarchive.
* Open (resume) or fork a session in its native CLI after a preflight; the browser suspends while
  the agent runs and comes back when it exits.
* Explicit native delete into a restorable trash, with a preview first.
* Headless `--json` / table commands for scripts.

It is a standalone Python package. It never imports the ai-bridge server and works without it.

```
 ai-bridge — local agent sessions
 Search title, project, ID, model…  ( / )   All agents ▼  Any origin ▼  Any status ▼  All projects ▼
    Agent   Origin   Status     Updated   Title                     Project        │ Title    Parser refactor
  ● claude  MCP      active     just now  Fix the login bug         ~/src/app      │ Session  codex:01a1…
    codex   Direct   idle       16m       Parser refactor           ~/src/app      │ Origin   Direct  No MCP provenance record.
    cursor  Unknown  idle       1h        Design chat               ~/src/site     │ Last 40 messages …
 99/100 sessions · 1 marked · sort updated↓ · 1 archived hidden (A) · provenance ok
 q Quit  ? Help  / Search  o Open  n Rename  a Archive  d Delete…  t Trash  s Sort  p Details  r Refresh
```

## Install

Requires Linux and Python 3.12 or newer. The installer uses its own virtualenv and never
uses `sudo` or installs packages globally.

```bash
git clone <this repo> ai-bridge-cli && cd ai-bridge-cli
bash install.sh --dry-run      # show what would change (launchers, replacements)
bash install.sh                # install or update
```

What `install.sh` does:

1. Builds a fresh virtualenv in `~/.local/share/ai-bridge-cli/venv.staging-*` from a clean
   copy of the source, using the tested pins in `constraints.txt`.
2. Smoke-tests it (version, TUI import, a headless listing), then swaps it in as
   `~/.local/share/ai-bridge-cli/venv` and keeps the previous one as `venv.previous`.
3. Installs two tiny launchers, `~/.local/bin/ai-bridge-cli` and `~/.local/bin/ai-bridge`, which
   run `venv/bin/python -I -m ai_bridge_cli`. Their hashes are recorded in
   `~/.local/share/ai-bridge-cli/launchers.json`.

An existing `~/.local/bin/ai-bridge` is replaced **only** if it is one of:

* a launcher this tool installed (hash matches its record), or
* the ai-bridge server's own launcher, where the server's ownership record
  (`~/.local/share/ai-bridge/.cli-launcher.json`) matches its path, uid and sha256, **and** the
  hash is a known server launcher. Use `--accept-main-record` to trust the record for an
  unrecognized server launcher version.

The original is backed up under `~/.local/share/ai-bridge-cli/backups/`, and the server's record
is never modified. Any other file at that path (a foreign or edited file, or a symlink) is left
untouched and reported. Pass `--no-replace-ai-bridge` to install only `ai-bridge-cli`.

```bash
bash install.sh --status       # who owns ~/.local/bin/ai-bridge{,-cli}
bash install.sh --rollback     # restore the server's original ai-bridge launcher + previous venv
bash install.sh --uninstall    # remove our launchers and venvs (restores the server launcher;
                               # keeps aliases, the trash and backups)
```

The installer does not restart, reconfigure or reinstall the ai-bridge MCP service.

For development: `python3 -m venv .venv && .venv/bin/pip install -e '.[test]' && .venv/bin/pytest`.

## Usage

```bash
ai-bridge                       # on a terminal: open the browser. Otherwise: print help.
ai-bridge sessions browse       # open the browser explicitly
ai-bridge --help                # every command; each subcommand has its own --help
```

`ai-bridge` and `ai-bridge-cli` are the same program.

### Browser keys

| Key | Action |
| --- | --- |
| `↑ ↓ PgUp PgDn Home End` | move |
| `/` | search (title, alias, project, ID, model); `Esc` returns to the list |
| `Tab` | move between search, filters and the list |
| `s` / `S` / click header | cycle sort key / reverse |
| `Space`, `*`, `Esc` | mark row, mark all visible (again: clear), clear marks |
| `Enter` / `o` | open (resume) in the native CLI, after a preflight and confirmation |
| `f` | fork (Codex `fork`, Claude `--resume --fork-session`) |
| `n` | rename (display alias; empty clears) |
| `a` | archive / unarchive the marked rows (or the current row) |
| `d` / `Del` | remove: **Archive** (default) or **Move to trash…** (native delete, previewed) |
| `t` | trash: restore an item, or `P` to purge a batch |
| `A` | show / hide archived sessions |
| `p` | details and preview pane (full screen on narrow terminals) |
| `y` | copy `agent:ID` to the clipboard (OSC 52) |
| `r` / `F5` | refresh (also automatic every 20 s) |
| `?` | help |
| `q` | quit |

The layout adapts to the terminal width. Wide terminals show the details pane beside the list.
Medium terminals hide the project column and filter. Narrow terminals show a compact list with
details as a full-screen toggle. Store errors, an empty store, or no matches are shown in place
rather than as an empty table.

### Headless commands

Output is a table on a terminal and JSON otherwise; force either with `--json` or `--table`.
Errors on a non-terminal are a JSON object on stderr. Exit codes: `0` ok, `1` error, `2` usage,
`3` partially completed (some items failed or were blocked).

```bash
ai-bridge sessions list [--agent claude|codex|cursor ...] [--origin mcp|direct|unknown]
                        [--status active|uncertain|recent|idle] [--project DIR] [--search WORDS]
                        [--sort updated|title|agent|project] [--reverse]
                        [--archived hide|include|only] [--native-archived] [--all]
                        [--mode ...] [--type ...] [--task-id ...] [--limit N] [--offset N]
ai-bridge sessions inspect agent:UUID|ID-PREFIX [--history N]
ai-bridge sessions open|resume|fork agent:UUID [--dry-run]
ai-bridge sessions rename agent:UUID "New name" | --clear
ai-bridge sessions archive|unarchive agent:UUID [agent:UUID ...]
ai-bridge sessions delete agent:UUID [...] [--dry-run]           # preview, prints a plan id
ai-bridge sessions delete agent:UUID [...] --yes --plan-id ID    # perform exactly that plan
ai-bridge sessions trash list | restore BATCH|codex:UUID [--session SEL] [--dry-run] | purge BATCH --yes
```

Selectors are `agent:UUID`. `inspect` also accepts a unique ID prefix of 8 or more characters.
Anything that changes state requires the exact ID.

## What the columns mean

**Origin** comes only from the ai-bridge server's provenance records, read **read-only** from
`$AI_BRIDGE_STATE_DIR/management/provenance/<sha256(agent + ":" + session_id)>.json`
(schema 1: `agent`, `session_id`, `origin: "mcp"`, `first_seen`, `job_ids`).

| Origin | Meaning |
| --- | --- |
| **MCP** | A record exists, matches this exact agent and session ID, and is a private (0600), single-link regular file owned by you, reached without symlinks through private directories. JSON output also sets `bridge_origin: "mcp"`, as the server does. |
| **Direct** | The provenance directory is valid and has no record for this session, or this account has no bridge state at all. |
| **Unknown** | The record or directory failed any check, or the bridge has never created the provenance directory, so origin cannot be verified. |

A headless or exec launch mode is **never** treated as MCP evidence. Sessions created through the
bridge before it began recording provenance appear as Direct. This tool never creates, locks or
writes anything in the provenance directory.

**Status** comes from one snapshot per refresh.

| Status | Meaning |
| --- | --- |
| **active** | Something is using the session right now: a bridge job running it (process identity verified), a live Claude Code process registered in `$CLAUDE_CONFIG_DIR/sessions/` (pid and start time verified), a process holding its file open or locked, or a process started with its ID. |
| **uncertain** | A bridge job is unfinished but its process cannot be verified. |
| **recent** | Modified in the last 2 minutes. |
| **idle** | None of the above. |

## Archive vs. delete

**Archive** is the default and the safe choice. It hides sessions from the default listing and
changes no native files. Aliases and archive flags live in `~/.local/state/ai-bridge-cli/overlays.json`.
Aliases and archive flags written by older ai-bridge CLIs in the server's state directory are
still honoured, read-only.

**Delete (move to trash)** is explicit. It always shows a preview first, then re-plans at
execution time and refuses if anything changed: files (device, inode, size, mtime), activity, or
blockers. It never unlinks anything.

| Agent | Method | Restore |
| --- | --- | --- |
| Claude Code | The transcript `<id>.jsonl`, its `<id>/` sidecar folder (subagents, tool results), `file-history/<id>` and `session-env/<id>` are renamed into `~/.local/share/ai-bridge-cli/trash/<batch>/` with a manifest. | `trash restore` puts every path back and never overwrites. |
| Cursor CLI | The chat folder (`store.db` with its WAL/SHM) and `projects/*/agent-transcripts/<id>` are moved the same way. | Same. |
| Codex | Rollout files are indexed by Codex's SQLite state, so this tool **never moves or deletes them**. It runs the installed `codex archive <UUID>` after checking that `codex archive --help` advertises it. If not, delete is reported as unsupported. | `codex unarchive`, via `trash restore`. |

Delete refuses when any of these hold:

* the session is active, uncertain, has unfinished bridge jobs, or was modified in the last 2 minutes;
* a path is a symlink, a hard link, a special file, not yours, outside the configured store, or
  reached through a symlink below the store root (every component is opened with `O_NOFOLLOW`);
* the trash is on a different filesystem, because moves must be atomic renames and there is no
  copy-then-delete;
* the session ID is ambiguous.

`trash purge BATCH --yes` permanently removes one batch; it is the only real deletion, and only
of data this tool moved. Purging leaves Codex items archived by Codex.

## Opening sessions

Before opening, the preflight checks:

* the stored file is yours, regular, not group/world-writable, and reached without symlinks;
* the project directory exists;
* the session is not in use;
* the installed CLI advertises the flag it needs (`claude --resume`,
  `codex resume [--include-non-interactive] ID`, `cursor-agent --resume`).

It re-checks the file's identity immediately before launch. The native CLI runs in the session's
project directory with your environment, minus `AI_BRIDGE_*` variables. Cursor resume is refused
while `AI_BRIDGE_CURSOR_HOME` points away from `~/.cursor`, because Cursor CLI itself would open
a different store.

## Bridge commands (optional)

If the ai-bridge server is installed for this account (`~/.local/share/ai-bridge/current ->
releases/<id>`), these commands keep working as they did in the server's old built-in CLI:

| Command | How |
| --- | --- |
| `agents status [--active] [--limit N]`, `agents inspect JOB` | **read-only**: job records are parsed directly and status is derived in memory. The server's own job loader rewrites records and backfills provenance, so it is not used. |
| `tasks list`, `tasks inspect ID` | **read-only**, with the server's ownership checks. |
| `agents stop JOB`, `tasks create`, `tasks cleanup ID --dry-run/--yes [--allow-uninspectable]`, `sessions continue SEL PROMPT`, `sessions start --agent A PROMPT` (`--cwd --task-id --permission --model --timeout --wait SECONDS`) | Run the **installed server's own code** in a subprocess, using its verified active release and its own virtualenv (`python -I`). The release must be yours, not writable by others, inside `releases/`, and marked `READY`. |
| `bridge status [--probe]` | Shows where everything is read from and whether the runtime is usable. |

Nothing from `ai_bridge` is imported into this process. The service is never restarted. Without
a bridge, these commands fail with a clear message and everything else works.

## Configuration

| Variable | Default | Used for |
| --- | --- | --- |
| `CLAUDE_CONFIG_DIR` | `~/.claude` | Claude Code store |
| `CODEX_HOME` | `~/.codex` | Codex store, and passed through to `codex` |
| `AI_BRIDGE_CURSOR_HOME` | `~/.cursor` | Cursor CLI store (read only; resume needs the default) |
| `AI_BRIDGE_STATE_DIR` | `~/.local/state/ai-bridge` | bridge state (read-only: jobs, provenance, legacy overlays) |
| `AI_BRIDGE_TASKS_DIR` | `~/.ai-bridge-tasks` | bridge task workspaces (read-only listing) |
| `AI_BRIDGE_CLI_STATE_DIR` | `~/.local/state/ai-bridge-cli` | this tool's aliases and archive flags |
| `AI_BRIDGE_CLI_DATA_DIR` | `~/.local/share/ai-bridge-cli` | this tool's trash |
| `AI_BRIDGE_CLI_BRIDGE_ROOT` | `~/.local/share/ai-bridge` | where to find the server's releases |
| `AI_BRIDGE_{CLAUDE,CODEX,CURSOR}_BIN` | `PATH` lookup | native CLI binaries |

Cursor's SQLite stores are opened read-only. When the WAL is empty they are opened `immutable=1`,
so not even the `-shm` index is touched. All titles, paths and messages are untrusted: control
characters are escaped and nothing is interpreted as markup.

## Intentionally deferred

* Permanent Codex deletion (`codex delete`). Codex sessions can only be archived natively.
* Copying the trash across filesystems. Moves are atomic renames on one filesystem, or refused.
* Running the bridge adapter against legacy, unversioned server installs with no `current` symlink.
* Native fork for Cursor, which Cursor CLI does not offer.
* The `--limit` 100 cap of the old server CLI. Listing now returns everything unless `--limit`
  is given.
* macOS and Windows. Activity detection relies on Linux `/proc`.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/pytest            # synthetic fixtures only; never reads your real stores
```

The scanners, the Cursor store reader and parts of the terminal output were adapted from the
MIT-licensed ai-bridge server. MIT licensed; see `LICENSE`.
