"""Regression tests for the independent security review findings."""
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import fixtures
import pytest
from conftest import CLAUDE_ID, CODEX_ID, CURSOR_ID, REAL_SNAPSHOT

from agentls import activity, fsutil, trash
from agentls.activity import Activity
from agentls.catalog import Catalog
from agentls.scanners import Scanner

SRC = str(Path(__file__).parents[1] / "src")


def catalog():
    return Catalog().refresh(probe_activity=False)


def touch_old(path, when=fixtures.OLD + 10):
    os.utime(path, (when, when))


def batch_manifest(env, batch_id):
    return json.loads((env.cli_data / "trash" / batch_id / "manifest.json").read_text())


# -- (1) recursive fingerprints, recency, revalidation -------------------------------

def test_cursor_in_place_wal_write_after_preview_is_refused(populated):
    session = catalog().get("cursor", CURSOR_ID)
    plan = trash.build_plan([session], Activity())
    assert plan.items[0].allowed
    wal = Path(session.storage_path).with_name("store.db-wal")
    wal.write_bytes(b"\0" * 64)               # in-place change inside the chat folder
    touch_old(wal)
    touch_old(wal.parent)                      # even with timestamps forged to look old
    with pytest.raises(ValueError, match="changed since the preview"):
        trash.execute(plan)
    assert Path(session.storage_path).exists()


def test_cursor_store_db_rewrite_in_place_is_refused(populated):
    session = catalog().get("cursor", CURSOR_ID)
    plan = trash.build_plan([session], Activity())
    db = Path(session.storage_path)
    with open(db, "r+b") as stream:            # same inode, same size, mtime forged back
        stream.seek(0)
        stream.write(stream.read(16))
    touch_old(db, fixtures.OLD)
    with pytest.raises(ValueError):
        trash.execute(plan)                    # ctime moved even though mtime/size did not


def test_nested_claude_file_change_is_refused(populated):
    session = catalog().get("claude", CLAUDE_ID)
    plan = trash.build_plan([session], Activity())
    nested = Path(session.storage_path).with_suffix("") / "subagents" / "agent-1.jsonl"
    with open(nested, "a") as stream:
        stream.write("{}\n")
    touch_old(nested)
    with pytest.raises(ValueError):
        trash.execute(plan)
    assert nested.exists() and Path(session.storage_path).exists()


def test_recent_nested_file_blocks_plan(populated):
    session = catalog().get("claude", CLAUDE_ID)
    nested = Path(session.storage_path).with_suffix("") / "subagents" / "agent-1.jsonl"
    os.utime(nested, None)  # now
    plan = trash.build_plan([session], Activity())
    assert any("Modified within" in b for b in plan.items[0].blockers)


def test_recent_status_blocks(populated):
    from dataclasses import replace
    session = replace(catalog().get("claude", CLAUDE_ID), status="recent", status_note="just written")
    assert any("Status is recent" in b for b in trash.build_plan([session], Activity()).items[0].blockers)


def test_change_between_revalidation_window_is_detected(populated, monkeypatch):
    """Revalidation runs immediately before each rename, after the journal write."""
    session = catalog().get("cursor", CURSOR_ID)
    plan = trash.build_plan([session], Activity())
    db = Path(session.storage_path)

    def tamper(point):
        if point == "before_rename":
            with open(db.with_name("store.db-wal"), "ab") as stream:
                stream.write(b"x")
            touch_old(db.with_name("store.db-wal"))
            touch_old(db.parent)
    monkeypatch.setattr(trash, "_fault", tamper)
    result = trash.execute(plan)
    assert not result["moved"] and "changed" in result["failed"][0]["error"]
    assert db.exists()
    assert batch_manifest(populated, result["batch_id"])["items"][0]["status"] == "failed"


def test_symlink_or_fifo_inside_session_folder_blocks(populated):
    session = catalog().get("claude", CLAUDE_ID)
    folder = Path(session.storage_path).with_suffix("")
    os.mkfifo(folder / "pipe")
    os.symlink("/etc/passwd", folder / "link")
    fixtures.age(folder)
    blockers = trash.build_plan([session], Activity()).items[0].blockers
    assert any("special files" in b for b in blockers) and any("symlinks" in b for b in blockers)


def test_oversized_tree_is_refused(populated, monkeypatch):
    real = fsutil.tree_fingerprint
    monkeypatch.setattr(trash, "tree_fingerprint", lambda fd: real(fd, max_entries=1))
    session = catalog().get("claude", CLAUDE_ID)
    assert any("Too large" in b for b in trash.build_plan([session], Activity()).items[0].blockers)


