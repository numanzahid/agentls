import asyncio
import contextlib
import json
import os

import fixtures
import pytest
from conftest import CLAUDE_ID, CODEX_CHILD, CODEX_ID, CURSOR_ID

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
        # The Codex child is an exec chat: hidden by the default Interactive mode filter.
        assert len(app.shown) == 3 and app.query_one("#table").row_count == 3
        assert f"codex:{CODEX_CHILD}" not in keys(app)
        assert app.current() is not None
        status = app.query_one("#table")._border_subtitle
        assert status.plain == "1 non-interactive · 1 of 3"
        assert "dim" in str(status.spans[0].style)  # hidden: dim; shown: bold
        assert app.query_one("#side").display  # wide: details beside the table
    drive(scenario)


def test_search_and_filters(populated):
    async def scenario(app, pilot):
        await pilot.press("slash")
        for ch in "login":
            await pilot.press(ch)
        await pilot.pause()
        assert keys(app) == [f"claude:{CLAUDE_ID}"]
        await pilot.press("escape")
        assert app.focused.id == "table"
        assert app.query_one("#search").value == "login"
        assert keys(app) == [f"claude:{CLAUDE_ID}"]
        await pilot.press("slash", "ctrl+u", "escape")
        await pilot.pause()
        assert len(app.shown) == 3 and app.focused.id == "table"
        app.query_one("#agent").value = "codex"
        await pilot.pause()
        assert {s.agent for s in app.shown} == {"codex"}
        app.query_one("#agent").value = "all"
        app.query_one("#origin").value = "mcp"
        await pilot.pause()
        assert app.shown == [] and app.query_one("#empty").display
        assert "No sessions match" in str(app.query_one("#empty").render())
    drive(scenario)


def test_mode_filter_marks_and_toggles_noninteractive_chats(populated):
    child = f"codex:{CODEX_CHILD}"

    async def scenario(app, pilot):
        assert child not in keys(app)
        app.query_one("#mode").value = "include"
        await pilot.pause()
        assert child in keys(app) and len(app.shown) == 4
        title = app.query_one("#table").get_cell(child, "title").plain
        assert title.startswith("⚙ ")
        assert not app.query_one("#table").get_cell(f"claude:{CLAUDE_ID}", "title").plain.startswith("⚙")
        app.query_one("#mode").value = "only"
        await pilot.pause()
        assert keys(app) == [child]
        app.query_one("#mode").value = "hide"
        await pilot.pause()
        assert child not in keys(app)
    drive(scenario)


@pytest.mark.parametrize('width', [40, 150])
@pytest.mark.parametrize('query', ['login', 'no-such-session', ''])
def test_search_escape_preserves_query_and_rows_immediately(populated, width, query):
    async def scenario(app, pilot):
        await pilot.press('slash', *query)
        expected = keys(app)
        table = app._q('#table')
        if query == 'login':
            assert expected == [f'claude:{CLAUDE_ID}']
        elif query:
            assert expected == []
        else:
            assert len(expected) == 3
        for _ in range(2):
            await pilot.press('escape')
            assert app.focused is table and table.display
            assert app._q('#search').value == app.query_state.search == query
            assert keys(app) == expected
            assert [key.value for key in table.rows] == expected
    drive(scenario, size=(width, 24))


@pytest.mark.parametrize('clear_keys', [('ctrl+u',), ('ctrl+shift+a', 'backspace')])
def test_explicit_search_clear_from_middle_of_query(populated, clear_keys):
    async def scenario(app, pilot):
        await pilot.press('slash', *'login', 'left', 'left', *clear_keys)
        assert app.focused.id == 'search'
        assert app._q('#search').value == app.query_state.search == ''
        assert len(app.shown) == app._q('#table').row_count == 3
    drive(scenario)


def test_three_types_in_search_then_navigates_outside_input(populated):
    async def scenario(app, pilot):
        # A numbered pytest project path may itself contain "3", matching every row.
        for session in app.catalog.sessions:
            session.cwd = '/fixture/project'
        await pilot.press('slash', '3')
        assert app.focused.id == 'search'
        assert app._q('#search').value == app.query_state.search == '3'
        expected = keys(app)
        assert expected and len(expected) < 3
        await pilot.press('escape', '4', '3')
        assert app.focused.id == 'table'
        assert app._q('#search').value == '3' and keys(app) == expected
        assert [key.value for key in app._q('#table').rows] == expected
    drive(scenario)


