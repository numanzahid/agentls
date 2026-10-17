import asyncio
import contextlib
import json
import os

import fixtures
import pytest
from conftest import CLAUDE_ID, CODEX_ID, CURSOR_ID

from ai_bridge_cli import native, settings
from ai_bridge_cli.tui import screens
from ai_bridge_cli.tui.app import SessionBrowser


def browser():
    return SessionBrowser(periodic_refresh=False, probe_activity=False)


async def settle(pilot, app):
    for _ in range(5):
        await pilot.pause()
        await app.workers.wait_for_complete()
    await pilot.pause()


def drive(scenario, size=(150, 40)):
    async def runner():
        app = browser()
        async with app.run_test(size=size) as pilot:
            await settle(pilot, app)
            await scenario(app, pilot)
    asyncio.run(runner())


def keys(app):
    return [s.selector for s in app.shown]


def test_lists_all_sessions_with_details(populated):
    async def scenario(app, pilot):
        assert len(app.shown) == 4 and app.query_one("#table").row_count == 4
        assert app.current() is not None
        status = str(app.query_one("#statusbar").render())
        assert "4/4 sessions" in status
        assert app.query_one("#side").display  # wide: details beside the table
    drive(scenario)


def test_search_and_filters(populated):
    async def scenario(app, pilot):
        await pilot.press("slash")
        for ch in "login":
            await pilot.press(ch)
        await pilot.pause()
        assert keys(app) == [f"claude:{CLAUDE_ID}"]
        await pilot.press("escape")  # clears search, back to the table
        await pilot.pause()
        assert len(app.shown) == 4 and app.focused.id == "table"
        app.query_one("#agent").value = "codex"
        await pilot.pause()
        assert {s.agent for s in app.shown} == {"codex"}
        app.query_one("#agent").value = "all"
        app.query_one("#origin").value = "mcp"
        await pilot.pause()
        assert app.shown == [] and app.query_one("#empty").display
        assert "No sessions match" in str(app.query_one("#empty").render())
    drive(scenario)


def test_sort_cycle(populated):
    async def scenario(app, pilot):
        await pilot.press("s")
        assert app.query_state.sort == "title"
        titles = [s.display_title.casefold() for s in app.shown]
        assert titles == sorted(titles)
        await pilot.press("S")
        assert app.query_state.reverse
    drive(scenario)


def test_mark_and_archive_many_then_show_archived(populated):
    async def scenario(app, pilot):
        await pilot.press("space", "space")
        assert len(app.marked) == 2
        await pilot.press("a")
        await pilot.pause()
        assert len(app.shown) == 2 and not app.marked
        data = json.loads((populated.cli_state / "overlays.json").read_text())
        assert sum(1 for r in data["sessions"].values() if r.get("archived")) == 2
        await pilot.press("A")
        await pilot.pause()
        assert len(app.shown) == 4
    drive(scenario)


def test_mark_all_toggles(populated):
    async def scenario(app, pilot):
        await pilot.press("asterisk")
        assert len(app.marked) == 4
        await pilot.press("asterisk")
        assert not app.marked
    drive(scenario)


def test_rename_sets_alias(populated):
    async def scenario(app, pilot):
        target = app.current()
        await pilot.press("n")
        await pilot.pause()
        assert isinstance(app.screen, screens.InputScreen)
        app.screen.query_one("Input").value = "Renamed here"
        await pilot.press("enter")
        await pilot.pause()
        assert app.by_key[target.selector].alias == "Renamed here"
    drive(scenario)


def test_delete_offers_archive_by_default(populated):
    async def scenario(app, pilot):
        await pilot.press("d")
        await pilot.pause()
        assert isinstance(app.screen, screens.ChoiceScreen)
        await pilot.press("enter")  # first option: archive
        await pilot.pause()
        assert len(app.shown) == 3
    drive(scenario)


def test_trash_and_restore_flow(populated):
    async def scenario(app, pilot):
        claude = app.by_key[f"claude:{CLAUDE_ID}"]
        app.query_one("#table").move_cursor(row=keys(app).index(claude.selector))
        await pilot.press("d")
        await pilot.pause()
        await pilot.press("down", "enter")  # "Move to trash…"
        await settle(pilot, app)
        assert isinstance(app.screen, screens.ConfirmScreen) and app.screen.danger
        assert app.focused.id == "cancel"  # destructive dialogs default to Cancel
        await pilot.click("#confirm")
        await settle(pilot, app)
        assert not os.path.exists(claude.storage_path)
        assert f"claude:{CLAUDE_ID}" not in app.by_key
        await pilot.press("t")
        await settle(pilot, app)
        assert isinstance(app.screen, screens.TrashScreen)
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, screens.ConfirmScreen)
        await pilot.click("#confirm")
        await settle(pilot, app)
        assert os.path.exists(claude.storage_path) and f"claude:{CLAUDE_ID}" in app.by_key
    drive(scenario)


