"""Textual session browser.

Threading model: catalog refreshes, previews, preflights and file moves run in
thread workers; every widget update happens on the app thread. The UI reads
only `self.by_key` / `self.shown`, which are swapped in on the app thread
after a refresh completes, so a background scan never shows partial state.
"""
from __future__ import annotations

import os
import shlex
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime

from rich.table import Table
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult, SuspendNotSupported
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.css.query import NoMatches
from textual.widgets import DataTable, Footer, Header, Input, Select, Static

from .. import native, paths, settings, trash
from ..catalog import Catalog, Query
from ..model import SORT_KEYS, Session
from ..output import relative
from ..scanners import history
from ..textsafe import one_line, safe
from .screens import ChoiceScreen, ConfirmScreen, HelpScreen, InputScreen, SettingsScreen, TrashScreen, plan_table

AGENT_STYLE = {"claude": "#d97757", "codex": "#10a37f", "cursor": "#5b8def"}
AGENT_SHORT = {"claude": "cl", "codex": "cx", "cursor": "cu"}
STATUS_STYLE = {"active": "bold green", "uncertain": "bold yellow", "recent": "cyan", "idle": "dim"}
STATUS_ICON = {"active": "●", "uncertain": "◐", "recent": "○", "idle": "·"}
ORIGIN_LABEL = {"mcp": ("MCP", "bold magenta"), "direct": ("Direct", ""), "unknown": ("Unknown", "dim")}
AUTO_REFRESH_SECONDS = 20
PREVIEW_MESSAGES = 40
WIDE, MEDIUM = 130, 90


@dataclass(frozen=True)
class NativeHandoff:
    session: Session
    plan: native.LaunchPlan


def home_short(path: str | None) -> str:
    if not path:
        return "-"
    home = str(paths.home())
    return "~" + path[len(home):] if path == home or path.startswith(home + "/") else path


def stamp(value) -> str:
    if not value:
        return "-"
    try:
        return datetime.fromtimestamp(float(value)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OverflowError, OSError):
        return safe(value)