# -- (2) write-ahead journal, crash recovery, partial restore, purge refusal -----------

CRASH = r"""
import os, sys
sys.path.insert(0, sys.argv[1])
from agentls import activity, trash
from agentls.catalog import Catalog
activity.snapshot = lambda: activity.Activity()
point, count = sys.argv[2], int(sys.argv[3])
seen = []
def fault(p):
    if p == point:
        seen.append(p)
        if len(seen) == count:
            os._exit(9)          # hard crash: no except/finally handlers run
trash._fault = fault
session = Catalog().refresh(probe_activity=False).get("claude", sys.argv[4])
plan = trash.build_plan([session], activity.Activity())
trash.execute(plan)
"""


def crash(point, count, sid=CLAUDE_ID):
    return subprocess.run([sys.executable, "-c", CRASH, SRC, point, str(count), sid],
                          capture_output=True, text=True, env=os.environ.copy())


@pytest.mark.parametrize("point,count", [("before_rename", 1), ("after_rename", 1),
                                         ("before_rename", 2), ("after_rename", 2)])
def test_crash_mid_move_never_strands_files(populated, point, count):
    before = fixtures_digest(populated.claude)
    proc = crash(point, count)
    assert proc.returncode == 9, proc.stderr
    batches = trash.list_batches()
    assert len(batches) == 1
    status = batches[0]["items"][0]["status"]
    assert status in ("partial", "failed", "trashed")
    if status == "partial":
        with pytest.raises(ValueError, match="Refusing to purge"):
            trash.purge(batches[0]["batch_id"])
    trash.restore(batches[0]["batch_id"])
    assert fixtures_digest(populated.claude) == before   # every object accounted for and back
    assert trash.list_batches()[0]["items"][0]["status"] in ("restored", "failed")
    trash.purge(batches[0]["batch_id"])                    # resolved batches can be purged


def fixtures_digest(root):
    import hashlib
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file() and not p.is_symlink()}


def test_journal_write_failure_before_rename_moves_nothing(populated, monkeypatch):
    before = fixtures_digest(populated.claude)
    session = catalog().get("claude", CLAUDE_ID)
    plan = trash.build_plan([session], Activity())
    real, calls = trash._write_manifest, []

    def flaky(batch, manifest):
        calls.append(1)
        if len(calls) == 3:   # 1: batch header, 2: item planned, 3: first "moving" intent
            raise OSError(28, "No space left on device")
        real(batch, manifest)
    monkeypatch.setattr(trash, "_write_manifest", flaky)
    result = trash.execute(plan)
    assert result["failed"] and not result["moved"]
    monkeypatch.setattr(trash, "_write_manifest", real)
    assert fixtures_digest(populated.claude) == before


def test_journal_write_failure_after_rename_is_recoverable(populated, monkeypatch):
    before = fixtures_digest(populated.claude)
    session = catalog().get("claude", CLAUDE_ID)
    plan = trash.build_plan([session], Activity())
    real, calls = trash._write_manifest, []

    def flaky(batch, manifest):
        calls.append(1)
        if len(calls) >= 4:   # the "in_trash" commit of the first entry and everything after
            raise OSError(5, "I/O error")
        real(batch, manifest)
    monkeypatch.setattr(trash, "_write_manifest", flaky)
    result = trash.execute(plan)
    assert not result["moved"]
    monkeypatch.setattr(trash, "_write_manifest", real)
    batch_id = result["batch_id"]
    trash.restore(batch_id)                    # recovery locates the moved object from the intent
    assert fixtures_digest(populated.claude) == before


def test_partial_restore_resumes_and_blocks_purge(populated):
    session = catalog().get("claude", CLAUDE_ID)
    result = trash.execute(trash.build_plan([session], Activity()))
    folder = Path(session.storage_path).with_suffix("")
    folder.mkdir()                             # occupy one original location
    out = trash.restore(result["batch_id"])
    assert out["failed"]
    status = trash.list_batches()[0]["items"][0]["status"]
    assert status == "partial"
    with pytest.raises(ValueError, match="Refusing to purge"):
        trash.purge(result["batch_id"])
    folder.rmdir()
    assert trash.restore(result["batch_id"])["restored"] == [session.selector]
    assert folder.exists() and Path(session.storage_path).exists()