@pytest.mark.parametrize('width', [40, 150])
def test_escape_other_panes_keeps_search(populated, width):
    async def scenario(app, pilot):
        await pilot.press('slash', *'login', 'escape', 'space')
        marked = set(app.marked)
        assert marked
        for pane in ('2', '4'):
            await pilot.press(pane)
            if pane == '2':
                await pilot.press('enter', 'escape')
                assert app.focused.id == 'agent' and not app._q('#agent').expanded
            await pilot.press('escape')
            assert app.focused.id == 'table' and app._q('#table').display
            assert app.marked == marked
        for modal in ('question_mark', '5'):
            await pilot.press(modal)
            assert len(app.screen_stack) == 2
            await pilot.press('escape')
            assert len(app.screen_stack) == 1 and app.focused.id == 'table'
        await pilot.press('escape', 'escape', 'ctrl+u')
        assert not app.marked
        assert app._q('#search').value == app.query_state.search == 'login'
        assert keys(app) == [f'claude:{CLAUDE_ID}']
        assert [key.value for key in app._q('#table').rows] == keys(app)
    drive(scenario, size=(width, 24))


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
        assert len(app.shown) == 1 and not app.marked
        data = json.loads((populated.cli_state / "overlays.json").read_text())
        assert sum(1 for r in data["sessions"].values() if r.get("archived")) == 2
        await pilot.press("A")
        await pilot.pause()
        assert len(app.shown) == 3
    drive(scenario)


def test_mark_all_toggles(populated):
    async def scenario(app, pilot):
        await pilot.press("asterisk")
        assert len(app.marked) == 3
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


def test_remove_archive_shortcut(populated):
    async def scenario(app, pilot):
        await pilot.press("d")
        await pilot.pause()
        assert isinstance(app.screen, screens.RemoveScreen)
        await pilot.press("a")
        await pilot.pause()
        assert len(app.shown) == 2
    drive(scenario)


@pytest.mark.parametrize("confirm", ["d", "click"])
def test_trash_and_restore_flow(populated, confirm):
    async def scenario(app, pilot):
        target = app.by_key[f"claude:{CLAUDE_ID}"]
        app._q("#table").move_cursor(row=keys(app).index(target.selector))
        await pilot.press("d")
        await settle(pilot, app)
        assert isinstance(app.screen, screens.RemoveScreen) and app.focused.id == "cancel"
        assert os.path.exists(target.storage_path)
        if confirm == "click":
            await pilot.click("#trash")
        else:
            await pilot.press("d")
        await settle(pilot, app)
        assert len(app.screen_stack) == 1 and not os.path.exists(target.storage_path)
        await pilot.press("t")
        await settle(pilot, app)
        await pilot.press("enter")
        await pilot.pause()
        await pilot.click("#confirm")
        await settle(pilot, app)
        assert os.path.exists(target.storage_path)
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


def test_list_position_counter_and_thin_scrollbar(populated):
    from ai_bridge_cli.tui.scrollbar import ThinScrollBarRender

    async def scenario(app, pilot):
        table = app.query_one("#table")
        assert table._border_subtitle.plain.endswith(" · 1 of 3")
        assert [str(c.label) for c in table.columns.values()][3:5] == ["Updated↓", "Status"]  # sort arrow
        await pilot.press("down")
        await settle(pilot, app)
        assert table._border_subtitle.plain.endswith(" · 2 of 3")
        keys = str(app.query_one("#keybar").render())
        assert keys.startswith("Quit: q | Help: ? | ") and "Delete: d | " in keys
    drive(scenario)
    bar = ThinScrollBarRender.render_bar(size=10, virtual_size=40, window_size=10, position=30)
    assert "".join(seg.text for seg in bar.segments) == " " * 8 + "▐" * 2  # thumb at the end, no track


def test_narrow_terminal_layout(populated):
    async def scenario(app, pilot):
        assert app.tier == "narrow" and not app.query_one("#side").display
        assert [c.value for c in app.query_one("#table").columns] == ["mark", "agent", "title", "project"]
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
        await pilot.press("d")
        await settle(pilot, app)
        assert len(app.screen_stack) == 1
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
        assert app._q("#table").row_count == 3
        await pilot.press("escape")
        await pilot.press("down")
        await settle(pilot, app)
    drive(scenario)


def test_quit_with_pending_preview_is_clean(populated):
    async def scenario(app, pilot):
        await pilot.press("down", "down")  # schedules debounced preview loads
        await app.action_quit()
    drive(scenario)


