# ai-bridge-cli

A terminal browser and manager for the **native local sessions** of
[Claude Code](https://docs.claude.com/en/docs/claude-code), OpenAI Codex CLI and Cursor CLI,
with optional integration with an [ai-bridge](https://github.com/numanzahid/ai-bridge) MCP server
on the same account.

* One scrollable list of every local session from all three agents, whether you started it
  directly in a terminal or through the bridge.
* Search, filter by agent / origin / project / status, sort by updated / title / agent / project.
* Last Model column shows the most recently recorded known model for each chat.
* Multi-select, details and conversation preview, rename (display alias), archive / unarchive.
* Open (resume) or fork a session in its native CLI after a preflight; by default the browser
  exits and exec-replaces itself with the agent. Settings can select suspend and return instead.
* Explicit native delete into a restorable trash, with a preview first.
* Headless `--json` / table commands for scripts.

It is a standalone Python package. It never imports the ai-bridge server and works without it.

```
 ai-bridge — local agent sessions
 Search title, project, ID, model…  ( / )   All agents ▼  Any origin ▼  Any status ▼  All projects ▼
    Agent   Title                  Last Model           Updated  │ Title       Parser refactor
  ● claude  Fix the login bug      claude-sonnet-4-6     just now │ Last Model  gpt-5.4
    codex   Parser refactor        gpt-5.4               16m      │ Session     codex:01a1…
    cursor  Design chat            —                     1h       │ Last 40 messages …
 99/100 sessions · 1 marked · sort updated↓ · 1 archived hidden (A) · provenance ok
 q Quit  ? Help  / Search  o Open  n Rename  a Archive  d Delete…  t Trash  s Sort  p Details  r Refresh
```

## Install

Requires Linux and Python 3.12 or newer. The installer uses its own virtualenv and never
uses `sudo` or installs packages globally.

```bash
git clone https://github.com/numanzahid/ai-bridge-cli.git && cd ai-bridge-cli
bash install.sh --dry-run      # show what would change (launchers, replacements)
bash install.sh                # install or update
```

What `install.sh` does:

1. Runs a **read-only preflight** of the launcher plan and refuses before building anything if
   `~/.local/bin/ai-bridge-cli` is a foreign file or `~/.local/bin` is unsafe.
2. Builds a fresh virtualenv in `~/.local/share/ai-bridge-cli/venv.staging-*` from a clean
   copy of the source, using the tested pins in `constraints.txt`, and smoke-tests it (version,
   TUI import, a headless listing).
3. Activates it in **one locked transaction** (`~/.local/share/ai-bridge-cli/.install.lock`; a
   concurrent install or rollback is refused). The transaction repeats the preflight, swaps the
   new venv in, keeps the previous one as `venv.previous`, and verifies the relocated venv. It then
   installs two tiny launchers, `~/.local/bin/ai-bridge-cli` and `~/.local/bin/ai-bridge`, which run
   `venv/bin/python -I -m ai_bridge_cli`. Every launcher change is journaled in
   `~/.local/share/ai-bridge-cli/launchers.json` *before* it is published. If any step fails,
   the previous launchers and the previous venv are put back. If the process is killed midway,
   the next activation or rollback recovers the durable venv rename journal
   (`venv-transaction.json`) before proceeding. Recovery can itself be interrupted and retried.
   A rollback consumes `venv.previous`, so repeating it leaves the restored environment in place;
   the displaced environment is retained as `venv.rolled-back-*` until uninstall.

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
| `1` / `2` / `3` / `4` / `5` | focus search / sessions / details / filters / open settings |
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
| `Ctrl+S` / `F2` | settings: close/exec native (default) or suspend and return |
| `q` | quit |

The layout adapts to the terminal width. Wide terminals show the details pane beside the list.
Title and Last Model appear first; secondary columns (updated, status, origin, project)
appear as space permits. Long models are ellipsized in the list; `p` shows the full value
in details, including on narrow terminals. Medium terminals hide the project filter, and
narrow terminals use short agent names and details as a full-screen toggle. Store errors,
an empty store, or no matches are shown in place rather than as an empty table.

The numbered navigation labels stay visible above the search bar. Digits type normally in
search and other text inputs; press Enter or Esc to leave search before using them to navigate.
Dialogs and expanded filter menus retain their own keyboard handling. `3` reveals hidden
details; `2` returns to the session list on narrow terminals. `4` reveals all filters vertically
when space is limited; `2`, `3` or Esc collapses them again.

Last Model uses Claude assistant message models (excluding sidechains and synthetic placeholders),
Codex turn-context models, and Cursor chat metadata's `lastUsedModel`. Missing or invalid newer
Claude/Codex records do not erase an earlier known model. `—` means unknown; configured agent defaults are
never substituted. Claude/Codex model lookup reads backward with bounded memory, including past
large tool outputs. Cursor chats without `lastUsedModel` remain unknown.
The existing JSON field `last_used_model` remains a string or `null`; the JSON schema is unchanged.
Headless `sessions inspect` also includes the complete value.

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
                        [--allow-uninspectable]                  # HIGH RISK, see "Archive vs. delete"
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
| **Direct** | *Proven* not MCP. Either this account has no ai-bridge state and no installation at all (checked with `lstat`), or there is no record, the session was created after the provenance epoch (the earliest verified `first_seen`, which shows recording was active), no bridge job references it, and every bridge job record could be read. |
| **Unknown** | Everything else: no record but no proof (for example a session older than verified provenance recording, which may have been used through MCP before records existed); a record, directory or path component that fails any check, including dangling symlinks; or an installed bridge without a provenance directory. |

A headless or exec launch mode is **never** treated as MCP evidence. This tool never creates,
locks or writes anything in the provenance directory.

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

**Delete (move to trash)** is explicit. It always shows a preview first and never unlinks anything.

* **Preview / plan.** Every file records its device, inode, type, size, mtime and ctime. Every
  directory also records a bounded, non-following, recursive fingerprint of *every object inside*.
  This catches in-place writes to `store.db` or its WAL and changes to nested Claude files even
  when timestamps are forged. The newest mtime anywhere in the tree must be at least 2 minutes old.
* **Execute.** Re-plans from scratch and requires the same plan ID. For each session it then
  takes a fresh process snapshot, and immediately before each rename it re-validates the identity
  and fingerprint. Renames use `renameat2(RENAME_NOREPLACE)` and **fail closed** where atomic
  no-replace is unsupported (there is no check-then-rename fallback).
* **Write-ahead journal.** Each entry is durably written to the batch manifest, with its stored
  name and identity, *before* it is renamed: `planned → moving → in_trash`. The manifest, the
  source directory and the trash directory are all fsynced. After a crash or failed write,
  restore and purge first resolve every entry by locating the object physically, so nothing is
  ever stranded untracked. Restore resumes partial and interrupted batches. Purge refuses any
  batch with unresolved or partially restored sessions.

| Agent | Method | Restore |
| --- | --- | --- |
| Claude Code | The transcript `<id>.jsonl`, its `<id>/` sidecar folder (subagents, tool results), `file-history/<id>` and `session-env/<id>` are renamed into `~/.local/share/ai-bridge-cli/trash/<batch>/` with a manifest. | `trash restore` puts every path back and never overwrites. |
| Cursor CLI | The chat folder (`store.db` with its WAL/SHM) and `projects/*/agent-transcripts/<id>` are moved the same way. | Same. |
| Codex | Rollout files are indexed by Codex's SQLite state, so this tool **never moves or deletes them**. It runs the installed `codex archive <UUID>` after checking that `codex archive --help` advertises it. If not, delete is reported as unsupported. | `codex unarchive`, via `trash restore`. |

Delete refuses when any of these hold:

* the session is active, uncertain or recent, has unfinished bridge jobs, or anything in it was
  modified in the last 2 minutes;
* process inspection is incomplete. This covers `/proc` unavailable, `/proc/locks` or a command
  line unreadable, an unreadable Claude session-registry entry, and a same-user process whose
  open files cannot be listed (non-dumpable processes, for example running `codex`). A
  reported allow-list of session managers (`(sd-pam)`, `sshd`, `sshd-session`, `systemd`) is
  tolerated. Confirmed zombie/dead processes are ignored because they hold no open files.
  `sessions delete --allow-uninspectable` overrides this as an explicit, high-risk
  choice that is recorded in the plan ID; the TUI never overrides it;
* any bridge job record is unreadable, or an unfinished bridge job of the same agent has no known
  session yet;
* a directory contains symlinks, special files or foreign-owned files, or has more than 20,000 entries;
* a path is a symlink, a hard link, a special file, not yours, outside the configured store, or
  reached through a symlink below the store root (every component is opened with `O_NOFOLLOW`);
* the trash is on a different filesystem, because moves must be atomic renames and there is no
  copy-then-delete;
* the session ID is ambiguous.

`trash purge BATCH --yes` permanently removes one fully resolved batch. It is the only real
deletion, and only of data this tool moved. Purging leaves Codex items archived by Codex.

If a preview says **Running processes cannot be fully inspected**, the checker cannot rule out
open chat files; it has not proved that the selected chat is active. Linux can deny access to
`/proc/PID/fd` for non-dumpable agents, ptrace/security restrictions or restricted proc mounts.
Check the listed processes with `ps -p PID1,PID2 -o pid,ppid,user,stat,comm`.
`Z` means an exited zombie awaiting its parent's reap; this version ignores confirmed zombies.
Do not kill a parent agent just to clear zombies. For live agents, exit normally, wait at least
120 seconds after the last write, refresh, then request a new preview:

```bash
ai-bridge sessions delete agent:EXACT-UUID --dry-run
```

Preview does not delete chats. If live inspection remains blocked, archive with `a` to hide the
chat without modifying native files, or leave it alone. The TUI never adds
`--allow-uninspectable`. Any explicit CLI override still requires its own matching preview/plan
ID and does not bypass known activity, recent writes, storage checks or unfinished jobs.

All native-store reads are bounded and opened `O_NOFOLLOW|O_NONBLOCK`, and must be regular
files. A FIFO, device or symlink named like a session cannot hang or redirect a scan. One
unreadable file becomes one error row; it never hides the rest of its agent.

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

The browser confirms the command, project directory and launch behavior before doing anything.
By default **Open and Fork close the browser completely**. Textual restores the terminal,
activity/storage/directory/executable are rechecked, then `exec` replaces Python with the native
CLI in the saved project directory. There is no Python parent waiting for the agent; tmux can
observe the native process. Exiting the agent does not return to the browser. If the final
recheck or exec fails after teardown, a plain terminal error explains how to reopen the browser.
Earlier refusals and cancelled confirmations keep the browser open.

An executed child cannot change the waiting parent shell's working directory. Without shell
integration, exiting the native agent returns to the directory where you ran `ai-bridge`
(for example HOME), even though the agent ran in the saved project.

To opt into returning Bash to the saved project after a close/exec launch, run this in your
interactive Bash, and add the same line to `~/.bashrc` if you want it in future shells:

```bash
eval "$(command ai-bridge shell-init bash)"
```

This defines functions for both `ai-bridge` and `ai-bridge-cli`. They call the installed command,
wait for it, then use Bash's own `cd` on the saved launch directory. Native launch still directly
replaces Python, preserving its PID and allowing tmux's `#{pane_current_command}` automatic
rename to see the agent. No tmux panes/windows are created, and no tmux or shell config is edited.
Each invocation uses a fresh private temporary directory with a NUL-terminated cwd handoff;
paths are read as data, never evaluated. The handoff variable is stripped from the native
environment. The wrapper cleans up on ordinary return and preserves the command's exit status,
including nonzero native exits. Cancellation, dry runs, failed exec and suspend mode do not
change the parent shell's cwd. If the project disappears before return, Bash reports the failed
`cd` and keeps its current directory. Killing the shell itself can leave a temporary directory.

Use the function in the shell you want to move: `command ai-bridge`, an external script,
`bash -c`, a subshell or `exec ai-bridge` bypasses the parent-shell handoff. Only Bash integration
is provided. Disable for the current shell with `unset -f ai-bridge ai-bridge-cli _ai_bridge_shell_run`.
The returned directory is the saved launch project, not any directory the native agent later visits.
With tmux bindings that use `-c '#{pane_current_path}'`, future panes inherit the project once
the waiting shell has changed directory. Such bindings alone cannot change an existing shell's cwd.

Press **Ctrl+S or F2** to choose **A: close/exec (default)** or **B: suspend and return**.
Use arrow keys then Enter to save, or Esc to cancel. B keeps the browser alive while the native
child runs, restores it afterwards and refreshes the listing. Both modes require an interactive
terminal; use `sessions open agent:UUID --dry-run` for a headless launch preview.

The preference is per user at `$XDG_CONFIG_HOME/ai-bridge-cli/settings.json` (default
`~/.config/ai-bridge-cli/settings.json`), separate from the MCP configuration and native stores.
It is written atomically with fsync inside an owner-only directory (0700), as an owner-only
regular file (0600). Symlinks, hard links, foreign ownership, shared permissions and malformed
settings are refused. An unreadable config shows a warning and uses the close/exec default;
a failed save leaves the current preference in effect. Reading defaults creates no files.

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
