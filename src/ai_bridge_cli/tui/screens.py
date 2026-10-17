"""Modal screens for the session browser. All user data is rendered as plain Text."""
from __future__ import annotations

from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Label, OptionList, Static
from textual.widgets.option_list import Option

from ..output import relative
from ..textsafe import one_line, safe


class Dialog(ModalScreen):
    """Base: Escape cancels (dismisses with None)."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]
    DEFAULT_CLASSES = "dialog"

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmScreen(Dialog):
    def __init__(self, title: str, body: Text | Table | str, confirm: str | Text = "Confirm", danger: bool = False):
        super().__init__()
        self.title_text, self.body, self.confirm, self.danger = title, body, confirm, danger

    def compose(self) -> ComposeResult:
        with Vertical(classes="box" + (" danger" if self.danger else "")):
            yield Label(Text(self.title_text), classes="box-title")
            with VerticalScroll(classes="box-body"):
                yield Static(self.body if not isinstance(self.body, str) else Text(self.body))
            with Horizontal(classes="buttons"):
                yield Button("Cancel", id="cancel")
                yield Button(self.confirm, id="confirm", variant="error" if self.danger else "primary")

    def on_mount(self) -> None:
        # Destructive dialogs start on Cancel; Enter must be a deliberate choice.
        self.query_one("#cancel" if self.danger else "#confirm", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")


class RemoveScreen(Dialog):
    """Final confirmation for ordinary archive or safe native deletion."""

    BINDINGS = [Binding("a", "choose('archive')", "Archive", priority=True),
                Binding("d", "choose('trash')", "Delete", priority=True),
                Binding("c", "cancel", "Cancel", priority=True)]

    def __init__(self, title: str):
        super().__init__()
        self.title_text = title

    def compose(self) -> ComposeResult:
        with Vertical(classes="box removal"):
            yield Label(Text(self.title_text), classes="box-title")
            yield Static(Text("Archive hides chats; Delete removes them from the native list. Both are restorable."))
            with Horizontal(classes="buttons"):
                yield Button(Text("a Archive"), id="archive")
                yield Button(Text("d Delete"), id="trash")
                yield Button(Text("c Cancel"), id="cancel")

    def on_mount(self) -> None:
        self.query_one("#cancel", Button).focus()

    def action_choose(self, choice: str) -> None:
        self.dismiss(choice)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None if event.button.id == "cancel" else event.button.id)


class DeleteConfirmScreen(ConfirmScreen):
    """Shortcuts are modal-local and work even when a button has focus."""

    BINDINGS = [Binding("y", "confirm_delete", "Confirm", priority=True),
                Binding("n", "cancel", "Cancel", priority=True)]

    def __init__(self, title: str, body: Table | Text, allowed: bool):
        super().__init__(title, body, Text("[y] Confirm"), danger=True)
        self.allowed = allowed

    def on_mount(self) -> None:
        super().on_mount()
        self.query_one(".box").add_class("native-delete")
        self.query_one("#cancel", Button).label = Text("[n] Cancel")
        self.query_one("#confirm", Button).disabled = not self.allowed

    def action_confirm_delete(self) -> None:
        if self.allowed:
            self.dismiss(True)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.prevent_default()  # Textual also dispatches handlers on base classes.
        event.stop()
        self.dismiss(self.allowed and event.button.id == "confirm")


class InputScreen(Dialog):
    def __init__(self, title: str, value: str = "", placeholder: str = "", note: str = ""):
        super().__init__()
        self.title_text, self.value, self.placeholder, self.note = title, value, placeholder, note

    def compose(self) -> ComposeResult:
        with Vertical(classes="box"):
            yield Label(Text(self.title_text), classes="box-title")
            if self.note:
                yield Static(Text(self.note, style="dim"))
            yield Input(value=self.value, placeholder=self.placeholder, id="value")
            with Horizontal(classes="buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Save", id="save", variant="primary")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(self.query_one(Input).value if event.button.id == "save" else None)


class ChoiceScreen(Dialog):
    """Pick one of several labelled options (first option is the default)."""

    def __init__(self, title: str, choices: list[tuple[str, str, str]], note: str = ""):
        super().__init__()
        self.title_text, self.choices, self.note = title, choices, note

    def compose(self) -> ComposeResult:
        with Vertical(classes="box"):
            yield Label(Text(self.title_text), classes="box-title")
            if self.note:
                yield Static(Text(self.note, style="dim"))
            yield OptionList(*[Option(Text.assemble((label, "bold"), "\n  ", (hint, "dim")), id=value)
                               for value, label, hint in self.choices], id="choices")
            yield Static(Text("Enter choose · Esc cancel", style="dim"))

    def on_mount(self) -> None:
        self.query_one(OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)


class SettingsScreen(ChoiceScreen):
    def __init__(self, mode: str, config_path: str):
        super().__init__("5 Settings · native Open / Fork behavior", [
            ("exec", "A · Close browser and exec native CLI (default)",
             "Same process, in the chat's project directory. Browser does not return."),
            ("suspend", "B · Suspend browser and return afterwards",
             "Native CLI runs as a child; browser returns when it exits."),
        ], note=f"Current: {'A · exec' if mode == 'exec' else 'B · suspend'}\n"
                f"Enter saves for this user · Esc cancels\n{safe(config_path)}")
        self.mode = mode

    def on_mount(self) -> None:
        super().on_mount()
        self.query_one(OptionList).highlighted = 0 if self.mode == "exec" else 1


def plan_table(plan: dict) -> Table:
    grid = Table.grid(padding=(0, 1))
    grid.add_column(no_wrap=True)
    grid.add_column(overflow="fold")
    for item in plan["items"]:
        mark = Text("✔ ", style="green bold") if item["allowed"] else Text("✘ ", style="red bold")
        grid.add_row(mark, Text.assemble((f"{item['agent']} ", "bold"), one_line(item["title"], 80)))
        grid.add_row("", Text(f"{item['agent']}:{item['session_id']}", style="dim"))
        method = "Native Codex archive (restorable)" if item["method"] == "codex-archive" else "Move to trash (restorable)"
        grid.add_row("", Text(method, style="italic"))
        for entry in item["entries"]:
            grid.add_row("", Text(f"{entry['kind']}: {safe(entry['path'])}", style="dim"))
        for blocker in item["blockers"]:
            grid.add_row("", Text(f"blocked: {safe(blocker)}", style="red"))
        grid.add_row("", "")
    if any("Running processes cannot be fully inspected" in blocker
           for item in plan["items"] for blocker in item["blockers"]):
        grid.add_row("", Text("Incomplete check does not prove activity. Close listed agents, wait 120s, refresh. "
                              "Archive (a) keeps files; the browser never bypasses safety checks.",
                              style="yellow"))
    return grid


class HelpScreen(Dialog):
    BINDINGS = [Binding("escape,q,question_mark", "cancel", "Close")]

    KEYS = [
        ("Navigation", [("↑/↓ PgUp/PgDn Home/End", "move"), ("/", "search (Esc returns to the list)"),
                        ("1 / 2 / 3 / 4 / 5", "search / filters / session list / preview & details / settings (outside text entry)"),
                        ("Tab / Shift+Tab", "move between search, filters and list"),
                        ("s / S", "cycle sort key / reverse order (or click a column header)"),
                        ("p", "toggle details & preview pane"), ("r / F5", "refresh")]),
        ("Selection", [("Space", "mark / unmark row"), ("*", "mark all visible (again: clear)"),
                       ("Esc", "clear marks (in the list)")]),
        ("Actions", [("Enter / o", "open (resume) in the native CLI"), ("f", "fork (codex, claude)"),
                     ("n", "rename (display alias only)"), ("a", "archive / unarchive (hide; files untouched)"),
                     ("d / Del", "archive or delete (preflight checked)"),
                     ("t", "trash: restore or purge"), ("y", "copy agent:ID"), ("A", "show/hide archived"),
                     ("Ctrl+S / F2", "settings: close/exec native (default) or suspend and return"),
                     ("q", "quit")]),
    ]

    def compose(self) -> ComposeResult:
        table = Table.grid(padding=(0, 2))
        table.add_column(style="bold cyan", no_wrap=True)
        table.add_column()
        for section, rows in self.KEYS:
            table.add_row(Text(section, style="bold underline"), "")
            for key, text in rows:
                table.add_row(key, text)
            table.add_row("", "")
        legend = Text.assemble(
            ("Origin  ", "bold"), ("MCP", "magenta bold"), " verified bridge provenance record · ",
            ("Direct", "bold"), " no record · ", ("Unknown", "dim"), " cannot verify\n",
            ("Status  ", "bold"), ("active", "green"), " in use · ", ("uncertain", "yellow"),
            " bridge job unverifiable · ", ("recent", "cyan"), " modified <2 min · idle\n",
            ("Last Model  ", "bold"), "last known model recorded in this chat; — = unknown. "
            "Full value in details (p); configured defaults are never substituted.")
        with Vertical(classes="box wide"):
            yield Label("Keyboard help", classes="box-title")
            with VerticalScroll(classes="box-body"):
                yield Static(table)
                yield Static(legend)
            yield Static(Text("Esc / ? close", style="dim"))


class TrashScreen(Dialog):
    """Restore or purge items moved to the trash, and natively archived Codex sessions."""

    BINDINGS = [Binding("escape,q", "cancel", "Close"), Binding("enter,r", "restore", "Restore"),
                Binding("P", "purge", "Purge batch")]

    def __init__(self, batches: list[dict], codex_archived: list):
        super().__init__()
        self.batches, self.codex_archived = batches, codex_archived
        self.rows: dict[str, tuple] = {}
        self.changed = False

    def compose(self) -> ComposeResult:
        with Vertical(classes="box wide"):
            yield Label("Trash", classes="box-title")
            yield DataTable(id="trash-table", cursor_type="row", zebra_stripes=True)
            yield Static(Text("Enter restore · P purge batch (permanent) · Esc close", style="dim"), id="trash-help")

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns("Batch", "Status", "Agent", "Session", "Title")
        for batch in self.batches:
            if batch.get("error"):
                key = f"err:{batch['batch_id']}"
                self.rows[key] = ("error", batch, None)
                table.add_row(batch["batch_id"], Text("unreadable", style="red"), "", "",
                              Text(one_line(batch["error"], 80)), key=key)
                continue
            for item in batch.get("items", []):
                key = f"{batch['batch_id']}/{item['agent']}:{item['session_id']}"
                self.rows[key] = ("batch", batch, item)
                status = item.get("status")
                table.add_row(Text(f"{batch['batch_id']}\n{relative(batch.get('created'))}"),
                              Text(status or "?", style={"trashed": "cyan", "restored": "green",
                                                         "failed": "red", "partial": "red"}.get(status, "")),
                              item["agent"], item["session_id"][:8], Text(one_line(item.get("title"), 60)),
                              key=key, height=2)
        for session in self.codex_archived:
            key = f"codex-native/{session.selector}"
            self.rows[key] = ("codex", None, session)
            table.add_row(Text("codex archive\n(native)"), Text("archived", style="cyan"), "codex",
                          session.session_id[:8], Text(one_line(session.display_title, 60)), key=key, height=2)
        if not self.rows:
            self.query_one("#trash-help", Static).update(Text("Trash is empty. Esc close", style="dim"))
        table.focus()

    def _current(self):
        table = self.query_one(DataTable)
        if not self.rows or table.cursor_row is None:
            return None
        try:
            key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        except Exception:
            return None
        return self.rows.get(key)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()  # the table consumes Enter before screen bindings
        self.action_restore()

    def action_restore(self) -> None:
        current = self._current()
        if not current or current[0] == "error":
            return
        kind, batch, item = current
        if kind == "batch" and item.get("status") != "trashed":
            self.app.notify("Only trashed items can be restored.", severity="warning", markup=False)
            return
        title = item.display_title if kind == "codex" else item.get("title")
        self.dismiss(("restore", kind, batch["batch_id"] if batch else None,
                      item.selector if kind == "codex" else f"{item['agent']}:{item['session_id']}", title))

    def action_purge(self) -> None:
        current = self._current()
        if not current or current[0] != "batch":
            self.app.notify("Select an item of a trash batch to purge that batch.", severity="warning",
                            markup=False)
            return
        self.dismiss(("purge", "batch", current[1]["batch_id"], None, None))