def test_open_suspends_and_runs_native_cli(populated, monkeypatch):
    settings.save(settings.Settings(launch_mode="suspend"))
    ran = []
    monkeypatch.setattr(native, "interactive_terminal", lambda: True)
    monkeypatch.setattr(native, "run", lambda plan: ran.append(plan.argv) or 0)
    monkeypatch.setattr(SessionBrowser, "suspend", lambda self: contextlib.nullcontext())

    async def scenario(app, pilot):
        app.query_one("#table").move_cursor(row=keys(app).index(f"claude:{CLAUDE_ID}"))
        await pilot.press("o")
        await settle(pilot, app)
        assert isinstance(app.screen, screens.ConfirmScreen)
        await pilot.press("enter")
        await settle(pilot, app)
        assert ran and ran[0][1:] == ["--resume", CLAUDE_ID]
    drive(scenario)


def test_open_refusal_is_reported_not_launched(populated, monkeypatch):
    ran = []
    monkeypatch.setattr(native, "run", lambda plan: ran.append(plan) or 0)

    async def scenario(app, pilot):
        app.query_one("#table").move_cursor(row=keys(app).index(f"cursor:{CURSOR_ID}"))
        await pilot.press("o")
        await settle(pilot, app)
        assert not isinstance(app.screen, screens.ConfirmScreen) and not ran
    drive(scenario)


def test_narrow_terminal_layout(populated):
    async def scenario(app, pilot):
        assert app.tier == "narrow" and not app.query_one("#side").display
        assert [c.value for c in app.query_one("#table").columns] == ["mark", "agent", "title", "model", "updated"]
        await pilot.press("p")
        await settle(pilot, app)
        assert app.query_one("#side").display and not app.query_one("#table").display
        await pilot.press("escape")
        assert app.query_one("#table").display
    drive(scenario, size=(60, 20))


def test_help_screen(populated):
    async def scenario(app, pilot):
        await pilot.press("question_mark")
        await pilot.pause()
        assert isinstance(app.screen, screens.HelpScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, screens.HelpScreen)
    drive(scenario)


def test_empty_stores_message(env):
    async def scenario(app, pilot):
        assert app.shown == []
        assert "No sessions found" in str(app.query_one("#empty").render())
    drive(scenario)


def test_markup_in_titles_is_not_interpreted(env):
    fixtures.claude_session(env.claude, CLAUDE_ID, env.project, "[bold red]boom[/] \x1b[31m")
    env.age()

    async def scenario(app, pilot):
        cell = app.query_one("#table").get_cell(f"claude:{CLAUDE_ID}", "title")
        assert "[bold red]boom[/]" in cell.plain and "\x1b" not in cell.plain
    drive(scenario)


def test_codex_trash_uses_native_archive(populated):
    async def scenario(app, pilot):
        app.query_one("#table").move_cursor(row=keys(app).index(f"codex:{CODEX_ID}"))
        await pilot.press("d")
        await pilot.pause()
        await pilot.press("down", "enter")
        await settle(pilot, app)
        await pilot.click("#confirm")
        await settle(pilot, app)
        assert ["archive", CODEX_ID] in populated.calls()
    drive(scenario)


def test_background_updates_while_a_modal_is_open(populated):
    """Refresh/preview callbacks must target the main screen, not the active modal."""
    async def scenario(app, pilot):
        await pilot.press("question_mark")
        await pilot.pause()
        app._preview_cache.clear()
        app.refresh_catalog()
        app._schedule_preview()
        await pilot.pause(0.3)
        await settle(pilot, app)
        assert isinstance(app.screen, screens.HelpScreen)
        assert app._q("#table").row_count == 4
        await pilot.press("escape")
        await pilot.press("down")
        await settle(pilot, app)
    drive(scenario)


def test_quit_with_pending_preview_is_clean(populated):
    async def scenario(app, pilot):
        await pilot.press("down", "down")  # schedules debounced preview loads
        await app.action_quit()
    drive(scenario)


