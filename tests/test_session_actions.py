"""All stores are temporary; process tests signal only disposable fixture children."""

import json
import os
from dataclasses import replace
from pathlib import Path

import fixtures
import pytest
from conftest import CLAUDE_ID, CODEX_CHILD, CODEX_ID, CURSOR_ID

from agentls import activity, codex_delete, mcp, session_actions as actions
from agentls.catalog import Catalog
from agentls.fsutil import UnsafePath

pytest_plugins = ["test_process_delete"]


def selected(agent="codex", sid=CODEX_ID):
    return Catalog().refresh(probe_activity=False).get(agent, sid)


@pytest.mark.parametrize("status", ["idle", "active", "recent", "uncertain"])
def test_native_force_unarchived_statuses(populated, status):
    row = replace(selected(), status=status)
    plan = codex_delete.prepare_force([row])
    assert {s.session_id for s in plan.affected} == {CODEX_ID, CODEX_CHILD}
    assert not any(a[:2] == ["delete", "--force"] for a in populated.calls())
    assert codex_delete.execute_force(plan) == [row.selector]
    assert not Path(row.storage_path).exists()
    assert not populated.cli_data.exists()


def test_native_force_allows_live_evidence_without_signal(populated, monkeypatch):
    row = selected()
    info = os.stat(row.storage_path)
    act = activity.Activity(
        open_files={(info.st_dev, info.st_ino): 987654},
        cmdline_ids={CODEX_CHILD: 987654},
        uninspectable=[(987654, "codex")],
    )
    monkeypatch.setattr(activity, "snapshot", lambda: act)
    signals = []
    monkeypatch.setattr(
        actions.signal, "pidfd_send_signal", lambda *a: signals.append(a)
    )
    plan = codex_delete.prepare_force([row])
    assert "987654" in "\n".join(plan.processes) and plan.inspection_note
    codex_delete.execute_force(plan)
    assert not signals


def test_native_force_content_can_change_scope_cannot(populated):
    row = selected()
    plan = codex_delete.prepare_force([row])
    with open(row.storage_path, "a") as stream:
        stream.write("{}\n")
    codex_delete.execute_force(plan)
    assert not Path(row.storage_path).exists()


@pytest.mark.parametrize(
    "change",
    ["descendant", "replace", "duplicate", "hardlink", "symlink", "binary", "index"],
)
def test_force_revalidates_before_native_mutation(populated, change):
    row = selected()
    plan = codex_delete.prepare_force([row])
    path = Path(row.storage_path)
    if change == "descendant":
        fixtures.codex_session(
            populated.codex,
            "01a00000-0000-7000-8000-000000000003",
            populated.project,
            parent=CODEX_CHILD,
        )
    elif change == "replace":
        path.rename(path.with_suffix(".old"))
        path.write_bytes(path.with_suffix(".old").read_bytes())
    elif change == "duplicate":
        fixtures.codex_session(
            populated.codex, CODEX_ID, populated.project, archived=True
        )
    elif change == "hardlink":
        os.link(path, populated.root / "link")
    elif change == "symlink":
        path.rename(populated.root / "actual")
        path.symlink_to(populated.root / "actual")
    elif change == "binary":
        binary = Path(plan.binary)
        binary.write_text(binary.read_text() + "\n# replaced\n")
    else:
        import sqlite3

        with sqlite3.connect(populated.codex / "state_5.sqlite") as con:
            con.execute(
                "CREATE TABLE threads (id TEXT, rollout_path TEXT, archived INTEGER)"
            )
            con.execute("INSERT INTO threads VALUES (?, ?, 1)", (CODEX_ID, str(path)))
    with pytest.raises((OSError, ValueError)):
        codex_delete.execute_force(plan)
    assert not any(a[:2] == ["delete", "--force"] for a in populated.calls())