@pytest.mark.parametrize("width", [40, 100, 129, 130])
def test_model_and_title_fit_after_resize(populated, width):
    async def scenario(app, pilot):
        # Also exercise resizing within a tier and with the side pane hidden.
        await pilot.resize_terminal(width, 30)
        await settle(pilot, app)
        table = app.query_one("#table")
        columns = {key.value: col for key, col in table.columns.items()}
        assert columns["title"].width >= 10
        assert table.virtual_size.width <= table.size.width
        # Last Model and Updated drop before Project when space runs out.
        assert ("model" in columns) == (width == 129) and ("project" in columns) == (width > 40)
        if "model" in columns:
            assert str(columns["model"].label) == "Last Model"
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
        from io import StringIO

        from rich.console import Console

        out = StringIO()
        console = Console(file=out, width=60)
        console.print(app.query_one("#details").content)
        assert "Last Model" in out.getvalue()
        grid = app.query_one("#details").content
        labels = grid.columns[0]._cells
        values = grid.columns[1]._cells
        assert values[labels.index("Last Model")].plain == (model or "—")
        assert app.query_one("#table").get_cell(f"claude:{CLAUDE_ID}", "model").plain == (model or "—")
    drive(scenario, size=(200, 30))


@pytest.mark.parametrize("shortcut", ["ctrl+s", "f2"])
def test_settings_keyboard_saves_and_reloads(populated, shortcut):
    async def scenario(app, pilot):
        assert app.settings.launch_mode == "exec"
        await pilot.press(shortcut)
        await pilot.pause()
        assert isinstance(app.screen, screens.SettingsScreen)
        options = app.screen.query_one("OptionList")
        assert options.get_option_at_index(options.highlighted).id == "return"
        assert "OFF" in str(options.get_option("return").prompt)
        assert "browser closes" in str(app.screen.query_one("#explain").render())  # explainer, separate
        await pilot.press("enter")  # toggle on: saved at once, menu stays open
        await pilot.pause()
        assert isinstance(app.screen, screens.SettingsScreen) and "ON" in str(options.get_option("return").prompt)
        assert app.settings.launch_mode == settings.load().launch_mode == "suspend"
        assert browser().settings.launch_mode == "suspend"
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, screens.SettingsScreen)
        await pilot.press(shortcut)
        await pilot.pause()
        await pilot.press("enter")
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


def test_settings_change_auto_refresh_interval(populated):
    async def scenario(app, pilot):
        app.periodic_refresh = True
        app._schedule_auto_refresh()
        assert app.settings.refresh_seconds == 60 and app._refresh_timer._interval == 60
        await pilot.press("f2")
        await pilot.pause()
        options = app.screen.query_one("OptionList")
        assert "● Every 60 seconds (default)" in str(options.get_option("refresh:60").prompt)
        options.highlighted = options.get_option_index("refresh:0")
        await pilot.pause()
        assert "rescans" in str(app.screen.query_one("#explain").render())
        await pilot.press("enter")
        await pilot.pause()
        assert app._refresh_timer is None and settings.load().refresh_seconds == 0
        assert "● Off" in str(options.get_option("refresh:0").prompt)
        assert settings.load().launch_mode == "exec"  # the other setting is kept
        assert app.screen.query_one(".box").border_title == "Settings"
    drive(scenario)


def test_settings_failure_keeps_current_mode(populated, monkeypatch):
    def fail(preference):
        raise ValueError("unsafe config")
    monkeypatch.setattr(settings, "save", fail)

    async def scenario(app, pilot):
        await pilot.press("f2")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert app.settings.launch_mode == "exec"
        assert "OFF" in str(app.screen.query_one("OptionList").get_option("return").prompt)
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
    from io import StringIO

    from rich.console import Console

    from ai_bridge_cli import activity, trash
    from ai_bridge_cli.catalog import Catalog
    session = Catalog().refresh(probe_activity=False).get("claude", CLAUDE_ID)
    plan = trash.build_plan([session], act=activity.Activity(uninspectable=[(42, "codex")]))
    out = StringIO()
    Console(file=out, width=100).print(screens.plan_table(plan.to_json()))
    text = out.getvalue()
    assert "does not prove" in text and "Archive (a)" in text and "never bypasses" in text
    assert not plan.allowed


