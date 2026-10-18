# Usage

## Browser keys

| Key | Action |
| --- | --- |
| `↑ ↓ PgUp PgDn Home End` | move |
| `/` | search title, alias, project, ID and model; `Enter` or `Esc` returns to the list and keeps the query |
| `Ctrl+U` (in search) | clear the query |
| `Esc` | back to the list; closes dropdowns and dialogs; never clears search |
| `Tab` | move between search, filters and the list |
| `1`–`5` | focus search / filters / list / details, or open settings |
| `s` / `S` / click header | cycle sort key / reverse |
| `Space`, `*`, `Esc` | mark row, mark all visible (again to clear), clear marks |
| `Enter` / `o` | open (resume) in the native CLI |
| `f` | fork (Codex `fork`, Claude `--resume --fork-session`) |
| `n` | rename (display alias; empty clears) |
| `a` | archive / unarchive the marked rows (or the current row) |
| `d` / `Del` | removal menu: archive, delete, stop, force delete |
| `x` / `D` | stop session / force delete |
| `t` | trash: restore, or `P` to purge a batch |
| `A` | show / hide archived sessions |
| `p` | details and preview |
| `y` | copy `agent:ID` to the clipboard (OSC 52) |
| `r` / `F5` | refresh (also automatic every 20 s) |
| `Ctrl+S` / `F2` | settings |
| `?` / `q` | help / quit |

Digits type normally inside search and other inputs; press `Enter` or `Esc` first to navigate.

## Layout

* **Wide (130+ columns):** details beside the list; search and filters on one row.
* **Medium (90–129):** filters on their own row under search; details toggle full screen.
* **Narrow (<90):** filters two per line, short agent names, fewer columns.

The focused pane's border is highlighted. When rows are marked, the status bar shows
**N selected / N visible**: the sessions an action will affect.

## Columns

**Last Model** is the most recently recorded model: Claude assistant messages (no sidechains or
placeholders), Codex turn contexts, Cursor's `lastUsedModel`. `—` means unknown; agent defaults
are never substituted. `p` shows the full value.

**Status** is taken from one snapshot per refresh:

| Status | Meaning |
| --- | --- |
| active | in use now: a verified bridge job, a live registered Claude process, a process holding the file open or locked, or one started with its ID |
| uncertain | a bridge job is unfinished but its process cannot be verified |
| recent | modified in the last 2 minutes |
| idle | none of the above |

**Origin** (MCP / Direct / Unknown) is shown only from ai-bridge records; see
[bridge.md](bridge.md#origin).

## Headless commands

Output is a table on a terminal and JSON otherwise (`--json` / `--table` to force). Errors on a
non-terminal are JSON on stderr. Exit codes: `0` ok, `1` error, `2` usage, `3` partial.

```bash
ai-bridge sessions list [--agent claude|codex|cursor ...] [--origin mcp|direct|unknown]
                        [--status active|uncertain|recent|idle] [--project DIR] [--search WORDS]
                        [--sort updated|title|agent|project] [--reverse]
                        [--archived hide|include|only] [--native-archived] [--all]
                        [--mode ...] [--type ...] [--task-id ...] [--limit N] [--offset N]
ai-bridge sessions inspect agent:UUID|ID-PREFIX [--history N]
ai-bridge sessions open|resume|fork agent:UUID [--dry-run]
ai-bridge sessions rename agent:UUID "New name" | --clear
ai-bridge sessions archive|unarchive agent:UUID [...]
ai-bridge sessions delete agent:UUID [...] --dry-run            # preview; prints a plan id
ai-bridge sessions delete agent:UUID [...] --yes --plan-id ID   # run exactly that plan
ai-bridge sessions trash list | restore BATCH|codex:UUID [--dry-run] | purge BATCH --yes
```

Selectors are `agent:UUID`. `inspect` also accepts a unique ID prefix of 8+ characters; anything
that changes state needs the exact ID.
