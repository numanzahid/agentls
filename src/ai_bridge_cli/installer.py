"""Launcher management for install.sh (stdlib only; runnable as a plain script).

Installs ~/.local/bin/ai-bridge-cli and ~/.local/bin/ai-bridge as tiny shell
launchers into this tool's own virtualenv. Ownership is recorded in
$DATA/launchers.json (path -> sha256). An existing ~/.local/bin/ai-bridge is
replaced only if it is:
  * a launcher this tool installed (hash matches our record), or
  * the ai-bridge server's launcher: its own ownership record
    (~/.local/share/ai-bridge/.cli-launcher.json) must match path, uid and
    sha256, AND the hash must be a known server launcher (or
    --accept-main-record is given). The original is backed up first and can
    be restored with `install.sh --rollback`. The server's record is left
    untouched, so after a rollback it matches again.
Anything else is left alone and reported.

Transactions: every mutating command holds an exclusive lock
($DATA/.install.lock). `activate` runs a read-only preflight, swaps the staged
virtualenv in (keeping the previous one), verifies it, then publishes the
launchers. Each launcher change is journaled in the ownership record
("pending") before it is published, so an interrupted run is recognised as our
own on the next run. Any failure restores the previous launchers and venv.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import shutil
import subprocess
import grp
import hashlib
import json
import os
import pwd
import secrets
import shlex
import stat
import sys
import tempfile
import time
from pathlib import Path

# sha256 of launchers shipped by the ai-bridge server (`ai-bridge` script).
KNOWN_MAIN_LAUNCHERS = {
    "f446464d96edd768a61934b298c2e2c28e49f4e812e56ad1816aa2bd64c86768",
}
MARKER = "# ai-bridge-cli launcher"
MAX_LAUNCHER = 16384
NAMES = ("ai-bridge-cli", "ai-bridge")


class Refusal(ValueError):
    pass


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def private_primary_group(owner: int, gid: int) -> bool:
    try:
        account, group = pwd.getpwuid(owner), grp.getgrgid(gid)
        accounts, groups = pwd.getpwall(), grp.getgrall()
        if account.pw_gid != gid or group.gr_gid != gid:
            return False
        matching = [g for g in groups if g.gr_gid == gid]
        return (all(a.pw_name == account.pw_name for a in accounts if a.pw_gid == gid) and
                all(name == account.pw_name for g in [group, *matching] for name in g.gr_mem))
    except (KeyError, OSError):
        return False


def safe_dir(path: Path, uid: int) -> None:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise Refusal(f"{path} must be a real directory (not a symlink).")
    if info.st_uid != uid or info.st_mode & 0o002:
        raise Refusal(f"{path} must be owned by you and not world-writable.")
    if info.st_mode & 0o020 and not private_primary_group(uid, info.st_gid):
        raise Refusal(f"{path} is writable by a shared group.")


def read_regular(path: Path, uid: int, limit: int = MAX_LAUNCHER) -> tuple[bytes, os.stat_result]:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != uid or info.st_nlink != 1:
            raise Refusal(f"{path} is not a single-link regular file owned by you.")
        if info.st_mode & 0o022:
            raise Refusal(f"{path} is writable by group/others.")
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise Refusal(f"{path} is unexpectedly large.")
    return data, info


class Layout:
    def __init__(self, home: Path, data_dir: Path | None = None):
        self.home = Path(home).resolve(strict=True)
        self.bin = self.home / ".local/bin"
        self.data = Path(data_dir or self.home / ".local/share/ai-bridge-cli").absolute()
        self.record = self.data / "launchers.json"
        self.backups = self.data / "backups"
        self.main_record = self.home / ".local/share/ai-bridge/.cli-launcher.json"
        self.venv = self.data / "venv"
        self.previous = self.data / "venv.previous"
        self.lock = self.data / ".install.lock"
        self.venv_journal = self.data / "venv-transaction.json"


def launcher_bytes(python: Path, prog: str) -> bytes:
    return (f"#!/bin/sh\n{MARKER} ({prog}); managed by ai-bridge-cli install.sh - do not edit.\n"
            f"AI_BRIDGE_CLI_PROG={prog}\nexport AI_BRIDGE_CLI_PROG\n"
            f"exec {shlex.quote(str(python))} -I -m ai_bridge_cli \"$@\"\n").encode()


def _fault(point: str) -> None:
    """Fault-injection hook for tests (no-op in normal operation)."""


@contextlib.contextmanager
def install_lock(layout: Layout):
    layout.data.mkdir(mode=0o700, parents=True, exist_ok=True)
    safe_dir(layout.data, os.geteuid())
    fd = os.open(layout.lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refusal("Another ai-bridge-cli install/rollback is running; nothing was changed.") from None
        yield
    finally:
        os.close(fd)


def load_record(layout: Layout, uid: int) -> dict:
    if not layout.record.exists() and not layout.record.is_symlink():
        return {"schema": 1, "uid": uid, "launchers": {}, "pending": {}}
    data, info = read_regular(layout.record, uid, 65536)
    if info.st_mode & 0o077:
        raise Refusal(f"{layout.record} must be private (0600).")
    value = json.loads(data)
    if value.get("schema") != 1 or value.get("uid") != uid or not isinstance(value.get("launchers"), dict):
        raise Refusal(f"{layout.record} does not match this account.")
    value.setdefault("pending", {})
    if not isinstance(value["pending"], dict):
        raise Refusal(f"{layout.record} has a corrupt pending journal.")
    return value


def save_record(layout: Layout, value: dict) -> None:
    write_atomic(layout.record, (json.dumps(value, indent=1) + "\n").encode(), 0o600)


def write_atomic(path: Path, data: bytes, mode: int) -> None:
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def classify(layout: Layout, target: Path, uid: int, record: dict, accept_main_record: bool) -> dict:
    """What is currently at target: absent | ours | main | foreign (with reason)."""
    if not target.exists() and not target.is_symlink():
        return {"state": "absent"}
    if target.is_symlink():
        return {"state": "foreign", "reason": "is a symlink"}
    try:
        data, info = read_regular(target, uid)
    except (Refusal, OSError) as error:
        return {"state": "foreign", "reason": str(error)}
    digest = sha256(data)
    identity = (info.st_dev, info.st_ino, digest)
    pending = record.get("pending", {}).get(str(target)) or {}
    # A journaled-but-uncommitted publish of OUR launcher (interrupted run) is still ours;
    # an interrupted rollback journals the server's bytes, which must never count as ours.
    ours_pending = pending.get("new") if not pending.get("restore") else None
    if digest in (record["launchers"].get(str(target)), ours_pending):
        return {"state": "ours", "sha256": digest, "identity": identity, "data": data}
    if target.name == "ai-bridge":
        try:
            main_data, main_info = read_regular(layout.main_record, uid, 8192)
            main = json.loads(main_data)
        except FileNotFoundError:
            main = None
        except (Refusal, OSError, ValueError) as error:
            return {"state": "foreign", "reason": f"server launcher record unusable: {error}"}
        if main is not None:
            if main_info.st_mode & 0o077:
                return {"state": "foreign", "reason": "server launcher record is not private"}
            if (main.get("schema"), main.get("uid"), main.get("path"), main.get("sha256")) != (
                    1, uid, str(target), digest):
                return {"state": "foreign", "reason": "server launcher record does not match this file"}
            if digest not in KNOWN_MAIN_LAUNCHERS and not accept_main_record:
                return {"state": "foreign", "reason": "server-registered launcher with an unknown hash "
                                                      "(re-run with --accept-main-record to trust its record)"}
            return {"state": "main", "sha256": digest, "identity": identity, "data": data, "main_record": main}
    return {"state": "foreign", "reason": "not installed by ai-bridge-cli or a verified ai-bridge server launcher"}


def _publish(target: Path, data: bytes, expected: tuple | None) -> tuple:
    """Create (never overwriting) or replace a verified file atomically; returns new identity."""
    fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fchmod(stream.fileno(), 0o755)
            os.fsync(stream.fileno())
        if expected is None:
            os.link(tmp, target)  # fails if something appeared meanwhile
        else:
            info = os.lstat(target)
            current = sha256(target.read_bytes()) if stat.S_ISREG(info.st_mode) else None
            if (info.st_dev, info.st_ino, current) != expected:
                raise Refusal(f"{target} changed during installation; nothing replaced.")
            os.replace(tmp, target)
        _fsync_dir(target.parent)
        info = os.lstat(target)
        return (info.st_dev, info.st_ino, sha256(data))
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _unpublish(target: Path, expected: tuple) -> None:
    info = os.lstat(target)
    if (info.st_dev, info.st_ino, sha256(target.read_bytes())) != expected:
        raise Refusal(f"{target} changed; not removing it.")
    target.unlink()
    _fsync_dir(target.parent)


def backup(layout: Layout, target: Path, state: dict) -> Path:
    layout.backups.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    folder = layout.backups / f"{stamp}-{secrets.token_hex(3)}-{target.name}"  # sorts by time
    folder.mkdir(mode=0o700)
    write_atomic(folder / target.name, state["data"], 0o700)
    meta = {"schema": 1, "original_path": str(target), "sha256": state["sha256"],
            "source": "ai-bridge server launcher", "main_record": state.get("main_record"),
            "replaced_at": time.time()}
    write_atomic(folder / "backup.json", (json.dumps(meta, indent=1) + "\n").encode(), 0o600)
    return folder


def reconcile(layout: Layout, uid: int) -> dict:
    """Finish or drop journal entries left by an interrupted run (caller holds the lock)."""
    record = load_record(layout, uid)
    if not record["pending"]:
        return record
    for key, pending in list(record["pending"].items()):
        try:
            digest = sha256(read_regular(Path(key), uid)[0])
        except (Refusal, OSError):
            digest = None
        if digest is not None and digest == pending.get("new"):
            if pending.get("restore"):
                record["launchers"].pop(key, None)   # server launcher is back
            else:
                record["launchers"][key] = digest    # our publish landed
        record["pending"].pop(key)                   # otherwise it never landed
    save_record(layout, record)
    return record


def plan_launchers(layout: Layout, python: Path, uid: int, *, replace_ai_bridge: bool = True,
                   accept_main_record: bool = False) -> tuple[dict, list]:
    """Read-only: what install would do. Raises Refusal if ai-bridge-cli cannot be installed."""
    if layout.bin.exists() or layout.bin.is_symlink():
        safe_dir(layout.bin, uid)
    record = load_record(layout, uid)
    report = {"installed": [], "updated": [], "replaced_server_launcher": [], "unchanged": [],
              "skipped": [], "backups": []}
    actions = []
    for name in NAMES:
        target = layout.bin / name
        data = launcher_bytes(python, name)
        state = classify(layout, target, uid, record, accept_main_record)
        if state["state"] == "ours" and state["sha256"] == sha256(data):
            report["unchanged"].append(str(target))
            continue
        if state["state"] == "foreign":
            if name == "ai-bridge-cli":
                raise Refusal(f"{target} exists and is not ours ({state['reason']}); nothing was changed.")
            report["skipped"].append({"path": str(target), "reason": state["reason"]})
            continue
        if state["state"] == "main" and not replace_ai_bridge:
            report["skipped"].append({"path": str(target), "reason": "server launcher kept (--no-replace-ai-bridge)"})
            continue
        report[{"absent": "installed", "ours": "updated", "main": "replaced_server_launcher"}[state["state"]]].append(
            str(target))
        actions.append((target, data, state))
    return report, actions


def install(layout: Layout, python: Path, uid: int, *, replace_ai_bridge: bool = True,
            accept_main_record: bool = False, dry_run: bool = False) -> dict:
    """Publish launchers transactionally (caller holds the install lock unless dry_run)."""
    if not python.is_absolute() or (not dry_run and not os.access(python, os.X_OK)):
        raise Refusal(f"Virtualenv interpreter is not executable: {python}")
    if not dry_run:
        layout.data.mkdir(mode=0o700, parents=True, exist_ok=True)
        reconcile(layout, uid)
    report, actions = plan_launchers(layout, python, uid, replace_ai_bridge=replace_ai_bridge,
                                     accept_main_record=accept_main_record)
    report["dry_run"] = dry_run
    if dry_run or not actions:
        return report
    if not layout.bin.exists():
        layout.bin.mkdir(mode=0o755, parents=True)
        safe_dir(layout.bin, uid)
    record = load_record(layout, uid)
    done = []  # (target, new_identity, previous_state, previous_record_value)
    try:
        for target, data, state in actions:
            key = str(target)
            if state["state"] == "main":
                report["backups"].append(str(backup(layout, target, state)))
            previous_value = record["launchers"].get(key)
            record["pending"][key] = {"new": sha256(data), "old": state.get("sha256")}
            save_record(layout, record)   # journal before publishing
            _fault("before_publish")
            new_identity = _publish(target, data, state.get("identity"))
            done.append((target, new_identity, state, previous_value))
            _fault("after_publish")
            record["launchers"][key] = sha256(data)
            record["pending"].pop(key, None)
            save_record(layout, record)
    except BaseException:
        _undo_launchers(layout, uid, record, done)
        raise
    return report


def _undo_launchers(layout: Layout, uid: int, record: dict, done: list) -> None:
    for target, new_identity, state, previous_value in reversed(done):
        key = str(target)
        try:
            if state["state"] == "absent":
                _unpublish(target, new_identity)
            else:
                _publish(target, state["data"], new_identity)   # put the previous bytes back
            if previous_value is None:
                record["launchers"].pop(key, None)
            else:
                record["launchers"][key] = previous_value
        except Exception:
            continue  # keep pending: the next run still recognises our file
        record["pending"].pop(key, None)
    for key in [k for k in record["pending"] if not any(str(t) == k for t, *_ in done)]:
        record["pending"].pop(key, None)  # journaled but never published
    try:
        save_record(layout, record)
    except OSError:
        pass


def _run_version(python: Path) -> None:
    try:
        proc = subprocess.run([str(python), "-I", "-m", "ai_bridge_cli", "--version"], capture_output=True,
                              text=True, timeout=120, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as error:
        raise Refusal(f"{python} could not run ai_bridge_cli: {error}") from None
    if proc.returncode != 0 or not proc.stdout.strip():
        raise Refusal(f"{python} could not run ai_bridge_cli: {(proc.stderr or proc.stdout).strip()[-300:]}")


def _venv_slot(path: Path, uid: int) -> bool:
    if not path.exists() and not path.is_symlink():
        return False
    safe_dir(path, uid)
    return True


def _venv_identity(path: Path) -> list[int]:
    info = path.lstat()
    return [info.st_dev, info.st_ino]


def _recover_venv(layout: Layout, uid: int) -> None:
    """Undo an uncommitted rename plan; each undo is itself restartable."""
    if not layout.venv_journal.exists() and not layout.venv_journal.is_symlink():
        return
    journal = json.loads(read_regular(layout.venv_journal, uid, 65536)[0])
    if not isinstance(journal, dict) or journal.get("schema") != 1 or not isinstance(journal.get("moves"), list):
        raise Refusal("Invalid virtualenv transaction journal; preserve it for manual recovery.")
    moves = []
    for entry in journal["moves"]:
        if (not isinstance(entry, dict) or
                any(not isinstance(entry.get(key), str) or Path(entry[key]).name != entry[key]
                    for key in ("source", "target")) or
                not isinstance(entry.get("identity"), list) or len(entry["identity"]) != 2 or
                not all(isinstance(value, int) for value in entry["identity"])):
            raise Refusal("Invalid virtualenv rename entry; preserving directories.")
        source, target = (layout.data / entry[key] for key in ("source", "target"))
        if any(p.parent != layout.data or not (p.name == "venv" or p.name.startswith("venv."))
               for p in (source, target)):
            raise Refusal("Unsafe path in virtualenv transaction journal.")
        for path in (source, target):
            _venv_slot(path, uid)
        moves.append((source, target, entry["identity"]))
    if not journal.get("committed"):
        for source, target, identity in reversed(moves):
            if source.exists() and _venv_identity(source) == identity:
                continue  # rename never happened, or was already undone
            if not target.exists() or _venv_identity(target) != identity or source.exists():
                raise Refusal("Virtualenv transaction paths changed; preserving all directories for recovery.")
            target.rename(source)
            _fsync_dir(layout.data)
            _fault("after_venv_recovery")
    layout.venv_journal.unlink()
    _fsync_dir(layout.data)


@contextlib.contextmanager
def _venv_transaction(layout: Layout, uid: int, moves: list):
    journal = {"schema": 1, "moves": [
        {"source": source.name, "target": target.name, "identity": _venv_identity(source)}
        for source, target in moves]}
    # Nothing has moved until the complete undo plan is durable.
    write_atomic(layout.venv_journal, (json.dumps(journal) + "\n").encode(), 0o600)
    try:
        for index, (source, target) in enumerate(moves):
            if target.exists() or target.is_symlink():
                raise Refusal(f"Virtualenv rename destination exists: {target}")
            _fault(f"before_venv_rename_{index}")
            source.rename(target)
            _fsync_dir(layout.data)
            _fault(f"after_venv_rename_{index}")
        yield
        journal["committed"] = True
        write_atomic(layout.venv_journal, (json.dumps(journal) + "\n").encode(), 0o600)
    except BaseException:
        _recover_venv(layout, uid)
        raise
    _fault("after_venv_commit")
    _recover_venv(layout, uid)


def _prepare_venvs(layout: Layout, uid: int) -> None:
    for path in (layout.venv, layout.previous):
        _venv_slot(path, uid)
    _recover_venv(layout, uid)
    # Recover the legacy, unjournaled gap before a staged venv was promoted.
    # Never discard the last surviving environment on a retry.
    if not layout.venv.exists() and layout.previous.exists():
        layout.previous.rename(layout.venv)
        _fsync_dir(layout.data)


def activate(layout: Layout, stage: Path, uid: int, *, replace_ai_bridge: bool = True,
             accept_main_record: bool = False) -> dict:
    """Activate a staged venv with a durable, restartable undo plan."""
    stage = Path(stage).absolute()
    if stage.parent != layout.data or not stage.name.startswith("venv.staging-"):
        raise Refusal(f"Staging virtualenv must be {layout.data}/venv.staging-*.")
    with install_lock(layout):
        _prepare_venvs(layout, uid)
        safe_dir(stage, uid)
        future = layout.venv / "bin/python"
        plan_launchers(layout, future, uid, replace_ai_bridge=replace_ai_bridge,
                       accept_main_record=accept_main_record)
        _run_version(stage / "bin/python")
        discard = layout.data / f"venv.discard-{secrets.token_hex(8)}"
        had_venv, had_previous = layout.venv.exists(), layout.previous.exists()
        moves = []
        if had_previous:
            moves.append((layout.previous, discard))
        if had_venv:
            moves.append((layout.venv, layout.previous))
        moves.append((stage, layout.venv))
        with _venv_transaction(layout, uid, moves):
            _fault("after_venv_swap")
            _run_version(future)
            report = install(layout, future, uid, replace_ai_bridge=replace_ai_bridge,
                             accept_main_record=accept_main_record)
        if had_previous:
            shutil.rmtree(discard, ignore_errors=True)
        report["venv"] = str(layout.venv)
        report["previous_venv"] = str(layout.previous) if had_venv else None
        return report


def _latest_backup(layout: Layout, name: str) -> Path | None:
    if not layout.backups.is_dir():
        return None
    folders = sorted((p for p in layout.backups.iterdir() if p.name.endswith("-" + name) and p.is_dir()),
                     reverse=True)
    return folders[0] if folders else None


def rollback(layout: Layout, uid: int, dry_run: bool = False, venv: bool = False) -> dict:
    """Put the server's original ai-bridge launcher back (and optionally the previous venv)."""
    lock = contextlib.nullcontext() if dry_run else install_lock(layout)
    with lock:
        if venv:
            for path in (layout.venv, layout.previous):
                _venv_slot(path, uid)
            if not dry_run:
                _recover_venv(layout, uid)
        result = _rollback_launcher(layout, uid, dry_run)
        if venv and layout.previous.exists():
            if dry_run:
                result["would_restore_venv"] = str(layout.previous)
            else:
                # Consume the previous slot. Retain the displaced version separately;
                # a repeat rollback must never toggle between the two versions.
                retired = layout.data / f"venv.rolled-back-{secrets.token_hex(8)}"
                moves = [(layout.venv, retired)] if layout.venv.exists() else []
                moves.append((layout.previous, layout.venv))
                with _venv_transaction(layout, uid, moves):
                    _fault("after_venv_swap")
                result["restored_venv"] = str(layout.venv)
        return result


