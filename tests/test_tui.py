import asyncio
import contextlib
import json
import os

import fixtures
from conftest import CLAUDE_ID, CODEX_ID, CURSOR_ID

from ai_bridge_cli import native
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
    ran = []
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
        assert [c.value for c in app.query_one("#table").columns] == ["mark", "agent", "status", "updated", "title"]
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
