"""Installer transaction: preflight, lock, journaled launchers, venv rollback, faults."""
import fcntl
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ai_bridge_cli import installer

MAIN = b"#!/bin/sh\n# pretend ai-bridge server launcher\n"
SRC = str(Path(__file__).parents[1] / "src")


@pytest.fixture
def home(tmp_path, monkeypatch):
    (tmp_path / ".local/bin").mkdir(parents=True)
    os.chmod(tmp_path / ".local/bin", 0o755)
    target = tmp_path / ".local/bin/ai-bridge"
    target.write_bytes(MAIN)
    os.chmod(target, 0o755)
    digest = hashlib.sha256(MAIN).hexdigest()
    rec = tmp_path / ".local/share/ai-bridge/.cli-launcher.json"
    rec.parent.mkdir(parents=True)
    rec.write_text(json.dumps({"schema": 1, "uid": os.geteuid(), "path": str(target), "sha256": digest}))
    os.chmod(rec, 0o600)
    monkeypatch.setattr(installer, "KNOWN_MAIN_LAUNCHERS", {digest})
    return tmp_path


def layout(home):
    return installer.Layout(home)


def record(home):
    path = home / ".local/share/ai-bridge-cli/launchers.json"
    return json.loads(path.read_text()) if path.exists() else {"launchers": {}, "pending": {}}


def fault_at(point, nth=1):
    seen = []

    def fault(p):
        if p == point:
            seen.append(p)
            if len(seen) == nth:
                raise RuntimeError(f"injected at {p}")
    return fault


def stage(home, marker="new"):
    data = home / ".local/share/ai-bridge-cli"
    data.mkdir(parents=True, exist_ok=True)
    os.chmod(data, 0o700)
    path = data / f"venv.staging-{marker}"
    (path / "bin").mkdir(parents=True)
    python = path / "bin/python"
    python.write_text(f"#!/bin/sh\nexec {sys.executable} \"$@\"\n")
    python.chmod(0o755)
    (path / "MARKER").write_text(marker)
    return path


@pytest.mark.parametrize("point,nth", [("before_publish", 1), ("before_publish", 2),
                                       ("after_publish", 1), ("after_publish", 2)])
def test_launcher_failures_roll_back_completely(home, monkeypatch, point, nth):
    monkeypatch.setattr(installer, "_fault", fault_at(point, nth))
    with pytest.raises(RuntimeError):
        installer.install(layout(home), Path(sys.executable), os.geteuid())
    assert (home / ".local/bin/ai-bridge").read_bytes() == MAIN
    assert not (home / ".local/bin/ai-bridge-cli").exists()
    rec = record(home)
    assert rec["launchers"] == {} and rec["pending"] == {}
    assert installer.status(layout(home), os.geteuid())["ai-bridge"]["state"] == "main"


CRASH = r"""
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from ai_bridge_cli import installer
installer.KNOWN_MAIN_LAUNCHERS = {sys.argv[3]}
def fault(p):
    if p == sys.argv[4]:
        os._exit(9)
installer._fault = fault
installer.install(installer.Layout(Path(sys.argv[2])), Path(sys.executable), os.geteuid())
"""


@pytest.mark.parametrize("point", ["before_publish", "after_publish"])
def test_crash_between_journal_and_commit_is_recovered(home, point):
    digest = hashlib.sha256(MAIN).hexdigest()
    proc = subprocess.run([sys.executable, "-c", CRASH, SRC, str(home), digest, point], capture_output=True,
                          text=True)
    assert proc.returncode == 9, proc.stderr
    assert record(home)["pending"]                      # journal survived the crash
    report = installer.install(layout(home), Path(sys.executable), os.geteuid())  # rerun completes
    rec = record(home)
    assert rec["pending"] == {} and len(rec["launchers"]) == 2
    assert installer.MARKER.encode() in (home / ".local/bin/ai-bridge-cli").read_bytes()
    assert str(home / ".local/bin/ai-bridge-cli") in report["installed"] + report["unchanged"]
    assert installer.rollback(layout(home), os.geteuid())["restored"] == str(home / ".local/bin/ai-bridge")
    assert (home / ".local/bin/ai-bridge").read_bytes() == MAIN  # original server bytes, from the backup