def test_force_group_deduplicates_selected_descendants(populated):
    parent, child = selected(), selected(sid=CODEX_CHILD)
    plan = codex_delete.prepare_force([child, parent, parent])
    assert len(plan.sessions) == 1 and len(plan.affected) == 2
    codex_delete.execute_force(plan)
    assert [a for a in populated.calls() if a[:2] == ["delete", "--force"]] == [
        ["delete", "--force", CODEX_ID]
    ]


def test_force_group_reports_partial_native_failure(populated, monkeypatch):
    other = "01a00000-0000-7000-8000-000000000003"
    fixtures.codex_session(populated.codex, other, populated.project)
    plan = codex_delete.prepare_force([selected(), selected(sid=other)])
    run = codex_delete._run

    def fail_second(argv, *args, **kwargs):
        if argv[1:3] == ["delete", "--force"] and argv[-1] == other:
            raise UnsafePath("mock native busy refusal")
        return run(argv, *args, **kwargs)

    monkeypatch.setattr(codex_delete, "_run", fail_second)
    with pytest.raises(UnsafePath, match="Batch is not atomic") as error:
        codex_delete.execute_force(plan)
    assert (
        CODEX_ID in str(error.value) and Path(selected(sid=other).storage_path).exists()
    )


def test_stop_idle_retains_files_and_has_no_signals(populated, monkeypatch):
    signals = []
    monkeypatch.setattr(
        actions.signal, "pidfd_send_signal", lambda *a: signals.append(a)
    )
    plan = actions.prepare_stop([selected()])
    assert len(plan.affected) == 2 and not plan.targets
    assert actions.execute_stop(plan) == []
    assert not signals and Path(selected().storage_path).exists()


def test_independent_stop_active_retains_chat(process, populated):
    row, launch = process
    child = launch()
    plan = actions.prepare_stop([replace(row, status="active")])
    assert plan.targets[0].process.pid == child.pid
    assert actions.execute_stop(plan) == [row.selector]
    assert child.wait(timeout=1) == -15
    assert Path(row.storage_path).exists() and not populated.cli_data.exists()


def test_independent_stop_allows_recent_content(process):
    row, launch = process
    child = launch()
    os.utime(row.storage_path, None)
    plan = actions.prepare_stop([replace(row, status="recent")])
    with open(row.storage_path, "a") as stream:
        stream.write("{}\n")
    actions.execute_stop(plan)
    assert child.wait(timeout=1) == -15 and Path(row.storage_path).exists()


def test_independent_stop_no_sigkill(process):
    row, launch = process
    child = launch(ignore=True)
    with pytest.raises(UnsafePath, match="No SIGKILL"):
        actions.execute_stop(actions.prepare_stop([row]), timeout=0.01)
    assert child.poll() is None and Path(row.storage_path).exists()


def test_stop_shared_daemon_refused(process):
    row, launch = process
    child = launch(argv=["app-server", "--managed-daemon"])
    with pytest.raises(UnsafePath, match="shared daemons"):
        actions.prepare_stop([row])
    assert child.poll() is None


def test_group_failure_preflight_stops_nobody(process, populated, monkeypatch):
    row, launch = process
    child = launch()
    original = activity.snapshot
    claude = selected("claude", CLAUDE_ID)

    def incomplete():
        act = original()
        act.claude_live[CLAUDE_ID] = child.pid
        return act

    monkeypatch.setattr(activity, "snapshot", incomplete)
    with pytest.raises(UnsafePath):
        actions.prepare_stop([row, claude])
    assert child.poll() is None


def test_process_identity_change_after_pidfd_never_signals(process, monkeypatch):
    row, launch = process
    child = launch()
    plan = actions.prepare_stop([row])
    original = actions.os.pidfd_open
    signals = []

    def pin(pid):
        fd = original(pid)
        monkeypatch.setattr(
            actions,
            "inspect",
            lambda *a: replace(plan.targets[0].process, start="reused"),
        )
        return fd

    monkeypatch.setattr(actions.os, "pidfd_open", pin)
    monkeypatch.setattr(
        actions.signal, "pidfd_send_signal", lambda *a: signals.append(a)
    )
    with pytest.raises(UnsafePath, match="identity"):
        actions.execute_stop(plan)
    assert not signals and child.poll() is None