def _rollback_launcher(layout: Layout, uid: int, dry_run: bool) -> dict:
    record = load_record(layout, uid) if dry_run else reconcile(layout, uid)
    target = layout.bin / "ai-bridge"
    folder = _latest_backup(layout, "ai-bridge")
    if not folder:
        return {"restored": None, "note": "No backed-up server launcher; nothing to roll back."}
    meta = json.loads(read_regular(folder / "backup.json", uid, 65536)[0])
    data, _ = read_regular(folder / "ai-bridge", uid)
    if sha256(data) != meta.get("sha256") or meta.get("original_path") != str(target):
        raise Refusal(f"Backup {folder} failed verification; restore it manually.")
    state = classify(layout, target, uid, record, False)
    if state["state"] == "main" and state["sha256"] == meta["sha256"]:
        return {"restored": None, "note": "The server's launcher is already in place."}
    if state["state"] not in ("ours", "absent"):
        raise Refusal(f"{target} is no longer the ai-bridge-cli launcher ({state.get('reason', state['state'])}); "
                      "not overwriting it.")
    if dry_run:
        return {"would_restore": str(target), "from": str(folder)}
    record["pending"][str(target)] = {"new": meta["sha256"], "old": state.get("sha256"), "restore": True}
    save_record(layout, record)
    _publish(target, data, state.get("identity"))
    record["launchers"].pop(str(target), None)
    record["pending"].pop(str(target), None)
    save_record(layout, record)
    return {"restored": str(target), "from": str(folder), "sha256": meta["sha256"]}