@pytest.mark.parametrize('width', [40, 100, 150])
def test_numbered_focus_shortcuts(populated, width):
    async def scenario(app, pilot):
        assert not app.query('#pane-nav') and app.theme == 'ansi-dark'
        search = app._q('#search').region
        for selector in ('#agent', '#mode', '#origin', '#status', '#project'):
            control = app._q(selector)
            assert control.display and control.region.width >= 14 and control.region.right <= width
            if width < 130:  # filters wrap onto their own row under search
                assert control.region.y >= search.bottom
        await pilot.press('4')
        await pilot.pause()
        assert app.focused.id == 'side' and app._q('#side').display
        await pilot.press('3')
        await pilot.pause()
        assert app.focused.id == 'table' and app._q('#table').display
        app.details_open = False
        app._layout_side()
        await pilot.press('4')
        await pilot.pause()
        assert app.focused.id == 'side' and app._q('#side').display
        await pilot.press('2')
        await pilot.pause()
        assert app.focused.id == 'agent'
        for select in app._q('#filters').query('Select'):
            assert select.display
        assert app.focused.region.width > 0
        await pilot.press('3', '1', '1', '2', '3', '4', '5')
        await pilot.pause()
        assert app.focused.id == 'search' and app._q('#search').value == '12345'
        await pilot.press('enter', '5')
        await pilot.pause()
        assert isinstance(app.screen, screens.SettingsScreen)
        await pilot.press('1', '2', '3', '4', '5')
        assert isinstance(app.screen, screens.SettingsScreen)
        await pilot.press('escape')
        assert app.focused.id == 'table'
    drive(scenario, size=(width, 24))


def test_numbers_leave_expanded_filter_alone(populated):
    async def scenario(app, pilot):
        await pilot.press('2', 'enter')
        await pilot.pause()
        assert app._q('#agent').expanded
        await pilot.press('3', '5')
        assert app._q('#agent').expanded
        assert not isinstance(app.screen, screens.SettingsScreen)
    drive(scenario)


@pytest.mark.parametrize('cancel', ['c', 'escape', 'enter'])
def test_remove_cancel_never_touches_stores(populated, monkeypatch, cancel):
    from test_trash import digest_tree
    before = digest_tree(populated.claude), digest_tree(populated.codex), digest_tree(populated.cursor)
    plans = []
    monkeypatch.setattr(SessionBrowser, 'run_delete', lambda self, plan: plans.append(plan))

    async def scenario(app, pilot):
        await pilot.press('d')
        await settle(pilot, app)
        assert isinstance(app.screen, screens.RemoveScreen)
        assert app.focused.id == 'cancel'
        await pilot.press(cancel)
        await settle(pilot, app)
        assert len(app.screen_stack) == 1 and not plans
        assert not populated.cli_state.exists() and not populated.cli_data.exists()
        assert before == (digest_tree(populated.claude), digest_tree(populated.codex),
                          digest_tree(populated.cursor))
    drive(scenario)


@pytest.mark.parametrize('cancel', ['n', 'escape', 'enter'])
def test_process_delete_cancel_keeps_files(populated, monkeypatch, cancel):
    from types import SimpleNamespace

    from ai_bridge_cli import trash
    async def scenario(app, pilot):
        target = app.by_key[f'codex:{CODEX_ID}']
        plan = trash.build_plan([target])
        calls = []
        monkeypatch.setattr(app, 'run_delete', lambda *args: calls.append(args))
        app._confirm_process_delete(SimpleNamespace(process=SimpleNamespace(pid=42, argv=(b'codex', b'resume', CODEX_ID.encode())), deletion=plan))
        await pilot.pause()
        assert app.focused.id == 'cancel'
        assert '42' in app.screen.body.plain and target.selector in app.screen.body.plain
        assert 'Unsaved' in app.screen.body.plain
        await pilot.press('d', 'a', 'c')
        assert isinstance(app.screen, screens.DeleteConfirmScreen)
        if cancel == 'n':
            app.screen.query_one('#confirm').focus()
        await pilot.press(cancel)
        await settle(pilot, app)
        assert len(app.screen_stack) == 1 and not calls
        assert os.path.exists(target.storage_path) and not populated.cli_data.exists()
    drive(scenario)


@pytest.mark.parametrize('confirm', ['y', 'click'])
def test_process_delete_explicit_consent(populated, monkeypatch, confirm):
    from types import SimpleNamespace

    from ai_bridge_cli import trash
    async def scenario(app, pilot):
        target = app.by_key[f'codex:{CODEX_ID}']
        plan = trash.build_plan([target])
        stop = SimpleNamespace(process=SimpleNamespace(pid=42, argv=(b'codex', b'resume', CODEX_ID.encode())), deletion=plan)
        calls = []
        monkeypatch.setattr(app, 'run_delete', lambda *args: calls.append(args))
        app._confirm_process_delete(stop)
        await pilot.pause()
        if confirm == 'click':
            await pilot.click('#confirm')
        else:
            await pilot.press('y')
        await settle(pilot, app)
        assert calls == [(plan, stop)]
    drive(scenario)