@pytest.mark.parametrize("agent,sid", [("claude", CLAUDE_ID), ("cursor", CURSOR_ID)])
def test_force_quarantine_is_recoverable(populated, agent, sid):
    from agentls import trash

    row = selected(agent, sid)
    plan = actions.prepare_quarantine([row])
    result = actions.execute_quarantine(plan)
    assert result["moved"] == [row.selector] and not Path(row.storage_path).exists()
    trash.restore(result["batch_id"])
    assert Path(row.storage_path).exists()


def test_quarantine_never_overrides_recent_or_incomplete(populated, monkeypatch):
    row = selected("claude", CLAUDE_ID)
    os.utime(row.storage_path, None)
    with pytest.raises(UnsafePath, match="120"):
        actions.execute_quarantine(actions.prepare_quarantine([row]))
    assert Path(row.storage_path).exists()
    monkeypatch.setattr(
        activity, "snapshot", lambda: activity.Activity(uninspectable=[(55, "agent")])
    )
    with pytest.raises(UnsafePath, match="incomplete"):
        actions.prepare_quarantine([row])


def fake_stat(proc, pid, start="123", state="S"):
    base = proc / str(pid)
    base.mkdir(exist_ok=True, parents=True)
    (base / "stat").write_text(
        f"{pid} (fixture) {state} 1 10 " + "0 " * 16 + start + " 0"
    )
    (base / "cmdline").write_bytes(b"fixture-runner\0")


def mcp_job(env, monkeypatch, status="cleanup_incomplete"):
    proc = env.root / "proc"
    proc.mkdir()
    monkeypatch.setattr(mcp, "PROC", proc)
    monkeypatch.setattr(activity, "PROC", proc)
    monkeypatch.setattr(activity, "_ancestors", lambda: set())
    ident = {"boot_id": mcp.BOOT_ID, "start_ticks": "123", "pgid": 10}
    job = {
        "id": "1018-000001-abcdef",
        "kind": "agent",
        "agent": "codex",
        "session_id": CODEX_ID,
        "status": status,
        "runner": True,
        "pid": 4001,
        "identity": ident,
        "ended": 100,
        "exit_code": 0,
    }
    jobs = env.state / "jobs"
    jobs.mkdir(parents=True)
    path = jobs / (job["id"] + ".json")
    path.write_text(json.dumps(job))
    path.chmod(0o600)
    side = jobs / (job["id"] + ".process.json")
    side.write_text(
        json.dumps({"pid": 4002, "identity": ident, "descendants": {"4003": ident}})
    )
    side.chmod(0o600)
    return mcp.McpState(), path, side, proc


def test_stale_cleanup_refreshes_without_server_writes(populated, monkeypatch):
    state, path, side, proc = mcp_job(populated, monkeypatch)
    before = path.read_bytes(), side.read_bytes()
    fake_stat(proc, 4003)
    assert state.jobs()[0]["status"] == "cleanup_incomplete"
    # Same record + same cached state, but the last known child has now ended.
    fake_stat(proc, 4003, state="Z")
    assert state.jobs()[0]["identity_state"] == "ended_lineage_gone"
    catalog = Catalog(mcp=state).refresh()
    row = catalog.get("codex", CODEX_ID)
    assert row.status == "idle" and not row.active_job_ids
    from agentls import trash

    assert trash.build_plan([row]).allowed
    assert before == (path.read_bytes(), side.read_bytes())


@pytest.mark.parametrize(
    "case",
    [
        "missing-lineage",
        "missing-identity",
        "live-runner",
        "live-target",
        "unreadable",
        "not-ended",
    ],
)
def test_cleanup_missing_or_live_proof_stays_uncertain(populated, monkeypatch, case):
    state, path, side, proc = mcp_job(populated, monkeypatch)
    if case == "missing-lineage":
        side.unlink()
    elif case in ("missing-identity", "not-ended"):
        job = json.loads(path.read_text())
        job.pop("identity" if case == "missing-identity" else "ended")
        path.write_text(json.dumps(job))
    elif case == "unreadable":
        (proc / "4003").mkdir()
        (proc / "4003" / "stat").write_text("unreadable")
    else:
        fake_stat(proc, 4001 if case == "live-runner" else 4002)
    assert state.jobs()[0]["status"] == "cleanup_incomplete"