def test_purge_refuses_unresolved_lost_objects(populated):
    session = catalog().get("claude", CLAUDE_ID)
    result = trash.execute(trash.build_plan([session], Activity()))
    stored = populated.cli_data / "trash" / result["batch_id"] / "0" / "0"
    stored.rename(populated.root / "elsewhere")  # object vanished from the trash
    with pytest.raises(ValueError, match="unresolved"):
        trash.purge(result["batch_id"])
    assert (populated.cli_data / "trash" / result["batch_id"]).exists()


# -- (3) fail closed on uninspectable processes / MCP state ----------------------

def test_missing_proc_is_incomplete(monkeypatch, tmp_path):
    monkeypatch.setattr(activity, "PROC", tmp_path / "noproc")
    snap = REAL_SNAPSHOT()
    assert not snap.complete and "/proc" in snap.incompleteness()


def test_empty_proc_is_incomplete(monkeypatch, tmp_path):
    (tmp_path / "proc").mkdir()
    monkeypatch.setattr(activity, "PROC", tmp_path / "proc")
    assert not REAL_SNAPSHOT().complete


def test_uninspectable_processes_block_deletion_unless_explicit(populated):
    session = catalog().get("claude", CLAUDE_ID)
    act = Activity(uninspectable=[(4242, "codex")])
    plan = trash.build_plan([session], act)
    assert not plan.items[0].allowed and "4242 (codex)" in plan.items[0].blockers[0]
    risky = trash.build_plan([session], act, allow_uninspectable=True)
    assert risky.items[0].allowed and "HIGH RISK" in risky.items[0].warnings[0]
    assert risky.plan_id != trash.build_plan([session], Activity()).plan_id


def test_execute_refuses_when_inspection_becomes_incomplete(populated, monkeypatch):
    session = catalog().get("claude", CLAUDE_ID)
    plan = trash.build_plan([session], Activity())
    calls = []

    def snap():
        calls.append(1)  # replan sees a clean state; the per-session check right before moving does not
        return Activity() if len(calls) == 1 else Activity(problems=["/proc/locks unreadable"])
    monkeypatch.setattr(activity, "snapshot", snap)
    result = trash.execute(plan)
    assert not result["moved"] and "inspected" in result["failed"][0]["error"]
    assert Path(session.storage_path).exists()


def test_unreadable_mcp_job_blocks_deletion(populated):
    jobs = populated.state / "jobs"
    jobs.mkdir(parents=True)
    (jobs / "1018-000001-aaaaaa.json").write_text("{not json")
    plan = trash.build_plan([catalog().get("claude", CLAUDE_ID)], Activity())
    assert any("unreadable" in b for b in plan.items[0].blockers)


def test_unattributed_running_job_blocks_same_agent(populated):
    jobs = populated.state / "jobs"
    jobs.mkdir(parents=True)
    (jobs / "1018-000001-aaaaaa.json").write_text(json.dumps(
        {"id": "1018-000001-aaaaaa", "kind": "agent", "agent": "claude", "status": "running"}))
    c = catalog()
    claude_plan = trash.build_plan([c.get("claude", CLAUDE_ID)], Activity())
    cursor_plan = trash.build_plan([c.get("cursor", CURSOR_ID)], Activity())
    assert any("no known session" in b for b in claude_plan.items[0].blockers)
    assert cursor_plan.items[0].allowed


def test_unlistable_jobs_dir_is_a_problem(populated):
    jobs = populated.state / "jobs"
    jobs.mkdir(parents=True)
    os.chmod(jobs, 0o000)
    try:
        from agentls.mcp import McpState
        state = McpState()
        state.jobs()
        assert state.problems
    finally:
        os.chmod(jobs, 0o700)


# -- (4) atomic no-replace rename fails closed ---------------------------------------

def test_rename_noreplace_unsupported_fails_closed(tmp_path, monkeypatch):
    import ctypes
    src, dst = tmp_path / "a", tmp_path / "b"
    src.write_text("x")
    monkeypatch.setattr(fsutil, "_renameat2", lambda: (None, ctypes))
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(fsutil.NoReplaceUnsupported):
            fsutil.rename_noreplace(fd, "a", fd, "b")
    finally:
        os.close(fd)
    assert src.exists() and not dst.exists()


def test_rename_noreplace_never_overwrites(tmp_path):
    (tmp_path / "a").write_text("new")
    (tmp_path / "b").write_text("keep")
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(FileExistsError):
            fsutil.rename_noreplace(fd, "a", fd, "b")
    finally:
        os.close(fd)
    assert (tmp_path / "b").read_text() == "keep" and (tmp_path / "a").exists()