def uninstall(layout: Layout, uid: int, dry_run: bool = False) -> dict:
    lock = contextlib.nullcontext() if dry_run else install_lock(layout)
    with lock:
        report = {"removed": [], "kept": [], "rollback": None, "removed_venvs": []}
        try:
            report["rollback"] = _rollback_launcher(layout, uid, dry_run)
        except Refusal as error:
            report["rollback"] = {"error": str(error)}
        record = load_record(layout, uid) if dry_run else reconcile(layout, uid)
        for name in NAMES:
            target = layout.bin / name
            state = classify(layout, target, uid, record, False)
            if state["state"] == "ours":
                if not dry_run:
                    _unpublish(target, state["identity"])
                    record["launchers"].pop(str(target), None)
                    record["pending"].pop(str(target), None)
                    save_record(layout, record)
                report["removed"].append(str(target))
            elif state["state"] != "absent":
                report["kept"].append({"path": str(target), "reason": state.get("reason") or state["state"]})
        if layout.data.is_dir():
            for path in sorted(layout.data.iterdir()):
                if (path.name == "venv" or path.name.startswith("venv.")) and path.is_dir() and not path.is_symlink():
                    if not dry_run:
                        shutil.rmtree(path)
                    report["removed_venvs"].append(str(path))
        return report