def test_cleanup_pid_reuse_does_not_count_as_saved_lineage(populated, monkeypatch):
    state, path, side, proc = mcp_job(populated, monkeypatch)
    fake_stat(proc, 4003, start="999")
    assert state.jobs()[0]["identity_state"] == "ended_lineage_gone"


def test_live_session_evidence_wins_over_ended_job(populated, monkeypatch):
    state, path, side, proc = mcp_job(populated, monkeypatch)
    monkeypatch.setattr(
        activity, "snapshot", lambda: activity.Activity(cmdline_ids={CODEX_ID: 4999})
    )
    assert Catalog(mcp=state).refresh().get("codex", CODEX_ID).status == "active"


def test_stop_refuses_session_with_running_mcp_job(populated, monkeypatch):
    state, path, side, proc = mcp_job(populated, monkeypatch, status="running")
    for pid in (4001, 4002, 4003):
        fake_stat(proc, pid)
    with pytest.raises(UnsafePath, match="stop it through the MCP server"):
        actions.prepare_stop([selected()])
    assert Path(selected().storage_path).exists()


@pytest.mark.parametrize(
    "mode,match",
    [
        ("FAKE_DELETE_ERROR", "busy"),
        ("FAKE_DELETE_NOOP", "remain"),
        ("FAKE_NO_FORCE", "advertise"),
    ],
)
def test_native_force_refusal_never_reports_success(
    populated, monkeypatch, mode, match
):
    row = selected()
    if mode == "FAKE_NO_FORCE":
        monkeypatch.setenv(mode, "1")
        with pytest.raises(UnsafePath, match=match):
            codex_delete.prepare_force([row])
    else:
        plan = codex_delete.prepare_force([row])
        monkeypatch.setenv(
            mode, "native thread busy" if mode == "FAKE_DELETE_ERROR" else "1"
        )
        with pytest.raises(UnsafePath, match=match):
            codex_delete.execute_force(plan)
    assert Path(row.storage_path).exists()


@pytest.mark.parametrize("choice", ["n", "enter", "escape", "y"])
def test_force_tui_grouped_preview_and_cancel(populated, choice):
    from test_tui import drive, keys, settle
    from agentls.tui import screens

    async def scenario(app, pilot):
        app._q("#table").move_cursor(row=keys(app).index(f"codex:{CODEX_ID}"))
        await pilot.press("D")
        await settle(pilot, app)
        assert isinstance(app.screen, screens.DeleteConfirmScreen)
        body = app.screen.body.plain
        assert CODEX_ID in body and CODEX_CHILD in body
        assert "Irreversible" in body and "delete --force" in body
        assert app.focused.id == "cancel"
        assert Path(selected().storage_path).exists()
        await pilot.press(choice)
        await settle(pilot, app)
        assert len(app.screen_stack) == 1
        assert bool(list(populated.codex.rglob(f"*{CODEX_ID}.jsonl"))) == (
            choice != "y"
        )

    drive(scenario, size=(40, 20))


def test_stop_tui_retains_chat_and_warns(process, populated):
    from test_tui import drive, keys, settle
    from agentls.tui import screens

    row, launch = process
    child = launch()

    async def scenario(app, pilot):
        app._q("#table").move_cursor(row=keys(app).index(row.selector))
        await pilot.press("x")
        await settle(pilot, app)
        assert isinstance(app.screen, screens.DeleteConfirmScreen)
        assert (
            str(child.pid) in app.screen.body.plain
            and CODEX_ID in app.screen.body.plain
        )
        assert (
            "SIGTERM" in app.screen.body.plain
            and "Unsaved work" in app.screen.body.plain
        )
        assert child.poll() is None and app.focused.id == "cancel"
        await pilot.press("y")
        await settle(pilot, app)
        assert child.wait(timeout=1) == -15 and Path(row.storage_path).exists()
        assert not populated.cli_data.exists()

    drive(scenario, size=(40, 20))