def test_trash_without_atomic_rename_moves_nothing(populated, monkeypatch):
    import ctypes
    before = fixtures_digest(populated.claude)
    plan = trash.build_plan([catalog().get("claude", CLAUDE_ID)], Activity())
    monkeypatch.setattr(fsutil, "_renameat2", lambda: (None, ctypes))
    result = trash.execute(plan)
    assert not result["moved"] and "renameat2" in result["failed"][0]["error"]
    assert fixtures_digest(populated.claude) == before


# -- (7) bounded, non-blocking reads; per-session isolation ----------------------------

def run_with_timeout(fn, seconds=15):
    out = {}

    def target():
        try:
            out["value"] = fn()
        except BaseException as error:  # re-raised in the test thread
            out["error"] = error
    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(seconds)
    assert not thread.is_alive(), "hung"
    if "error" in out:
        raise out["error"]
    return out["value"]


def test_fifos_named_like_sessions_do_not_hang(env):
    fixtures.claude_session(env.claude, CLAUDE_ID, env.project)
    project = next((env.claude / "projects").iterdir())
    os.mkfifo(project / "99999999-2222-4333-8444-555555555555.jsonl")
    rollouts = env.codex / "sessions/2026/01/01"
    rollouts.mkdir(parents=True)
    os.mkfifo(rollouts / f"rollout-2026-01-01T00-00-00-{CODEX_ID}.jsonl")
    chat = env.cursor / "chats" / ("0" * 32) / CURSOR_ID
    chat.mkdir(parents=True)
    os.mkfifo(chat / "store.db")
    os.mkfifo(env.codex / "session_index.jsonl")
    (env.claude / "sessions").mkdir()
    os.mkfifo(env.claude / "sessions" / "1.json")
    result = run_with_timeout(lambda: Scanner().scan())
    assert [s.session_id for s in result.sessions] == [CLAUDE_ID] and not result.errors
    snap = run_with_timeout(REAL_SNAPSHOT)
    assert any("registry entry" in p for p in snap.problems)  # unreadable entry = incomplete, not ignored


def test_history_reader_rejects_fifo_and_symlink(tmp_path):
    from agentls.scanners.common import iter_jsonl
    os.mkfifo(tmp_path / "f.jsonl")
    with pytest.raises(fsutil.UnsafePath):
        run_with_timeout(lambda: list(iter_jsonl(tmp_path / "f.jsonl")))
    (tmp_path / "real.jsonl").write_text('{"a": 1}\n')
    os.symlink(tmp_path / "real.jsonl", tmp_path / "link.jsonl")
    with pytest.raises(OSError):
        list(iter_jsonl(tmp_path / "link.jsonl"))


def test_reads_are_bounded(tmp_path):
    from agentls.scanners import common
    path = tmp_path / "big.jsonl"
    with open(path, "wb") as stream:
        stream.write(b'{"x": "' + b"a" * (common.MAX_LINE + 10) + b'"}\n')
        stream.write(b'{"ok": 1}\n' * 10)
    rows = list(common.iter_jsonl(path))
    assert rows == [{"ok": 1}] * 10           # the oversized record is skipped, not buffered
    assert len(list(common.iter_jsonl(path, max_bytes=common.MAX_LINE // 2))) == 0
    assert len(list(common.iter_jsonl(path, max_lines=3))) <= 3


def test_one_broken_session_does_not_hide_its_agent(env, monkeypatch):
    fixtures.claude_session(env.claude, CLAUDE_ID, env.project)
    other = "33333333-2222-4333-8444-555555555555"
    fixtures.claude_session(env.claude, other, env.project, sidecar=False)
    from agentls.scanners import claude
    real = claude.parse
    monkeypatch.setattr(claude, "parse", lambda path, root, info: (
        (_ for _ in ()).throw(RuntimeError("hostile")) if path.stem == other else real(path, root, info)))
    result = Scanner().scan()
    rows = {s.session_id: s for s in result.sessions}
    assert not result.errors and rows[CLAUDE_ID].error is None
    assert "hostile" in rows[other].error and not rows[other].resumable


def test_codex_lost_archive_resolution(populated):
    """An interrupted `codex archive` is resolved from the rollout's location."""
    item = {"method": "codex-archive", "codex_state": "archiving",
            "entries": [{"path": str(populated.root / "gone.jsonl")}]}
    trash._resolve_codex(item)
    assert item["codex_state"] == "archived" and trash._item_status(item) == "trashed"
    item = {"method": "codex-archive", "codex_state": "archiving",
            "entries": [{"path": catalog().get("codex", CODEX_ID).storage_path}]}
    trash._resolve_codex(item)
    assert item["codex_state"] == "not_archived" and trash._item_status(item) == "failed"