@pytest.mark.parametrize("width", [40, 60, 80, 90, 100, 129, 130, 150, 200])
def test_model_and_title_fit_after_resize(populated, width):
    async def scenario(app, pilot):
        # Also exercise resizing within a tier and with the side pane hidden.
        await pilot.resize_terminal(width, 30)
        await settle(pilot, app)
        table = app.query_one("#table")
        columns = {key.value: col for key, col in table.columns.items()}
        assert columns["title"].width >= 10
        assert str(columns["model"].label) == "Last Model"
        assert table.virtual_size.width <= table.size.width
        assert table.get_cell(f"claude:{CLAUDE_ID}", "model").plain == "claude-test-1"
        assert table.get_cell(f"codex:{CODEX_ID}", "model").plain == "gpt-test"
        assert table.get_cell(f"cursor:{CURSOR_ID}", "model").plain == "—"
        if app.tier == "wide":
            await pilot.press("p")
            await settle(pilot, app)
            assert table.virtual_size.width <= table.size.width
    drive(scenario)


@pytest.mark.parametrize("model", ["claude-" + "long-model-name-" * 12, None])
def test_details_show_full_model_and_unknown(env, model):
    fixtures.claude_session(env.claude, CLAUDE_ID, env.project, model=model)

    async def scenario(app, pilot):
        from rich.console import Console
        from io import StringIO

        out = StringIO()
        console = Console(file=out, width=60)
        console.print(app.query_one("#details").content)
        assert "Last Model" in out.getvalue()
        grid = app.query_one("#details").content
        labels = grid.columns[0]._cells
        values = grid.columns[1]._cells
        assert values[labels.index("Last Model")].plain == (model or "—")
        assert app.query_one("#table").get_cell(f"claude:{CLAUDE_ID}", "model").plain == (model or "—")
    drive(scenario)


@pytest.mark.parametrize("shortcut", ["ctrl+s", "f2"])
def test_settings_keyboard_saves_and_reloads(populated, shortcut):
    async def scenario(app, pilot):
        assert app.settings.launch_mode == "exec"
        await pilot.press(shortcut)
        await pilot.pause()
        assert isinstance(app.screen, screens.SettingsScreen)
        assert app.screen.query_one("OptionList").highlighted == 0
        await pilot.press("down", "enter")
        await pilot.pause()
        assert app.settings.launch_mode == settings.load().launch_mode == "suspend"
        assert browser().settings.launch_mode == "suspend"
        await pilot.press(shortcut)
        await pilot.pause()
        assert app.screen.query_one("OptionList").highlighted == 1
        await pilot.press("up", "escape")
        await pilot.pause()
        assert app.settings.launch_mode == "suspend"  # Escape does not save
        await pilot.press(shortcut)
        await pilot.pause()
        await pilot.press("up", "enter")
        await pilot.pause()
        assert browser().settings.launch_mode == "exec"
    drive(scenario)