class SessionBrowser(App):
    CSS_PATH = "app.tcss"
    TITLE = "ai-bridge"
    SUB_TITLE = "local agent sessions"

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("question_mark", "help", "Help"),
        Binding("ctrl+s,f2", "settings", "Settings", priority=True),
        Binding("slash", "focus_search", "Search"),
        Binding("o", "open", "Open"),
        Binding("space", "toggle_mark", "Mark", show=False),
        Binding("asterisk", "mark_all", "Mark all", show=False),
        Binding("n", "rename", "Rename"),
        Binding("a", "archive", "Archive"),
        Binding("d,delete", "delete", "Delete…"),
        Binding("t", "trash", "Trash"),
        Binding("s", "cycle_sort", "Sort"),
        Binding("S", "reverse_sort", "Reverse sort", show=False),
        Binding("A", "toggle_archived", "Archived", show=False),
        Binding("p", "toggle_details", "Details"),
        Binding("r,f5", "refresh", "Refresh"),
        Binding("f", "fork", "Fork", show=False),
        Binding("y", "copy_id", "Copy ID", show=False),
        Binding("escape", "escape", "Back", show=False),
    ]

    def __init__(self, catalog: Catalog | None = None, periodic_refresh: bool = True, probe_activity: bool = True):
        super().__init__()
        self.catalog = catalog or Catalog()
        self.periodic_refresh = periodic_refresh
        self.probe_activity = probe_activity
        self.query_state = Query()
        self.by_key: dict[str, Session] = {}
        self.shown: list[Session] = []
        self.marked: set[str] = set()
        self.tier = ""
        self.details_open = True
        self.scanning = False
        self.loaded = False
        self.last_error: str | None = None
        self._preview_cache: dict[tuple, list[dict] | str] = {}
        self._preview_timer = None
        self._preview_key: str | None = None
        self._refresh_lock = threading.Lock()
        self.settings = settings.Settings()
        self.settings_error: str | None = None
        try:
            self.settings = settings.load()
        except (OSError, ValueError) as error:
            self.settings_error = f"CLI settings could not be loaded; using close/exec default: {error}"

    def _q(self, selector: str, expect=None):
        """Main-screen widgets, even while a modal is on top (App.query_one uses the active screen)."""
        screen = self.screen_stack[0] if self.screen_stack else self.screen
        return screen.query_one(selector, expect) if expect is not None else screen.query_one(selector)

    def _alive(self) -> bool:
        """Timers/worker callbacks may land during shutdown, after widgets are gone."""
        if not self.is_running or not self.screen_stack:
            return False
        try:
            self._q("#table", DataTable)
            return True
        except NoMatches:
            return False

    # -- layout -------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="filters"):
            yield Input(placeholder="Search title, project, ID, model…  ( / )", id="search")
            yield Select([("All agents", "all"), ("Claude Code", "claude"), ("Codex", "codex"),
                          ("Cursor", "cursor")], value="all", allow_blank=False, id="agent", compact=True)
            yield Select([("Any origin", "all"), ("MCP (bridge)", "mcp"), ("Direct", "direct"),
                          ("Unknown", "unknown")], value="all", allow_blank=False, id="origin", compact=True)
            yield Select([("Any status", "all"), ("Active", "active"), ("Uncertain", "uncertain"),
                          ("Recent", "recent"), ("Idle", "idle")], value="all", allow_blank=False, id="status",
                         compact=True)
            yield Select([("All projects", "all")], value="all", allow_blank=False, id="project", compact=True)
        with Horizontal(id="body"):
            yield DataTable(id="table", cursor_type="row", zebra_stripes=True)
            with VerticalScroll(id="side"):
                yield Static(id="details")
                yield Static(id="preview")
        yield Static(Text("Scanning sessions…"), id="empty")
        yield Static(id="statusbar")
        yield Footer()

    def on_mount(self) -> None:
        if self.settings_error:
            self.notify(self.settings_error, severity="warning", markup=False, timeout=15)
        self._q("#table", DataTable).focus()
        self._apply_tier(force=True)
        self.refresh_catalog(initial=True)
        if self.periodic_refresh:
            self.set_interval(AUTO_REFRESH_SECONDS, self._auto_refresh)

    def on_resize(self) -> None:
        # App/screen geometry is updated after the resize event is dispatched.
        self.call_after_refresh(self._apply_tier)

    def _apply_tier(self, force: bool = False) -> None:
        width = self.size.width
        tier = "wide" if width >= WIDE else "medium" if width >= MEDIUM else "narrow"
        changed = tier != self.tier
        self.tier = tier
        self.set_class(tier == "narrow", "narrow")
        self.set_class(tier == "medium", "medium")
        # Side pane: beside the table when wide; otherwise toggled to a full view.
        if changed or force:
            self.details_open = tier == "wide"
        self._layout_side()
        if self.loaded:
            self._rebuild_table()

    def _layout_side(self) -> None:
        side, table = self._q("#side"), self._q("#table", DataTable)
        if self.tier == "wide":
            side.display = self.details_open
            table.display = True
            side.set_class(False, "full")
        else:
            side.display = self.details_open
            table.display = not self.details_open
            side.set_class(True, "full")

    # -- data ---------------------------------------------------------------
    @work(thread=True, exclusive=True, group="refresh")
    def refresh_catalog(self, initial: bool = False, quiet: bool = False) -> None:
        self.call_from_thread(self._set_scanning, True)
        sessions = None
        # Superseded thread workers keep running; never let two scans interleave.
        with self._refresh_lock:
            try:
                self.catalog.refresh(probe_activity=self.probe_activity)
                sessions, error = list(self.catalog.sessions), None
            except Exception as exc:  # keep the previous listing on failure
                error = f"Refresh failed: {type(exc).__name__}: {exc}"
        self.call_from_thread(self._refresh_done, error, quiet, sessions)

    def _set_scanning(self, value: bool) -> None:
        if not self._alive():
            return
        self.scanning = value
        self._update_status()

    def _refresh_done(self, error: str | None, quiet: bool, sessions: list[Session] | None = None) -> None:
        if not self._alive():
            return
        self.scanning = False
        if error:
            self.last_error = error
            self.notify(error, severity="error", markup=False, timeout=10)
        else:
            self.last_error = None
            self.by_key = {s.selector: s for s in sessions or []}
            self.marked &= set(self.by_key)
            self._preview_cache = {k: v for k, v in self._preview_cache.items() if k[0] in self.by_key}
            if not quiet:
                for agent, message in self.catalog.errors.items():
                    self.notify(f"{agent}: {message}", title="Store unreadable", severity="warning",
                                markup=False, timeout=10)
                for warning in self.catalog.warnings:
                    self.notify(warning, severity="warning", markup=False, timeout=10)
            self._update_project_options()
        self.loaded = True
        self._rebuild_table()

    def _auto_refresh(self) -> None:
        if len(self.screen_stack) == 1 and not self.scanning:
            self.refresh_catalog(quiet=True)

    def _update_project_options(self) -> None:
        select = self._q("#project", Select)
        current = select.value
        projects = self.catalog.projects()
        options = [("All projects", "all")] + [(one_line(home_short(p), 60), p) for p in projects]
        with self.prevent(Select.Changed):
            select.set_options(options)
            select.value = current if current in projects else "all"
        if current != "all" and current not in projects:
            self.query_state.project = None

    # -- table --------------------------------------------------------------
    def _columns(self) -> list[tuple[str, str, int | None]]:
        width = self.size.width
        if self.tier == "wide" and self.details_open:
            width -= max(40, int(width * 0.38))
        # Keep the title and model before secondary metadata. Reserve the gutter
        # and padding explicitly so the title never falls off-screen.
        compact = self.tier == "narrow"
        cols = [("mark", " ", 1), ("agent", "Ag" if compact else "Agent", 2 if compact else 6)]
        model_width = min(28, max(10, width // 5))
        cols += [("title", "Title", 0), ("model", "Last Model", model_width)]
        for column in [("updated", "Updated", 8), ("status", "Status", 9),
                       ("origin", "Origin", 7), ("project", "Project", 22)]:
            used = sum(c[2] + 2 for c in cols) + column[2] + 2 + 2
            if width - used >= 20:
                cols.append(column)
        used = sum(c[2] + 2 for c in cols) + 2
        cols[2] = ("title", "Title", max(1, width - used))
        return cols

    def _cell(self, s: Session, column: str, now: float):
        if column == "mark":
            return Text("●", style="bold yellow") if s.selector in self.marked else Text(" ")
        if column == "agent":
            label = AGENT_SHORT[s.agent] if self.tier == "narrow" else s.agent
            return Text(label, style=AGENT_STYLE[s.agent])
        if column == "origin":
            label, style = ORIGIN_LABEL.get(s.origin, ("?", "dim"))
            return Text(label, style=style)
        if column == "status":
            style = STATUS_STYLE.get(s.status, "")
            return Text(STATUS_ICON.get(s.status, "?") if self.tier == "narrow" else s.status, style=style)
        if column == "updated":
            return Text(relative(s.updated, now).replace(" ago", ""), style="dim" if s.status == "idle" else "")
        if column == "model":
            return Text(safe(s.last_used_model or "—"), overflow="ellipsis", no_wrap=True)
        if column == "project":
            return Text(one_line(home_short(s.cwd), 200), style="dim", overflow="ellipsis", no_wrap=True)
        tags = []
        if s.archived:
            tags.append(("[archived] ", "italic dim"))
        if s.native_archived:
            tags.append(("[codex-archived] ", "italic dim"))
        if s.error:
            tags.append(("⚠ ", "bold red"))
        if s.session_type == "subagent":
            tags.append(("↳ ", "dim"))
        title = Text.assemble(*tags, (one_line(s.display_title, 300), "italic" if s.alias else ""))
        title.no_wrap, title.overflow = True, "ellipsis"
        return title

    def _rebuild_table(self) -> None:
        table = self._q("#table", DataTable)
        current = self._current_key()
        try:  # query the app-thread snapshot, never the catalog a worker may be refreshing
            self.shown = self.catalog.query(self.query_state, list(self.by_key.values()))
        except ValueError as error:
            self.notify(str(error), severity="error", markup=False)
            self.shown = []
        columns = self._columns()
        now = time.time()
        with self.batch_update():
            table.clear(columns=True)
            for key, label, width in columns:
                table.add_column(Text(label, style="bold"), key=key, width=width)
            for s in self.shown:
                table.add_row(*(self._cell(s, key, now) for key, _, _ in columns), key=s.selector)
            if self.shown:
                keys = [s.selector for s in self.shown]
                table.move_cursor(row=keys.index(current) if current in keys else 0, animate=False)
        self._update_empty()
        self._update_status()
        self._schedule_preview()

    def _refresh_rows(self, keys: set[str]) -> None:
        """Re-render only some rows (marks, alias, archive) without rebuilding."""
        table = self._q("#table", DataTable)
        now = time.time()
        columns = [c[0] for c in self._columns()]
        for key in keys:
            s = self.by_key.get(key)
            if s is None or key not in table.rows:
                continue
            for column in columns:
                table.update_cell(key, column, self._cell(s, column, now))
        self._update_status()

    def _update_empty(self) -> None:
        empty = self._q("#empty", Static)
        if self.shown:
            empty.display = False
            return
        empty.display = True
        if not self.loaded or (self.scanning and not self.by_key):
            empty.update(Text("Scanning sessions…", style="bold"))
        elif not self.by_key:
            roots = "\n".join(f"  {a}: {home_short(r)}" for a, r in
                              ((a, str(paths.agent_root(a))) for a in paths.AGENTS))
            empty.update(Text(f"No sessions found in the native stores:\n{roots}\n\n"
                              "Set CLAUDE_CONFIG_DIR, CODEX_HOME or AI_BRIDGE_CURSOR_HOME if they live "
                              "elsewhere, then press r.", style="dim"))
        else:
            hidden = sum(1 for s in self.by_key.values() if s.archived)
            hint = "  Press A to show archived sessions." if hidden and self.query_state.archived == "hide" else ""
            empty.update(Text(f"No sessions match the current search and filters.{hint}\n"
                              "Esc clears the search; set filters back to All/Any.", style="dim"))

    def _update_status(self) -> None:
        q = self.query_state
        marked = len(self.marked & {s.selector for s in self.shown})
        arrow = "↑" if q.reverse else "↓"
        parts = [Text(f"{len(self.shown)}/{len(self.by_key)} sessions", style="bold")]
        if marked:
            parts.append(Text(f"{marked} marked", style="bold yellow"))
        parts.append(Text(f"sort {q.sort}{arrow}"))
        if q.archived != "hide":
            parts.append(Text("showing archived", style="italic"))
        else:
            hidden = sum(1 for s in self.by_key.values() if s.archived or s.native_archived)
            if hidden:
                parts.append(Text(f"{hidden} archived hidden (A)", style="dim"))
        state = self.catalog.provenance_state
        parts.append(Text("provenance ok" if state == "ok" else f"provenance {state}",
                          style="" if state == "ok" else "dim"))
        if self.catalog.errors:
            parts.append(Text("⚠ " + ", ".join(self.catalog.errors), style="bold red"))
        if self.last_error:
            parts.append(Text("⚠ refresh failed", style="bold red"))
        if self.scanning:
            parts.append(Text("scanning…", style="italic cyan"))
        self._q("#statusbar", Static).update(Text(" · ").join(parts))

    # -- selection & preview ------------------------------------------------
    def _current_key(self) -> str | None:
        table = self._q("#table", DataTable)
        if not table.row_count:
            return None
        try:
            return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        except Exception:
            return None

    def current(self) -> Session | None:
        key = self._current_key()
        return self.by_key.get(key) if key else None

    def targets(self) -> list[Session]:
        visible = {s.selector for s in self.shown}
        marked = [self.by_key[k] for k in self.marked if k in visible and k in self.by_key]
        if marked:
            return sorted(marked, key=lambda s: -s.updated)
        current = self.current()
        return [current] if current else []

    @on(DataTable.RowHighlighted, "#table")
    def _highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._schedule_preview()

    @on(DataTable.RowSelected, "#table")
    def _selected(self, event: DataTable.RowSelected) -> None:
        self.action_open()

    @on(DataTable.HeaderSelected, "#table")
    def _header(self, event: DataTable.HeaderSelected) -> None:
        key = {"updated": "updated", "title": "title", "agent": "agent", "project": "project"}.get(
            event.column_key.value)
        if key:
            if self.query_state.sort == key:
                self.query_state.reverse = not self.query_state.reverse
            else:
                self.query_state.sort, self.query_state.reverse = key, False
            self._rebuild_table()

    def _schedule_preview(self) -> None:
        if not self._alive():
            return
        session = self.current()
        self._render_details(session)
        if self._preview_timer:
            self._preview_timer.stop()
        if session is None or not self.details_open:
            return
        self._preview_timer = self.set_timer(0.15, lambda: self._start_preview(session))

    def _start_preview(self, session: Session) -> None:
        if not self._alive():
            return
        cache_key = (session.selector, session.updated, session.storage_path)
        self._preview_key = session.selector
        if cache_key in self._preview_cache:
            self._render_preview(session, self._preview_cache[cache_key])
            return
        self._q("#preview", Static).update(Text("Loading preview…", style="dim italic"))
        self.load_preview(session, cache_key)

    @work(thread=True, exclusive=True, group="preview")
    def load_preview(self, session: Session, cache_key: tuple) -> None:
        try:
            messages: list[dict] | str = history(session)[-PREVIEW_MESSAGES:]
        except Exception as error:
            messages = f"Preview unavailable: {type(error).__name__}: {error}"
        self.call_from_thread(self._preview_loaded, session, cache_key, messages)

    def _preview_loaded(self, session: Session, cache_key: tuple, messages) -> None:
        if not self._alive():
            return
        self._preview_cache[cache_key] = messages
        if self._preview_key == session.selector:
            self._render_preview(session, messages)

    def _render_details(self, s: Session | None) -> None:
        details = self._q("#details", Static)
        if s is None:
            details.update(Text("No session selected.", style="dim"))
            self._q("#preview", Static).update("")
            return
        grid = Table.grid(padding=(0, 1), expand=True)
        grid.add_column(style="bold", no_wrap=True)
        grid.add_column(overflow="fold", ratio=1)
        label, style = ORIGIN_LABEL.get(s.origin, ("?", ""))
        rows = [
            ("Title", Text(one_line(s.display_title, 500), style="bold")),
            ("Native title", Text(one_line(s.title, 500))) if s.alias else None,
            ("Session", Text(s.selector)),
            ("Origin", Text.assemble((label, style), (f"  {s.origin_note}" if s.origin_note else "", "dim"))),
            ("Status", Text.assemble((s.status, STATUS_STYLE.get(s.status, "")),
                                     (f"  {s.status_note}" if s.status_note else "", "dim"))),
            ("Project", Text(safe(s.cwd or "-"))),
            ("Updated", Text(f"{stamp(s.updated)}  ({relative(s.updated)})")),
            ("Created", Text(stamp(s.created))) if s.created else None,
            ("Mode", Text(f"{s.mode} / {s.session_type}" + (f" / {s.native_source}" if s.native_source else ""))),
            ("Last Model", Text(safe(s.last_used_model or "—"))),
            ("Parent", Text(s.parent_session_id)) if s.parent_session_id else None,
            ("Resumable", Text("yes", style="green") if s.resumable else
             Text.assemble(("no", "red"), (f"  {s.resume_note or ''}", "dim"))),
            ("Bridge jobs", Text(", ".join(s.job_ids[-5:]))) if s.job_ids else None,
            ("Task", Text(s.task_id)) if s.task_id else None,
            ("Archived", Text("yes (hidden by default)", style="italic")) if s.archived else None,
            ("Note", Text(safe(s.history_note), style="yellow")) if s.history_note else None,
            ("Error", Text(safe(s.error), style="red")) if s.error else None,
            ("Storage", Text(safe(s.storage_path), style="dim")),
        ]
        for row in rows:
            if row:
                grid.add_row(row[0], row[1])
        details.update(grid)

    def _render_preview(self, s: Session, messages) -> None:
        preview = self._q("#preview", Static)
        if isinstance(messages, str):
            preview.update(Text(messages, style="red"))
            return
        if not messages:
            preview.update(Text("No messages in this session.", style="dim"))
            return
        out = Text()
        out.append(f"Last {len(messages)} messages\n", style="bold underline")
        for message in messages:
            role = message.get("role", "?")
            style = {"user": "bold cyan", "assistant": "bold green", "tool": "dim"}.get(role, "")
            text = str(message.get("text", ""))
            limit = 240 if role == "tool" else 1200
            body = "\n".join(safe(line) for line in text[:limit].replace("\r", "").split("\n"))
            body += "…" if len(text) > limit else ""
            out.append(f"\n{role}: ", style=style)
            out.append(body, style="dim" if role == "tool" else "")
            out.append("\n")
        preview.update(out)

    # -- filters ------------------------------------------------------------
    @on(Input.Changed, "#search")
    def _search(self, event: Input.Changed) -> None:
        self.query_state.search = event.value
        self._rebuild_table()

    @on(Input.Submitted, "#search")
    def _search_done(self, event: Input.Submitted) -> None:
        self._q("#table", DataTable).focus()

    @on(Select.Changed)
    def _filter(self, event: Select.Changed) -> None:
        value = None if event.value in ("all", Select.BLANK) else event.value
        if event.select.id == "agent":
            self.query_state.agents = (value,) if value else ()
        elif event.select.id == "origin":
            self.query_state.origin = value
        elif event.select.id == "status":
            self.query_state.status = value
        elif event.select.id == "project":
            self.query_state.project = value
        self._rebuild_table()

    # -- actions: navigation ------------------------------------------------
    def action_focus_search(self) -> None:
        self._q("#search", Input).focus()

    def action_escape(self) -> None:
        search = self._q("#search", Input)
        if self.focused is search:
            if search.value:
                search.value = ""
            self._q("#table", DataTable).focus()
        elif self.details_open and self.tier != "wide":
            self.action_toggle_details()
        elif self.marked:
            changed = set(self.marked)
            self.marked.clear()
            self._refresh_rows(changed)
        elif search.value:
            search.value = ""

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_refresh(self) -> None:
        self.refresh_catalog()

    def action_cycle_sort(self) -> None:
        keys = list(SORT_KEYS)
        self.query_state.sort = keys[(keys.index(self.query_state.sort) + 1) % len(keys)]
        self.query_state.reverse = False
        self.notify(f"Sorted by {self.query_state.sort}", timeout=2, markup=False)
        self._rebuild_table()

    def action_reverse_sort(self) -> None:
        self.query_state.reverse = not self.query_state.reverse
        self._rebuild_table()

    def action_toggle_archived(self) -> None:
        self.query_state.archived = "include" if self.query_state.archived == "hide" else "hide"
        self.query_state.native_archived = self.query_state.archived == "include"
        self.notify("Showing archived sessions" if self.query_state.archived == "include"
                    else "Archived sessions hidden", timeout=2, markup=False)
        self._rebuild_table()

    def action_toggle_details(self) -> None:
        self.details_open = not self.details_open
        self._layout_side()
        self._rebuild_table()
        (self._q("#side") if self.details_open and self.tier != "wide"
         else self._q("#table", DataTable)).focus()
        self._schedule_preview()

    def action_copy_id(self) -> None:
        s = self.current()
        if s:
            self.copy_to_clipboard(s.selector)
            self.notify(f"Copied {s.selector}", timeout=3, markup=False)

    def action_toggle_mark(self) -> None:
        s = self.current()
        if not s:
            return
        self.marked.symmetric_difference_update({s.selector})
        self._refresh_rows({s.selector})
        table = self._q("#table", DataTable)
        if table.cursor_row < table.row_count - 1:
            table.move_cursor(row=table.cursor_row + 1)

    def action_mark_all(self) -> None:
        keys = {s.selector for s in self.shown}
        changed = keys if not keys <= self.marked else set(keys)
        if keys <= self.marked:
            self.marked -= keys
        else:
            self.marked |= keys
        self._refresh_rows(changed)

    # -- actions: sessions ----------------------------------------------------
    def action_settings(self) -> None:
        if len(self.screen_stack) != 1:
            return

        def chosen(mode):
            if mode is None:
                return
            preference = settings.Settings(launch_mode=mode)
            try:
                settings.save(preference)
            except (OSError, ValueError) as error:
                self.notify(f"Settings not saved: {error}", severity="error", markup=False, timeout=12)
                return
            self.settings = preference
            self.settings_error = None
            self.notify("Saved: close browser and exec native CLI." if mode == "exec" else
                        "Saved: suspend browser and return when native CLI exits.", markup=False)

        self.push_screen(SettingsScreen(self.settings.launch_mode, str(paths.cli_config_dir() / "settings.json")),
                         chosen)

    def _require_idle(self) -> bool:
        if self.scanning and not self.by_key:
            self.notify("Still scanning…", severity="warning", markup=False)
            return False
        return True

    def action_open(self, fork: bool = False) -> None:
        s = self.current()
        if not s or not self._require_idle():
            return
        self.notify(f"Checking {s.agent} session…", timeout=2, markup=False)
        self.preflight(s, "fork" if fork else "open")

    def action_fork(self) -> None:
        self.action_open(fork=True)

    @work(thread=True, exclusive=True, group="action")
    def preflight(self, session: Session, action: str) -> None:
        try:
            plan = native.plan(session, action)
        except Exception as error:
            self.call_from_thread(self.notify, f"Cannot {action}: {error}", severity="error", markup=False,
                                  timeout=10)
            return
        self.call_from_thread(self._confirm_launch, session, plan)

    def _confirm_launch(self, session: Session, plan: native.LaunchPlan) -> None:
        if not self._alive() or len(self.screen_stack) != 1:
            return
        mode = self.settings.launch_mode  # confirmation describes exactly the mode to be used
        body = Text()
        body.append(one_line(session.display_title, 200) + "\n\n", style="bold")
        body.append("Command  ", style="bold")
        body.append(safe(shlex.join(plan.argv)) + "\n")
        body.append("Directory  ", style="bold")
        body.append(safe(plan.cwd) + "\n\n")
        for warning in plan.warnings:
            body.append(f"⚠ {warning}\n", style="yellow")
        body.append("The browser closes completely and is replaced by the native CLI. "
                    "It will not return when the agent exits." if mode == "exec" else
                    "The browser is suspended while the native CLI runs and returns when it exits.",
                    style="dim")
        body.append("\nChange this preference in Settings (Ctrl+S / F2).", style="dim")
        verb = "Fork" if plan.action == "fork" else "Open"
        self.push_screen(ConfirmScreen(f"{verb} in {session.agent}?", body, verb),
                         lambda ok: self._launch(session, plan, mode) if ok else None)

    def _launch(self, session: Session, plan: native.LaunchPlan, mode: str) -> None:
        if not native.interactive_terminal():
            self.notify("Opening a native CLI needs an interactive terminal. "
                        f"Preview with: ai-bridge sessions open {session.selector} --dry-run",
                        severity="error", markup=False, timeout=15)
            return
        try:
            native.recheck(plan, session)
        except (native.PreflightError, OSError) as error:
            self.notify(str(error), severity="error", markup=False, timeout=10)
            return
        if mode == "exec":
            # App.run() restores terminal modes before returning this request.
            # The caller rechecks once more, then replaces Python, without a child.
            self.exit(NativeHandoff(session, plan))
            return
        try:
            with self.suspend():
                print(f"\n[ai-bridge] {safe(shlex.join(plan.argv))}\n"
                      f"[ai-bridge] in {one_line(plan.cwd)} — exit the agent to return.\n",
                      flush=True)
                code = native.run(plan)
        except SuspendNotSupported:
            self.notify("This terminal cannot be suspended here. Run: ai-bridge sessions open "
                        f"{session.selector}", severity="error", markup=False, timeout=15)
            return
        except native.PreflightError as error:
            self.notify(str(error), severity="error", markup=False, timeout=10)
            return
        self.notify(f"{session.agent} exited with status {code}.", markup=False,
                    severity="information" if code == 0 else "warning")
        self.refresh_catalog()

    def action_rename(self) -> None:
        s = self.current()
        if not s:
            return

        def done(value):
            if value is None:
                return
            alias = value.strip() or None
            try:
                self.catalog.overlays.update([s.key], alias=alias)
            except (OSError, ValueError) as error:
                self.notify(str(error), severity="error", markup=False)
                return
            s.alias = alias
            self._refresh_rows({s.selector})
            self._render_details(s)
            self.notify("Alias cleared." if alias is None else "Renamed (native title unchanged).", markup=False)

        self.push_screen(InputScreen("Rename session (display alias)", s.alias or "",
                                     placeholder=one_line(s.title, 80),
                                     note="Leave empty to clear. The native transcript is not modified."), done)

    def action_archive(self) -> None:
        sessions = self.targets()
        if sessions:
            self._set_archived(sessions, not all(s.archived for s in sessions))

    def _set_archived(self, sessions: list[Session], archived: bool) -> None:
        try:
            self.catalog.overlays.update([s.key for s in sessions], archived=archived)
        except (OSError, ValueError) as error:
            self.notify(str(error), severity="error", markup=False)
            return
        for s in sessions:
            s.archived = archived
        self.marked -= {s.selector for s in sessions}
        verb = "Archived" if archived else "Unarchived"
        hint = "  (A shows archived; a again to undo)" if archived and self.query_state.archived == "hide" else ""
        self.notify(f"{verb} {len(sessions)} session(s); files untouched.{hint}", markup=False)
        self._rebuild_table()

    def action_delete(self) -> None:
        sessions = self.targets()
        if not sessions or not self._require_idle():
            return
        count = len(sessions)
        what = f"{count} session(s)" if count > 1 else f"“{one_line(sessions[0].display_title, 60)}”"
        agents = sorted({s.agent for s in sessions})
        trash_hint = "; ".join(f"{a}: {trash.SUPPORT_NOTES[a]}" for a in agents)

        def chosen(choice):
            if choice == "archive":
                self._set_archived(sessions, True)
            elif choice == "trash":
                self.notify("Preparing preview…", timeout=2, markup=False)
                self.plan_delete(sessions)

        self.push_screen(ChoiceScreen(
            f"Remove {what}?",
            [("archive", "Archive (recommended)", "Hide from listings. Reversible; native files untouched."),
             ("trash", "Move to trash (native delete)…", "Shows a preview first. " + trash_hint),
             ("cancel", "Cancel", "")]), chosen)

    @work(thread=True, exclusive=True, group="action")
    def plan_delete(self, sessions: list[Session]) -> None:
        try:
            plan = trash.build_plan(sessions)
        except Exception as error:
            self.call_from_thread(self.notify, f"Preview failed: {error}", severity="error", markup=False)
            return
        self.call_from_thread(self._confirm_delete, plan)

    def _confirm_delete(self, plan: trash.DeletePlan) -> None:
        data = plan.to_json()
        allowed = len(plan.allowed)
        title = (f"Move {allowed} of {len(plan.items)} session(s) to trash?" if allowed else
                 "Nothing can be deleted")
        if not allowed:
            self.push_screen(ConfirmScreen(title, plan_table(data), "OK"))
            return
        self.push_screen(ConfirmScreen(title, plan_table(data), "Move to trash", danger=True),
                         lambda ok: self.run_delete(plan) if ok else None)

    @work(thread=True, exclusive=True, group="action")
    def run_delete(self, plan: trash.DeletePlan) -> None:
        try:
            result = trash.execute(plan)
        except Exception as error:
            self.call_from_thread(self.notify, f"Nothing deleted: {error}", severity="error", markup=False,
                                  timeout=12)
            return
        moved, failed = len(result["moved"]), result["failed"]
        message = f"Moved {moved} session(s) to trash (batch {result['batch_id']}); restore with t."
        if failed:
            message += f" {len(failed)} failed: " + "; ".join(f"{f['selector']}: {f['error']}" for f in failed)
        self.call_from_thread(self.notify, message, markup=False, timeout=12,
                              severity="warning" if failed else "information")
        self.call_from_thread(self.refresh_catalog)

    def action_trash(self) -> None:
        self.open_trash()

    @work(thread=True, exclusive=True, group="action")
    def open_trash(self) -> None:
        try:
            batches = trash.list_batches()
        except Exception as error:
            self.call_from_thread(self.notify, f"Trash unreadable: {error}", severity="error", markup=False)
            return
        archived = [s for s in self.by_key.values() if s.native_archived]
        self.call_from_thread(lambda: self.push_screen(TrashScreen(batches, archived), self._trash_choice))

    def _trash_choice(self, choice) -> None:
        if not choice:
            return
        action, kind, batch_id, selector, title = choice
        if action == "restore":
            body = Text.assemble((one_line(title, 200), "bold"), "\n", selector or "", "\n\n",
                                 ("Restores the original files/archive state. Nothing is overwritten.", "dim"))
            self.push_screen(ConfirmScreen("Restore session?", body, "Restore"),
                             lambda ok: self.run_restore(kind, batch_id, selector) if ok else None)
        else:
            body = Text(f"Permanently remove trash batch {batch_id}?\n\nThis cannot be undone. Codex items in "
                        "this batch stay archived by Codex.", style="")
            self.push_screen(ConfirmScreen("Purge batch", body, "Purge permanently", danger=True),
                             lambda ok: self.run_purge(batch_id) if ok else None)

    @work(thread=True, exclusive=True, group="action")
    def run_restore(self, kind: str, batch_id: str | None, selector: str) -> None:
        try:
            if kind == "codex":
                session = self.by_key[selector]
                trash.restore_native_codex(session)
                failed = []
            else:
                failed = trash.restore(batch_id, [selector])["failed"]
        except Exception as error:
            failed = [{"selector": selector, "error": str(error)}]
        if failed:
            self.call_from_thread(self.notify, f"Restore failed: {failed[0]['error']}", severity="error",
                                  markup=False, timeout=12)
        else:
            self.call_from_thread(self.notify, f"Restored {selector}.", markup=False)
        self.call_from_thread(self.refresh_catalog)

    @work(thread=True, exclusive=True, group="action")
    def run_purge(self, batch_id: str) -> None:
        try:
            trash.purge(batch_id)
            self.call_from_thread(self.notify, f"Purged {batch_id}.", markup=False)
        except Exception as error:
            self.call_from_thread(self.notify, f"Purge failed: {error}", severity="error", markup=False)


def run() -> int:
    if not native.interactive_terminal() or os.environ.get("TERM") == "dumb":
        print("ai-bridge: the session browser needs an interactive, capable terminal; "
              "use `ai-bridge sessions list`.", file=sys.stderr)
        return 2
    result = SessionBrowser().run()
    if isinstance(result, NativeHandoff):
        try:
            native.recheck(result.plan, result.session)
            native.exec_(result.plan)
        except (native.PreflightError, OSError) as error:
            print(f"ai-bridge: native launch failed: {safe(error)}. "
                  "Run ai-bridge to reopen the browser.", file=sys.stderr)
            return 1
    return 0