def test_interrupted_rollback_never_marks_server_launcher_ours(home):
    installer.install(layout(home), Path(sys.executable), os.geteuid())
    rec = record(home)
    target = str(home / ".local/bin/ai-bridge")
    # Simulate: rollback journaled and published the server bytes, then crashed.
    (home / ".local/bin/ai-bridge").write_bytes(MAIN)
    rec["pending"][target] = {"new": hashlib.sha256(MAIN).hexdigest(), "old": rec["launchers"][target],
                              "restore": True}
    installer.save_record(layout(home), rec)
    assert installer.status(layout(home), os.geteuid())["ai-bridge"]["state"] == "main"
    report = installer.uninstall(layout(home), os.geteuid())
    assert (home / ".local/bin/ai-bridge").read_bytes() == MAIN    # not removed as "ours"
    assert str(home / ".local/bin/ai-bridge-cli") in report["removed"]


def test_activate_swaps_venv_and_installs(home):
    data = home / ".local/share/ai-bridge-cli"
    old = stage(home, "old")
    old.rename(data / "venv")
    report = installer.activate(layout(home), stage(home, "new"), os.geteuid())
    assert (data / "venv/MARKER").read_text() == "new" and (data / "venv.previous/MARKER").read_text() == "old"
    assert report["replaced_server_launcher"] and str(data / "venv/bin/python") in \
        (home / ".local/bin/ai-bridge-cli").read_text()
    result = installer.rollback(layout(home), os.geteuid(), venv=True)
    assert result["restored"] and (data / "venv/MARKER").read_text() == "old"
    assert (home / ".local/bin/ai-bridge").read_bytes() == MAIN


@pytest.mark.parametrize("point", ["after_venv_swap", "before_publish", "after_publish"])
def test_activate_failure_restores_previous_venv_and_launchers(home, monkeypatch, point):
    data = home / ".local/share/ai-bridge-cli"
    stage(home, "old").rename(data / "venv")
    stage(home, "older").rename(data / "venv.previous")
    monkeypatch.setattr(installer, "_fault", fault_at(point))
    with pytest.raises(RuntimeError):
        installer.activate(layout(home), stage(home, "new"), os.geteuid())
    assert (data / "venv/MARKER").read_text() == "old"
    assert (data / "venv.previous/MARKER").read_text() == "older"
    assert not list(data.glob("venv.failed-*")) and not list(data.glob("venv.discard-*"))
    assert (home / ".local/bin/ai-bridge").read_bytes() == MAIN
    assert not (home / ".local/bin/ai-bridge-cli").exists()


def test_activate_preflight_refuses_before_touching_anything(home):
    foreign = home / ".local/bin/ai-bridge-cli"
    foreign.write_text("#!/bin/sh\necho someone else\n")
    staged = stage(home)
    with pytest.raises(installer.Refusal, match="not ours"):
        installer.activate(layout(home), staged, os.geteuid())
    assert staged.exists() and not (home / ".local/share/ai-bridge-cli/venv").exists()
    assert foreign.read_text().endswith("someone else\n")


def test_activate_refuses_broken_stage(home):
    staged = stage(home)
    (staged / "bin/python").write_text("#!/bin/sh\nexit 3\n")
    with pytest.raises(installer.Refusal):
        installer.activate(layout(home), staged, os.geteuid())
    assert not (home / ".local/share/ai-bridge-cli/venv").exists()