def status(layout: Layout, uid: int) -> dict:
    record = load_record(layout, uid)
    return {name: {k: v for k, v in classify(layout, layout.bin / name, uid, record, False).items()
                   if k in ("state", "reason", "sha256")} for name in NAMES}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="ai-bridge-cli installer")
    parser.add_argument("command", choices=("preflight", "install", "activate", "rollback", "uninstall", "status"))
    parser.add_argument("--venv-python", type=Path)
    parser.add_argument("--stage", type=Path)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--no-replace-ai-bridge", action="store_true")
    parser.add_argument("--accept-main-record", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    account = pwd.getpwuid(os.geteuid())
    if os.geteuid() == 0:
        print("ERROR: run as your normal user, not root.", file=sys.stderr)
        return 1
    if os.path.realpath(Path.home()) != os.path.realpath(account.pw_dir):
        print("ERROR: HOME differs from your passwd home directory.", file=sys.stderr)
        return 1
    layout = Layout(Path(account.pw_dir), args.data_dir)
    options = {"replace_ai_bridge": not args.no_replace_ai_bridge, "accept_main_record": args.accept_main_record}
    try:
        if args.command in ("preflight", "install"):
            python = args.venv_python or layout.venv / "bin/python"
            if args.command == "preflight" or args.dry_run:
                result, _ = plan_launchers(layout, python, account.pw_uid, **options)
                result["dry_run"] = True
            else:
                with install_lock(layout):
                    result = install(layout, python, account.pw_uid, **options)
        elif args.command == "activate":
            if not args.stage:
                raise Refusal("--stage is required.")
            result = activate(layout, args.stage, account.pw_uid, **options)
        elif args.command == "rollback":
            result = rollback(layout, account.pw_uid, args.dry_run, venv=True)
        elif args.command == "uninstall":
            result = uninstall(layout, account.pw_uid, args.dry_run)
        else:
            result = status(layout, account.pw_uid)
    except (Refusal, OSError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