@pytest.mark.parametrize('blocker', ['active', 'recent', 'uninspectable', 'bridge-job', 'unsupported'])
def test_native_delete_blocked_modal_cannot_confirm(populated, monkeypatch, blocker):
    from dataclasses import replace

    from ai_bridge_cli import activity, trash

    async def scenario(app, pilot):
        target = app.by_key[f'codex:{CODEX_ID}' if blocker == 'unsupported' else f'claude:{CLAUDE_ID}']
        if blocker == 'unsupported':
            monkeypatch.setenv('FAKE_NO_ARCHIVE', '1')
        elif blocker == 'recent':
            os.utime(target.storage_path, None)
        elif blocker == 'bridge-job':
            target = replace(target, active_job_ids=['unfinished-job'])
        act = (activity.Activity(claude_live={CLAUDE_ID: 42}) if blocker == 'active' else
               activity.Activity(uninspectable=[(42, 'codex')]) if blocker == 'uninspectable' else
               activity.Activity())
        plan = trash.build_plan([target], act=act)
        assert not plan.allowed
        calls = []
        monkeypatch.setattr(app, 'run_delete', lambda plan: calls.append(plan))
        app._confirm_delete(plan)
        await settle(pilot, app)
        assert app.screen.query_one('#confirm').disabled
        await pilot.press('y', 'tab', 'enter', 'y')
        await settle(pilot, app)
        assert not calls and os.path.exists(target.storage_path)
        assert not populated.cli_data.exists()
    drive(scenario)


@pytest.mark.parametrize('change', ['file', 'activity', 'uninspectable'])
def test_native_delete_rechecks_after_keyboard_confirmation(populated, monkeypatch, change):
    from ai_bridge_cli import activity

    async def scenario(app, pilot):
        target = app.by_key[f'claude:{CLAUDE_ID}']
        app._q('#table').move_cursor(row=keys(app).index(target.selector))
        await pilot.press('d')
        await settle(pilot, app)
        assert isinstance(app.screen, screens.RemoveScreen)
        if change == 'file':
            with open(target.storage_path, 'a') as stream:
                stream.write('{}\n')
        else:
            act = (activity.Activity(claude_live={CLAUDE_ID: 42}) if change == 'activity' else
                   activity.Activity(uninspectable=[(42, 'codex')]))
            monkeypatch.setattr(activity, 'snapshot', lambda: act)
        await pilot.press('d')
        await settle(pilot, app)
        assert os.path.exists(target.storage_path) and not populated.cli_data.exists()
    drive(scenario)


def test_mixed_selection_refuses_partial_deletion(populated):
    from dataclasses import replace

    from ai_bridge_cli import activity, trash
    async def scenario(app, pilot):
        allowed = app.by_key[f'claude:{CLAUDE_ID}']
        blocked = replace(app.by_key[f'cursor:{CURSOR_ID}'], status='active', status_note='in use')
        plan = trash.build_plan([allowed, blocked], act=activity.Activity())
        app._confirm_delete(plan)
        await settle(pilot, app)
        assert isinstance(app.screen, screens.DeleteConfirmScreen) and not app.screen.allowed
        await pilot.press('y')
        await settle(pilot, app)
        assert os.path.exists(allowed.storage_path) and os.path.exists(blocked.storage_path)
        assert not populated.cli_data.exists()
    drive(scenario)


def assert_focused_pane(app, selector, accent):
    """Check computed CSS, not just focus bookkeeping or stylesheet text."""
    for pane in ('#search', '#filter-controls', '#table', '#side'):
        widget = app._q(pane)
        if pane == selector:
            assert widget.has_focus_within
            assert widget.styles.border_top[1] == accent
            assert widget.styles.border_title_color == accent
            assert widget.styles.border_title_style.bold
            assert widget.region.width > 0 and widget.region.height > 0
        else:
            assert not widget.has_focus_within
            assert widget.styles.border_top[1] != accent