def test_concurrent_install_is_refused(home):
    staged = stage(home)
    lock = home / ".local/share/ai-bridge-cli/.install.lock"
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        with pytest.raises(installer.Refusal, match="Another"):
            installer.activate(layout(home), staged, os.geteuid())
        with pytest.raises(installer.Refusal, match="Another"):
            installer.rollback(layout(home), os.geteuid())
    finally:
        os.close(fd)


def test_stage_must_be_ours(home, tmp_path):
    with pytest.raises(installer.Refusal):
        installer.activate(layout(home), tmp_path, os.geteuid())


def test_preflight_and_dry_run_write_nothing(home):
    before = sorted(str(p) for p in home.rglob("*"))
    report, _ = installer.plan_launchers(layout(home), home / "x/bin/python", os.geteuid())
    assert report["replaced_server_launcher"]
    assert sorted(str(p) for p in home.rglob("*")) == before


def test_repeat_venv_rollback_is_noop(home):
    data = layout(home).data
    stage(home, "old").rename(data / "venv")
    installer.activate(layout(home), stage(home), os.geteuid())
    installer.rollback(layout(home), os.geteuid(), venv=True)
    before = sorted(str(p.relative_to(home)) for p in home.rglob("*"))
    for dry_run in (True, False, False):
        result = installer.rollback(layout(home), os.geteuid(), dry_run=dry_run, venv=True)
        assert not result.get("restored_venv") and not result.get("would_restore_venv")
        assert (data / "venv/MARKER").read_text() == "old"
    assert sorted(str(p.relative_to(home)) for p in home.rglob("*")) == before


@pytest.mark.parametrize("point", [f"{when}_venv_rename_{i}" for when in ("before", "after") for i in range(3)])
def test_each_rename_failure_is_restored_and_retryable(home, monkeypatch, point):
    data = layout(home).data
    stage(home, "old").rename(data / "venv")
    stage(home, "older").rename(data / "venv.previous")
    staged = stage(home)
    monkeypatch.setattr(installer, "_fault", fault_at(point))
    with pytest.raises(RuntimeError):
        installer.activate(layout(home), staged, os.geteuid())
    assert (data / "venv/MARKER").read_text() == "old"
    assert (data / "venv.previous/MARKER").read_text() == "older"
    assert (staged / "MARKER").read_text() == "new"
    monkeypatch.setattr(installer, "_fault", lambda p: None)
    installer.activate(layout(home), staged, os.geteuid())
    assert (data / "venv/MARKER").read_text() == "new"
    assert (data / "venv.previous/MARKER").read_text() == "old"


ACTIVATE_CRASH = r"""
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from ai_bridge_cli import installer
installer.KNOWN_MAIN_LAUNCHERS = {sys.argv[3]}
installer._run_version = lambda p: None
def fault(p):
    if p == sys.argv[4]:
        os._exit(9)
installer._fault = fault
layout = installer.Layout(Path(sys.argv[2]))
if sys.argv[5] == "rollback":
    installer.rollback(layout, os.geteuid(), venv=True)
else:
    installer.activate(layout, layout.data / "venv.staging-new", os.geteuid())
"""


def crash_venv(home, point, operation="activate"):
    proc = subprocess.run([sys.executable, "-c", ACTIVATE_CRASH, SRC, str(home),
                           hashlib.sha256(MAIN).hexdigest(), point, operation], capture_output=True, text=True)
    assert proc.returncode == 9, proc.stderr


@pytest.mark.parametrize("point", [f"after_venv_rename_{i}" for i in range(3)] +
                         ["after_venv_swap", "before_publish", "after_publish"])