def test_remove_menu_exposes_independent_actions(populated):
    from test_tui import drive, keys, settle
    from agentls.tui import screens

    async def scenario(app, pilot):
        app._q("#table").move_cursor(row=keys(app).index(f"codex:{CODEX_ID}"))
        await pilot.press("d")
        await settle(pilot, app)
        assert isinstance(app.screen, screens.RemoveScreen)
        assert app.screen.query_one("#stop").label.plain == "x Stop session"
        assert app.screen.query_one("#force").label.plain == "f Force delete"
        await pilot.press("f")
        await settle(pilot, app)
        assert isinstance(app.screen, screens.DeleteConfirmScreen)
        assert "Irreversible" in app.screen.body.plain
        await pilot.press("n")
        await settle(pilot, app)
        assert Path(selected().storage_path).exists()

    drive(scenario, size=(40, 20))


@pytest.mark.parametrize(
    "agent,sid,name",
    [("claude", CLAUDE_ID, "claude"), ("cursor", CURSOR_ID, "cursor-agent")],
)
def test_verified_claude_cursor_native_process_scope(
    populated, monkeypatch, agent, sid, name
):
    import shutil
    from agentls.tui import process_delete

    row = selected(agent, sid)
    root = populated.root / "fake-proc"
    base = root / "800001"
    base.mkdir(parents=True)
    binary = populated.root / "native-fixture" / name
    binary.parent.mkdir()
    shutil.copyfile("/usr/bin/true", binary)
    (base / "exe").symlink_to(binary)
    (base / "status").write_text("Uid:\t" + "\t".join([str(os.geteuid())] * 4))
    fake_stat(root, 800001)
    (base / "cmdline").write_bytes(f"{name}\0--resume\0{sid}\0".encode())
    (base / "fd").mkdir()
    (base / "fd" / "3").symlink_to(row.storage_path)
    (base / "task" / "800001").mkdir(parents=True)
    (base / "task" / "800001" / "children").write_text("")
    monkeypatch.setattr(activity, "PROC", root)
    monkeypatch.setattr(activity, "_ancestors", lambda: set())
    monkeypatch.setattr(process_delete, "find_binary", lambda agent: str(binary))
    info = os.stat(row.storage_path)
    key = info.st_dev, info.st_ino
    monkeypatch.setattr(
        activity,
        "snapshot",
        lambda: activity.Activity(
            open_files={key: 800001}, file_holders={key: {800001}}
        ),
    )
    plan = actions.prepare_stop([row])
    assert plan.targets[0].process.pid == 800001
    (base / "task" / "800001" / "children").write_text("800002")
    with pytest.raises(UnsafePath, match="child processes"):
        actions.prepare_stop([row])


def test_native_force_previews_mcp_background_lineage(populated, monkeypatch):
    state, path, side, proc = mcp_job(populated, monkeypatch, status="running")
    for pid in (4001, 4002, 4003):
        fake_stat(proc, pid)
    plan = codex_delete.prepare_force([selected()])
    preview = "\n".join(plan.processes)
    assert (
        "1018-000001-abcdef" in preview
        and "4003" in preview
        and "fixture-runner" in preview
    )
    fake_stat(proc, 4003, start="999")
    with pytest.raises(UnsafePath, match="identities"):
        codex_delete.execute_force(plan)
    assert Path(selected().storage_path).exists()


def test_native_force_cyclic_selected_ancestry_is_refused(populated):
    path = Path(selected().storage_path)
    row = json.loads(path.read_text().splitlines()[0])
    row["payload"]["parent_thread_id"] = CODEX_CHILD
    lines = path.read_text().splitlines()
    path.write_text(json.dumps(row) + "\n" + "\n".join(lines[1:]) + "\n")
    with pytest.raises(UnsafePath, match="Cyclic"):
        codex_delete.prepare_force([selected(), selected(sid=CODEX_CHILD)])