@pytest.mark.parametrize('width', [40, 100, 150])
def test_pane_focus_css_tracks_hotkeys_and_nested_filters(populated, width):
    async def scenario(app, pilot):
        accent = app._q('#table').styles.border_top[1]
        assert_focused_pane(app, '#table', accent)
        for selector, title in (('#search', '[1]-Search'), ('#filter-controls', '[2]-Filters'),
                                ('#table', '[3]-Session List'), ('#side', '[4]-Preview/Details')):
            assert app._q(selector).border_title.startswith(title)
        table = app._q('#table')
        if width > 40:  # count right-aligned in the top border, which it fills exactly
            assert table.border_title.endswith(' 3/4 sessions') and len(table.border_title) == table.outer_size.width - 6
        await pilot.press('1')
        await pilot.pause()
        assert_focused_pane(app, '#search', accent)
        await pilot.press('enter', '2')
        await pilot.pause()
        assert_focused_pane(app, '#filter-controls', accent)
        for selector in ('#agent', '#mode', '#origin', '#status', '#project'):
            control = app._q(selector)
            assert control.region.width > 0 and control.region.bottom <= 24
            await pilot.press('enter')
            await pilot.pause()
            assert control.expanded and control.has_focus_within
            assert_focused_pane(app, '#filter-controls', accent)
            await pilot.press('escape', 'tab')
        await pilot.press('3')
        await pilot.pause()
        assert_focused_pane(app, '#table', accent)
        await pilot.press('4')
        await pilot.pause()
        assert_focused_pane(app, '#side', accent)
        assert app._q('#side').region.bottom <= 24
        if width < 130:
            assert app._q('#side').has_class('full') and not app._q('#table').display
        await pilot.press('3')
        await pilot.pause()
        assert_focused_pane(app, '#table', accent)
    drive(scenario, size=(width, 24))


@pytest.mark.parametrize('width', [40, 150])
def test_pane_focus_css_tracks_clicks_without_marking_rows(populated, width):
    async def scenario(app, pilot):
        accent = app._q('#table').styles.border_top[1]
        await pilot.click('#search')
        await pilot.pause()
        assert_focused_pane(app, '#search', accent)
        # Open the narrow filter layout, then choose a control by mouse.
        await pilot.press('enter', '2')
        await pilot.click('#origin')
        await pilot.pause()
        assert_focused_pane(app, '#filter-controls', accent)
        await pilot.press('escape', '3')
        await pilot.click('#search')
        await pilot.click('#table', offset=(4, 3))
        await pilot.pause()
        assert_focused_pane(app, '#table', accent)
        assert not app.marked
        await pilot.press('4')
        # Static details content should focus its scrollable pane when clicked.
        await pilot.click('#search')
        await pilot.click('#details', offset=(2, 1))
        await pilot.pause()
        assert_focused_pane(app, '#side', accent)
        assert not app.marked
        await pilot.click('#search')
        await pilot.pause()
        assert_focused_pane(app, '#search', accent)
    drive(scenario, size=(width, 24))


@pytest.mark.parametrize('width', [40, 150])
def test_selected_count_stays_prominent_and_separate_from_pane_focus(populated, width):

    async def scenario(app, pilot):
        accent = app._q('#table').styles.border_top[1]
        await pilot.press('space', 'space')
        await pilot.pause()
        status = app._q('#table')._border_subtitle
        assert status.plain.startswith(' 2 selected ')
        assert 'reverse' in str(status.spans[0].style)
        assert_focused_pane(app, '#table', accent)
        marked = set(app.marked)
        await pilot.press('1')
        await pilot.pause()
        assert_focused_pane(app, '#search', accent)
        assert app.marked == marked and app._q('#table')._border_subtitle.plain == status.plain
        # Filter down to a marked row: count only the visible action targets.
        target = app.by_key[next(iter(marked))]
        app._q('#search').value = target.session_id
        await pilot.pause()
        assert app._q('#table')._border_subtitle.plain.startswith(' 1 selected ')
        await pilot.press('escape', 'escape')
        await pilot.pause()
        assert not app.marked and 'selected' not in app._q('#table')._border_subtitle.plain
        assert_focused_pane(app, '#table', accent)
    drive(scenario, size=(width, 24))


def test_normal_multi_delete_has_one_confirmation(populated):
    async def scenario(app, pilot):
        targets = [app.by_key[f'claude:{CLAUDE_ID}'], app.by_key[f'cursor:{CURSOR_ID}']]
        app.marked = {target.selector for target in targets}
        await pilot.press('d')
        await settle(pilot, app)
        assert isinstance(app.screen, screens.RemoveScreen)
        assert '2 session(s)' in app.screen.title_text
        await pilot.press('d')
        await settle(pilot, app)
        assert len(app.screen_stack) == 1
        assert all(not os.path.exists(target.storage_path) for target in targets)
    drive(scenario)