def test_settings_shortcut_in_search_does_not_type_or_leak(populated):
    async def scenario(app, pilot):
        await pilot.press("slash")
        await pilot.press("f2")
        await pilot.pause()
        assert isinstance(app.screen, screens.SettingsScreen)
        await pilot.press("q", "o", "d", "ctrl+s")
        assert isinstance(app.screen, screens.SettingsScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert app.focused.id == "search" and app._q("#search").value == ""
    drive(scenario, size=(60, 20))


@pytest.mark.parametrize("confirm", [False, True])
def test_default_open_exits_only_after_confirmation(populated, monkeypatch, confirm):
    from ai_bridge_cli.tui.app import NativeHandoff
    monkeypatch.setattr(native, "interactive_terminal", lambda: True)
    calls = []
    monkeypatch.setattr(native, "run", lambda plan: calls.append("child"))
    monkeypatch.setattr(native, "exec_", lambda plan: calls.append("exec"))

    async def scenario(app, pilot):
        app._q("#table").move_cursor(row=keys(app).index(f"claude:{CLAUDE_ID}"))
        await pilot.press("enter")
        await settle(pilot, app)
        assert isinstance(app.screen, screens.ConfirmScreen)
        assert "closes completely" in app.screen.body.plain
        assert str(populated.project) in app.screen.body.plain
        assert not calls and app.return_value is None
        await pilot.press("enter" if confirm else "escape")
        await pilot.pause()
        if confirm:
            assert isinstance(app.return_value, NativeHandoff)
            assert app.return_value.plan.cwd == str(populated.project)
            assert app.return_value.session.session_id == CLAUDE_ID
        else:
            assert app.return_value is None
            assert app.is_running
        assert not calls  # exec belongs after Textual has restored the terminal
    drive(scenario)


@pytest.mark.parametrize("failure", ["active", "non-tty", "missing-cwd", "missing-cli", "os-error"])
def test_open_recheck_refusal_keeps_browser(populated, monkeypatch, failure):
    from ai_bridge_cli.activity import Activity
    monkeypatch.setattr(native, "interactive_terminal", lambda: failure != "non-tty")

    async def scenario(app, pilot):
        app._q("#table").move_cursor(row=keys(app).index(f"claude:{CLAUDE_ID}"))
        await pilot.press("o")
        await settle(pilot, app)
        assert isinstance(app.screen, screens.ConfirmScreen)
        if failure == "active":
            monkeypatch.setattr(native.activity_mod, "snapshot", lambda: Activity(cmdline_ids={CLAUDE_ID: 42}))
        elif failure == "missing-cwd":
            populated.project.rmdir()
        elif failure == "missing-cli":
            from pathlib import Path
            Path(native.find_binary("claude")).unlink()  # temporary fake executable
        elif failure == "os-error":
            def fail(*args):
                raise PermissionError("session storage no longer readable")
            monkeypatch.setattr(native, "recheck", fail)
        await pilot.press("enter")
        await settle(pilot, app)
        assert app.is_running and app.return_value is None
    drive(scenario)


def test_settings_failure_keeps_current_mode(populated, monkeypatch):
    def fail(preference):
        raise ValueError("unsafe config")
    monkeypatch.setattr(settings, "save", fail)

    async def scenario(app, pilot):
        await pilot.press("f2")
        await pilot.pause()
        await pilot.press("down", "enter")
        await pilot.pause()
        assert app.settings.launch_mode == "exec"
    drive(scenario)


def test_wrapper_executes_only_after_tui_returns(populated, monkeypatch):
    from ai_bridge_cli.catalog import Catalog
    from ai_bridge_cli.tui import app as module
    session = Catalog().refresh(probe_activity=False).get("claude", CLAUDE_ID)
    plan = native.plan(session)
    order = []
    monkeypatch.setattr(native, "interactive_terminal", lambda: True)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setattr(SessionBrowser, "run", lambda self: order.append("terminal restored") or
                        module.NativeHandoff(session, plan))
    monkeypatch.setattr(native, "recheck", lambda p, s: order.append("recheck"))
    monkeypatch.setattr(native, "exec_", lambda p: order.append(("exec", p.cwd)))
    assert module.run() == 0
    assert order == ["terminal restored", "recheck", ("exec", str(populated.project))]


@pytest.mark.parametrize("stage", ["recheck", "exec_"])
def test_wrapper_launch_failure_reported_cleanly(populated, monkeypatch, capsys, stage):
    from ai_bridge_cli.catalog import Catalog
    from ai_bridge_cli.tui import app as module
    session = Catalog().refresh(probe_activity=False).get("claude", CLAUDE_ID)
    plan = native.plan(session)
    monkeypatch.setattr(native, "interactive_terminal", lambda: True)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setattr(SessionBrowser, "run", lambda self: module.NativeHandoff(session, plan))
    calls = []
    monkeypatch.setattr(native, "exec_", lambda p: calls.append(p))

    def fail(*args):
        raise native.PreflightError("launch refused")
    monkeypatch.setattr(native, stage, fail)
    assert module.run() == 1
    assert "launch refused" in capsys.readouterr().err
    assert not calls


def test_wrapper_non_tty_is_graceful(monkeypatch, capsys):
    from ai_bridge_cli.tui import app as module
    monkeypatch.setattr(native, "interactive_terminal", lambda: False)
    assert module.run() == 2
    assert "interactive" in capsys.readouterr().err


def test_suspend_unsupported_keeps_browser(populated, monkeypatch):
    from textual.app import SuspendNotSupported
    settings.save(settings.Settings(launch_mode="suspend"))
    monkeypatch.setattr(native, "interactive_terminal", lambda: True)
    calls = []
    monkeypatch.setattr(native, "run", lambda p: calls.append(p))

    def unsupported(self):
        raise SuspendNotSupported()
    monkeypatch.setattr(SessionBrowser, "suspend", unsupported)

    async def scenario(app, pilot):
        app._q("#table").move_cursor(row=keys(app).index(f"claude:{CLAUDE_ID}"))
        await pilot.press("o")
        await settle(pilot, app)
        assert "suspended" in app.screen.body.plain
        await pilot.press("enter")
        await settle(pilot, app)
        assert app.is_running and app.return_value is None and not calls
    drive(scenario)


def test_incomplete_delete_preview_gives_safe_alternatives(populated):
    from ai_bridge_cli import activity, trash
    from ai_bridge_cli.catalog import Catalog
    from rich.console import Console
    from io import StringIO
    session = Catalog().refresh(probe_activity=False).get("claude", CLAUDE_ID)
    plan = trash.build_plan([session], act=activity.Activity(uninspectable=[(42, "codex")]))
    out = StringIO()
    Console(file=out, width=100).print(screens.plan_table(plan.to_json()))
    text = out.getvalue()
    assert "does not prove" in text and "Archive (a)" in text and "never bypasses" in text
    assert not plan.allowed