def test_group_stop_exact_native_parent_and_descendant(process, populated, monkeypatch):
    import subprocess
    from agentls.tui import process_delete

    row, launch = process
    parent = launch()
    child_row = selected(sid=CODEX_CHILD)
    binary = process_delete.find_binary("codex")
    child = subprocess.Popen(
        [binary, "resume", CODEX_CHILD],
        env={**os.environ, "ROLLOUT": child_row.storage_path},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert child.stdout.readline() == b"ready\n"

        def snapshot():
            act = activity.Activity()
            for pid, session in ((parent.pid, row), (child.pid, child_row)):
                if activity.proc_start_ticks(pid) is None:
                    continue
                info = os.stat(session.storage_path)
                key = info.st_dev, info.st_ino
                act.open_files[key] = pid
                act.file_holders[key] = {pid}
            return act

        monkeypatch.setattr(activity, "snapshot", snapshot)
        plan = actions.prepare_stop([row, child_row])
        assert {t.process.pid for t in plan.targets} == {parent.pid, child.pid}
        assert set(actions.execute_stop(plan)) == {row.selector, child_row.selector}
        assert parent.wait(timeout=1) == child.wait(timeout=1) == -15
        assert Path(row.storage_path).exists() and Path(child_row.storage_path).exists()
    finally:
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=1)
        child.stdout.close()
        child.stderr.close()


def test_per_root_preparation_cannot_expand_grouped_consent(populated, monkeypatch):
    row = selected()
    plan = codex_delete.prepare_force([row])
    prepare = codex_delete.prepare_force
    calls = []
    extra = "01a00000-0000-7000-8000-000000000004"

    def changed(sessions):
        calls.append(sessions)
        if len(calls) == 2:
            fixtures.codex_session(
                populated.codex, extra, populated.project, parent=CODEX_ID
            )
        return prepare(sessions)

    monkeypatch.setattr(codex_delete, "prepare_force", changed)
    with pytest.raises(UnsafePath, match="Grouped native scope changed"):
        codex_delete.execute_force(plan)
    assert Path(row.storage_path).exists()
    assert not any(a[:2] == ["delete", "--force"] for a in populated.calls())


def test_new_descendant_after_stop_blocks_quarantine(populated, monkeypatch):
    row = selected("claude", CLAUDE_ID)
    plan = actions.prepare_quarantine([row])

    def ended(_):
        affected = list(plan.stop.affected) + [
            replace(
                row,
                session_id="01a00000-0000-7000-8000-000000000004",
                parent_session_id=CLAUDE_ID,
            )
        ]
        monkeypatch.setattr(actions, "_scope", lambda _: affected)

    monkeypatch.setattr(actions, "execute_stop", ended)
    with pytest.raises(UnsafePath, match="descendant sessions changed"):
        actions.execute_quarantine(plan)
    assert Path(row.storage_path).exists()


@pytest.mark.parametrize("agent,sid", [("claude", CLAUDE_ID), ("cursor", CURSOR_ID)])
def test_related_background_writer_prevents_stop_and_quarantine(
    populated, monkeypatch, agent, sid
):
    from agentls import trash

    row = selected(agent, sid)
    if agent == "claude":
        related = Path(row.storage_path).with_suffix("") / "subagents" / "agent-1.jsonl"
    else:
        related = Path(row.storage_path).with_name("store.db-wal")
        related.write_bytes(b"background writer fixture")
        populated.age()
    info = related.stat()
    key = info.st_dev, info.st_ino
    monkeypatch.setattr(
        activity,
        "snapshot",
        lambda: activity.Activity(
            open_files={key: 987654}, file_holders={key: {987654}}
        ),
    )
    with pytest.raises((OSError, ValueError)):
        actions.prepare_stop([row])
    plan = trash.build_plan([row])
    assert not plan.allowed and "related session file" in str(plan.items[0].blockers)
    assert related.exists() and Path(row.storage_path).exists()
