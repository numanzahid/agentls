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
* **Medium (90–129):** filters on their own rows under search, three per line; details toggle
  full screen.
* **Narrow (<90):** filters two per line, short agent names, fewer columns.

When the list is short of space, columns drop in this order: Last Model, Updated, Origin, Status,
Project (which first shrinks to 10 columns). A column never shows while one before it in that
order is hidden. Title always shows. The sort column's header carries an arrow (`Updated↓`).

The list's top border shows **shown/total sessions**. Its bottom border shows, from left to right:
scanning or error notes, **N selected** (the visible marked rows an action will affect), the
archived and non-interactive counts (bright when shown, dim when filtered out), and the cursor
position (`2 of 30`).

Colors come from the terminal's 16-color palette, so the browser follows your terminal theme
(Omarchy included), like lazygit. The focused pane's border is highlighted.

The details pane ends with a short preview: your last 5 messages and the agent's last reply.
Tool calls and their output are left out; open the chat for the full history.

## Filters

Agent, Mode, Origin, Status and Project. **Mode** defaults to **Interactive**, which hides
non-interactive chats (`codex exec`, `claude -p`); the list's bottom border counts them.
Choose **All modes** or **Non-interactive** to show them. Subagents take their parent's mode.
Cursor CLI records no launch mode, so its chats always show. The headless `sessions list` is
unfiltered by default; use `--mode`.

## Columns

**Title** marks non-interactive chats with `⚙` and subagents with `↳`.

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
