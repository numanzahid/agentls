# Removing sessions

| Action | Key | Effect | Undo |
| --- | --- | --- | --- |
| Archive | `a` | hides the session; native files untouched | unarchive |
| Delete | `d` → `d` | moves files to this tool's trash (Codex: `codex archive`) | `trash restore` |
| Stop session | `x` | stops the running agent; keeps the chat | — |
| Force delete | `D` | stop, then delete (Codex: `codex delete --force`) | Claude/Cursor: trash; Codex: **none** |
| Permanent delete | `A`, archived Codex row, `d` | deletes an already archived Codex chat | **none** |

On Codex rows the menu labels Archive and Delete as **Hide** and **Native archive**. Every dialog
opens with Cancel focused. If any selected chat is blocked, nothing in the selection
is changed. Headless `sessions delete` only does recoverable removal.

## Archive

Aliases and archive flags are stored in `~/.local/state/ai-bridge-cli/overlays.json`. Flags
written by older ai-bridge CLIs in the server's state directory are still read, never written.

## Delete (trash)

| Agent | What moves | Restore |
| --- | --- | --- |
| Claude Code | `<id>.jsonl`, its `<id>/` folder, `file-history/<id>`, `session-env/<id>` | `trash restore` |
| Cursor CLI | the chat folder (`store.db` + WAL/SHM) and `agent-transcripts/<id>` | `trash restore` |
| Codex | nothing moved by this tool; runs the installed `codex archive <UUID>` | `codex unarchive` via `trash restore` |

Trash lives in `~/.local/share/ai-bridge-cli/trash/<batch>/`. `trash purge BATCH --yes`
permanently removes one fully resolved batch.

**Refused when:**

* the session is active, uncertain or recent, or anything in it changed in the last 2 minutes;
* a bridge job record is unreadable, or an unfinished same-agent job has no known session yet;
* process inspection is incomplete (for example a non-dumpable `codex` process whose open files
  cannot be listed); see below;
* a path is a symlink, hard link, special file, not yours, outside the store, or under a
  symlinked directory, or a folder has over 20,000 entries;
* the trash is on another filesystem (moves are atomic renames only), or the ID is ambiguous.

**How it stays safe:** the preview fingerprints every file and folder (inode, size, times, and
the full tree), and execution requires the same plan ID. Each object is revalidated just before
an atomic `renameat2(RENAME_NOREPLACE)`; there is no fallback. A write-ahead journal records each
move before it happens, so a crash never strands files, and restore resumes interrupted batches.

### "Running processes cannot be fully inspected"

The checker could not rule out open chat files; it has not shown the chat is active. List the
processes with `ps -p PID1,PID2 -o pid,ppid,user,stat,comm`. `Z` (zombie) entries are ignored;
don't kill a parent agent to clear them. Exit live agents normally, wait 2 minutes, refresh, and
preview again. Otherwise, archive instead.

`sessions delete --allow-uninspectable` overrides this check only. It is high risk, recorded in
the plan ID, never used by the TUI, and does not bypass any other check.

## Stop session

Keeps chat files.

* **Bridge jobs** are stopped through the installed server's `agents.stop`, after verifying the
  job's private records and saved process identities. A second confirmation lists the jobs and
  their background processes and authorizes server cleanup, which may include SIGKILL. A
  cancellation request alone is not reported as stopped.
* **Native processes** are stopped only when exclusively identified: the user's own native
  executable running exactly `codex resume UUID`, `claude --resume UUID` or
  `agent`/`cursor-agent --resume UUID`, with matching start time and session file. Wrapped commands,
  shared holders, children, sockets, daemons and incomplete inspection are refused. A pidfd
  pins the process before SIGTERM; there is no automatic SIGKILL. When refused, close the client
  yourself and refresh.

## Force delete

* **Claude / Cursor:** a verified stop, then the normal trash move with all its checks. Related
  descendant sessions are previewed together. If stopping flushes new writes, wait 2 minutes and
  retry. Codex and other agents cannot be mixed in one force delete.
* **Codex:** runs `codex delete --force UUID` (only if `codex delete --help` advertises it) on
  archived or unarchived chats, including active ones. Identity, storage, index and descendant
  checks are repeated after confirmation. Codex itself handles cancellation and may refuse. No
  daemon is killed and no files or SQLite rows are removed by hand. Success requires the rollout
  files and index entries to be gone. **Irreversible.**

Permanent delete of an already archived Codex chat keeps stricter idle checks.

## Shared Codex app-server

A rollout held open by a verified managed Codex app-server can count as idle without stopping the
daemon. This narrow exception supports packaged Codex **0.163.0** only. It requires a verified
daemon identity and launch, complete inspection of every holder, a read-only `thread/read`
reporting `idle` or `notLoaded`, and no update in the last 120 seconds. Everything is rechecked
before archiving. Other versions and states stay blocked.