def test_killed_activation_recovers_before_retry(home, monkeypatch, point):
    data = layout(home).data
    stage(home, "old").rename(data / "venv")
    stage(home, "older").rename(data / "venv.previous")
    staged = stage(home)
    crash_venv(home, point)
    assert layout(home).venv_journal.exists()
    # Recovery itself can be interrupted and restarted.
    monkeypatch.setattr(installer, "_fault", fault_at("after_venv_recovery"))
    with pytest.raises(RuntimeError):
        installer.activate(layout(home), staged, os.geteuid())
    monkeypatch.setattr(installer, "_fault", lambda p: None)
    installer.activate(layout(home), staged, os.geteuid())
    assert (data / "venv/MARKER").read_text() == "new"
    assert (data / "venv.previous/MARKER").read_text() == "old"
    assert not layout(home).venv_journal.exists()


@pytest.mark.parametrize("point", ["after_venv_rename_0", "after_venv_rename_1", "after_venv_swap"])
def test_killed_rollback_is_retryable_and_idempotent(home, point):
    data = layout(home).data
    stage(home, "old").rename(data / "venv")
    installer.activate(layout(home), stage(home), os.geteuid())
    crash_venv(home, point, "rollback")
    for _ in range(2):
        installer.rollback(layout(home), os.geteuid(), venv=True)
        assert (data / "venv/MARKER").read_text() == "old"
        assert not (data / "venv.previous").exists()
    assert (home / ".local/bin/ai-bridge").read_bytes() == MAIN


def test_legacy_missing_active_preserves_last_good_venv(home, monkeypatch):
    data = layout(home).data
    stage(home, "old").rename(data / "venv.previous")
    monkeypatch.setattr(installer, "_fault", fault_at("after_venv_swap"))
    with pytest.raises(RuntimeError):
        installer.activate(layout(home), stage(home), os.geteuid())
    assert (data / "venv/MARKER").read_text() == "old"
    monkeypatch.setattr(installer, "_fault", lambda p: None)
    installer.activate(layout(home), data / "venv.staging-new", os.geteuid())
    assert (data / "venv.previous/MARKER").read_text() == "old"


@pytest.mark.parametrize("slot", ["venv", "venv.previous", "venv.staging-new"])
@pytest.mark.parametrize("dangling", [True, False])
def test_venv_symlinks_are_refused_without_touching_target(home, slot, dangling):
    staged = stage(home)
    target = layout(home).data / "external"
    if not dangling:
        target.mkdir()
        (target / "KEEP").write_text("untouched")
    path = layout(home).data / slot
    if path == staged:
        staged.rename(layout(home).data / "saved-stage")
    path.symlink_to(target, target_is_directory=True)
    with pytest.raises(installer.Refusal, match="real directory"):
        installer.activate(layout(home), staged, os.geteuid())
    if slot != "venv.staging-new":
        with pytest.raises(installer.Refusal, match="real directory"):
            installer.rollback(layout(home), os.geteuid(), venv=True)
    assert path.is_symlink()
    if not dangling:
        assert (target / "KEEP").read_text() == "untouched"


@pytest.mark.parametrize("rename_index", range(3))
def test_rename_syscall_failure_preserves_environments(home, monkeypatch, rename_index):
    data = layout(home).data
    stage(home, "old").rename(data / "venv")
    stage(home, "older").rename(data / "venv.previous")
    staged = stage(home)
    original = Path.rename
    calls = 0

    def rename(source, target):
        nonlocal calls
        calls += 1
        if calls == rename_index + 1:
            raise OSError("injected rename failure")
        return original(source, target)

    monkeypatch.setattr(Path, "rename", rename)
    with pytest.raises(OSError, match="injected rename"):
        installer.activate(layout(home), staged, os.geteuid())
    assert (data / "venv/MARKER").read_text() == "old"
    assert (data / "venv.previous/MARKER").read_text() == "older"
    installer.activate(layout(home), staged, os.geteuid())
    assert (data / "venv.previous/MARKER").read_text() == "old"


def test_killed_committed_activation_keeps_new_environment(home):
    data = layout(home).data
    stage(home, "old").rename(data / "venv")
    stage(home)
    crash_venv(home, "after_venv_commit")
    with installer.install_lock(layout(home)):
        installer._recover_venv(layout(home), os.geteuid())
    assert (data / "venv/MARKER").read_text() == "new"
    assert (data / "venv.previous/MARKER").read_text() == "old"
    assert not layout(home).venv_journal.exists()