@pytest.mark.parametrize('cancel', ['n', 'escape', 'enter'])
def test_archived_codex_permanent_modal_cancel(env, cancel):
    from test_codex_delete import archived, deleted
    target = archived(env)

    async def scenario(app, pilot):
        await pilot.press('A')
        await settle(pilot, app)
        app._q('#table').move_cursor(row=keys(app).index(target.selector))
        await pilot.press('d')
        await settle(pilot, app)
        assert isinstance(app.screen, screens.RemoveScreen)
        assert 'Permanently delete' in str(app.screen.query_one('#trash').label)
        assert app.focused.id == 'cancel'
        await pilot.press('d')
        await settle(pilot, app)
        assert isinstance(app.screen, screens.DeleteConfirmScreen)
        assert app.focused.id == 'cancel'
        assert CODEX_ID in app.screen.body.plain
        assert 'irreversible' in app.screen.body.plain and 'Descendant' in app.screen.body.plain
        await pilot.press(cancel)
        await settle(pilot, app)
        assert len(app.screen_stack) == 1
        assert not deleted(env) and os.path.exists(target.storage_path)
    drive(scenario, size=(100, 30))


@pytest.mark.parametrize('confirm', ['y', 'click'])
def test_archived_codex_permanent_modal_confirm_and_refresh(env, confirm):
    from test_codex_delete import archived, deleted
    target = archived(env, indexed=True)

    async def scenario(app, pilot):
        await pilot.press('A', 'd')
        await settle(pilot, app)
        await pilot.press('d')
        await settle(pilot, app)
        assert isinstance(app.screen, screens.DeleteConfirmScreen)
        assert not deleted(env)
        if confirm == 'click':
            await pilot.click('#confirm')
        else:
            await pilot.press('y')
        await settle(pilot, app)
        assert deleted(env) == [['delete', '--force', CODEX_ID]]
        assert not os.path.exists(target.storage_path)
        assert target.selector not in app.by_key and not app.shown
    drive(scenario)


def test_permanent_delete_native_error_refreshes_without_success(env, monkeypatch):
    from test_codex_delete import archived
    target = archived(env)
    monkeypatch.setenv('FAKE_DELETE_ERROR', 'thread is busy; unsafe references')
    notifications = []
    real_notify = SessionBrowser.notify

    def notify(self, message, **kwargs):
        notifications.append(message)
        return real_notify(self, message, **kwargs)
    monkeypatch.setattr(SessionBrowser, 'notify', notify)

    async def scenario(app, pilot):
        await pilot.press('A', 'd')
        await settle(pilot, app)
        await pilot.press('d')
        await settle(pilot, app)
        before = app.catalog.refreshed
        await pilot.press('y')
        await settle(pilot, app)
        assert app.catalog.refreshed > before
        assert target.selector in app.by_key and os.path.exists(target.storage_path)
        assert any('not confirmed' in n and 'busy' in n for n in notifications)
        assert not any('Permanently deleted' in n for n in notifications)
    drive(scenario)


def test_permanent_delete_mixed_selection_is_refused(env):
    from test_codex_delete import archived, deleted
    target = archived(env)
    fixtures.codex_session(env.codex, '01a00000-0000-7000-8000-000000000002', env.project)
    env.age()

    async def scenario(app, pilot):
        await pilot.press('A')
        await settle(pilot, app)
        app.marked = {s.selector for s in app.shown}
        await pilot.press('d')
        await settle(pilot, app)
        assert len(app.screen_stack) == 1
        assert not deleted(env) and os.path.exists(target.storage_path)
    drive(scenario)


def test_permanent_delete_error_after_native_changes_reconciles(env, monkeypatch):
    from test_codex_delete import archived
    target = archived(env, indexed=True)
    monkeypatch.setenv('FAKE_DELETE_EXIT_AFTER', '1')
    notifications = []
    monkeypatch.setattr(SessionBrowser, 'notify', lambda self, message, **kwargs: notifications.append(message))

    async def scenario(app, pilot):
        await pilot.press('A', 'd')
        await settle(pilot, app)
        await pilot.press('d')
        await settle(pilot, app)
        await pilot.press('y')
        await settle(pilot, app)
        assert target.selector not in app.by_key and not os.path.exists(target.storage_path)
        assert any('not confirmed' in n for n in notifications)
        assert not any('Permanently deleted' in n for n in notifications)
    drive(scenario)


def assert_buttons_fit(screen, width, rows):
    for button in screen.query('Button'):
        assert button.region.width >= len(button.label.plain)
        assert button.region.x >= 0 and button.region.right <= width and button.region.bottom <= rows