def test_recovery_refuses_changed_paths_and_preserves_good_environment(home):
    data = layout(home).data
    stage(home, "old").rename(data / "venv")
    staged = stage(home)
    crash_venv(home, "after_venv_rename_0")
    unexpected = stage(home, "unexpected")
    unexpected.rename(data / "venv")
    with pytest.raises(installer.Refusal, match="paths changed"):
        installer.activate(layout(home), staged, os.geteuid())
    assert (data / "venv.previous/MARKER").read_text() == "old"
    assert (data / "venv/MARKER").read_text() == "unexpected"
    assert layout(home).venv_journal.exists()


@pytest.mark.parametrize("journal", [
    {"schema": 2, "moves": []},
    {"schema": 1, "moves": [{"source": "../venv", "target": "venv", "identity": [1, 2]}]},
    {"schema": 1, "moves": [{"source": "chats", "target": "venv", "identity": [1, 2]}]},
])
def test_invalid_venv_journal_refuses_without_changes(home, journal):
    current = layout(home)
    stage(home, "old").rename(current.venv)
    installer.write_atomic(current.venv_journal, json.dumps(journal).encode(), 0o600)
    with pytest.raises(installer.Refusal):
        installer.activate(current, stage(home), os.geteuid())
    assert (current.venv / "MARKER").read_text() == "old"
    assert current.venv_journal.exists()


def test_rollback_without_active_venv_and_without_server_backup(home):
    current = layout(home)
    stage(home, "old").rename(current.previous)
    result = installer.rollback(current, os.geteuid(), dry_run=True, venv=True)
    assert result["would_restore_venv"] == str(current.previous)
    assert current.previous.exists() and not current.venv.exists()
    installer.rollback(current, os.geteuid(), venv=True)
    installer.rollback(current, os.geteuid(), venv=True)
    assert (current.venv / "MARKER").read_text() == "old"
    assert not current.previous.exists()


@pytest.mark.parametrize("failed_sync", range(1, 6))
def test_directory_sync_failure_keeps_good_venvs_and_allows_retry(home, monkeypatch, failed_sync):
    current = layout(home)
    stage(home, "old").rename(current.venv)
    stage(home, "older").rename(current.previous)
    staged = stage(home)
    original = installer._fsync_dir
    calls = 0

    def sync(path):
        nonlocal calls
        calls += 1
        if calls == failed_sync:
            raise OSError("injected directory sync failure")
        return original(path)

    monkeypatch.setattr(installer, "_fsync_dir", sync)
    with pytest.raises(OSError, match="directory sync"):
        installer.activate(current, staged, os.geteuid())
    # Even when recovery is deferred, neither good environment is deleted.
    markers = {p.read_text() for p in current.data.glob("*/MARKER")}
    assert {"old", "older"} <= markers
    installer.activate(current, staged, os.geteuid())
    assert (current.venv / "MARKER").read_text() == "new"
    assert (current.previous / "MARKER").read_text() == "old"


def test_activation_refuses_existing_retirement_destination(home, monkeypatch):
    current = layout(home)
    stage(home, "old").rename(current.venv)
    stage(home, "older").rename(current.previous)
    stage(home, "unrelated").rename(current.data / "venv.discard-collision")
    monkeypatch.setattr(installer.secrets, "token_hex", lambda size: "collision")
    with pytest.raises(installer.Refusal, match="destination exists"):
        installer.activate(current, stage(home), os.geteuid())
    assert (current.venv / "MARKER").read_text() == "old"
    assert (current.previous / "MARKER").read_text() == "older"
    assert (current.data / "venv.discard-collision/MARKER").read_text() == "unrelated"