@pytest.mark.parametrize('width', [40, 80, 120])
@pytest.mark.parametrize('kind', ['claude', 'codex', 'codex-archived'])
def test_removal_modal_layout_is_balanced_and_unclipped(env, width, kind):
    rows = 20
    if kind == 'claude':
        fixtures.claude_session(env.claude, CLAUDE_ID, env.project)
    else:
        fixtures.codex_session(env.codex, CODEX_ID, env.project, archived=kind == 'codex-archived')
    env.age()

    async def scenario(app, pilot):
        if kind == 'codex-archived':
            await pilot.press('A')
        await pilot.press('d')
        await settle(pilot, app)
        screen = app.screen
        assert isinstance(screen, screens.RemoveScreen)
        buttons = {b.id: b for b in screen.query('Button')}
        assert set(buttons) == {'archive', 'trash', 'cancel', 'stop', 'force'}
        assert_buttons_fit(screen, width, rows)
        for button in buttons.values():
            label = button.label.plain
            assert label[1] == ' ' and label[0].isalpha()
            assert button.region.width >= len(label) + (0 if screen.query_one('.removal').has_class('stacked') else 2)
            assert button.region.height == 1  # flat lazygit-style buttons
        top = [buttons[i].region for i in ('archive', 'trash', 'cancel')]
        bottom = [buttons[i].region for i in ('stop', 'force')]
        if width < screens.RemoveScreen.STACK_BELOW:
            assert screen.query_one('.removal').has_class('stacked')
            assert len({r.width for r in top + bottom}) == 1
            assert len({r.x for r in top + bottom}) == 1
        else:
            assert not screen.query_one('.removal').has_class('stacked')
            assert len({r.y for r in top}) == 1 and len({r.y for r in bottom}) == 1
            assert max(r.width for r in top) - min(r.width for r in top) <= 1
            assert max(r.width for r in bottom) - min(r.width for r in bottom) <= 1
            assert len({r.x for r in (top[0], bottom[0])}) == 1
        if kind == 'codex-archived':
            await pilot.press('d')
            await settle(pilot, app)
            assert isinstance(app.screen, screens.DeleteConfirmScreen)
            assert_buttons_fit(app.screen, width, rows)
        await pilot.press('escape')
    drive(scenario, size=(width, rows))


def test_preview_shows_last_user_messages_and_last_reply_only(populated):
    from ai_bridge_cli.tui.app import conversation_preview

    msgs = ([{"role": "user", "text": f"ask {i}"} for i in range(7)]
            + [{"role": "tool", "text": "ls -la"}, {"role": "assistant", "text": "first"},
               {"role": "tool", "text": "cat x"}, {"role": "assistant", "text": "final answer"}])
    picked = conversation_preview(msgs)
    assert [m["text"] for m in picked] == ["ask 2", "ask 3", "ask 4", "ask 5", "ask 6", "final answer"]

    async def scenario(app, pilot):
        app._render_preview(app.current(), picked)
        text = app.query_one("#preview").content.plain
        assert text.startswith("Your last 5 messages") and "Last reply\n\nfinal answer" in text
        assert "ls -la" not in text and "ask 1" not in text
    drive(scenario)


@pytest.mark.parametrize("list_size,page,position,area,expected", [
    (5, 10, 0, 20, (0, 20)), (10, 10, 0, 20, (0, 20)), (10, 5, 0, 20, (0, 10)), (10, 5, 5, 20, (10, 10)),
    (15, 5, 5, 21, (7, 7)), (15, 5, 10, 21, (14, 7)), (15, 5, 1, 21, (2, 7)), (15, 5, 9, 21, (12, 7)),
])
def test_scrollbar_thumb_matches_gocui(list_size, page, position, area, expected):
    from ai_bridge_cli.tui.scrollbar import lazygit_thumb  # cases from gocui's scrollbar_test.go
    assert lazygit_thumb(list_size, page, position, area) == expected


def test_scroll_thumb_is_drawn_on_the_list_border(env):
    for i in range(30):
        fixtures.claude_session(env.claude, f"5c1f0e2a-7b3d-4c9e-8a61-{i:012d}", env.project, f"chat {i}")
    fixtures.age(env.claude, env.codex, env.cursor)

    async def scenario(app, pilot):
        table = app.query_one("#table")
        rows = table.thumb_rows()
        assert rows and rows.start == 1  # at the top: the thumb starts right under the top border
        right = table.outer_size.width - 1
        lines = table.render_lines(table.region.reset_offset)
        assert "".join(s.text for s in lines[rows.start].crop(right, right + 1)) == "▐"
        assert "".join(s.text for s in lines[rows.stop].crop(right, right + 1)) == "│"
        table.move_cursor(row=table.row_count - 1, animate=False)
        await settle(pilot, app)
        assert table.thumb_rows().stop == table.size.height + 1  # snapped to the bottom
    drive(scenario, size=(150, 20))
