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
"""
from __future__ import annotations

import argparse
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
        self.home = Path(home).absolute()
        self.bin = self.home / ".local/bin"
        self.data = Path(data_dir or self.home / ".local/share/ai-bridge-cli").absolute()
        self.record = self.data / "launchers.json"
        self.backups = self.data / "backups"
        self.main_record = self.home / ".local/share/ai-bridge/.cli-launcher.json"


def launcher_bytes(python: Path, prog: str) -> bytes:
    return (f"#!/bin/sh\n{MARKER} ({prog}); managed by ai-bridge-cli install.sh - do not edit.\n"
            f"AI_BRIDGE_CLI_PROG={prog}\nexport AI_BRIDGE_CLI_PROG\n"
            f"exec {shlex.quote(str(python))} -I -m ai_bridge_cli \"$@\"\n").encode()


def load_record(layout: Layout, uid: int) -> dict:
    if not layout.record.exists() and not layout.record.is_symlink():
        return {"schema": 1, "uid": uid, "launchers": {}}
    data, info = read_regular(layout.record, uid, 65536)
    if info.st_mode & 0o077:
        raise Refusal(f"{layout.record} must be private (0600).")
    value = json.loads(data)
    if value.get("schema") != 1 or value.get("uid") != uid or not isinstance(value.get("launchers"), dict):
        raise Refusal(f"{layout.record} does not match this account.")
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
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


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
    if record["launchers"].get(str(target)) == digest:
        return {"state": "ours", "sha256": digest, "identity": identity}
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


def _publish(target: Path, data: bytes, expected: tuple | None) -> None:
    """Create (never overwriting) or replace a verified file atomically."""
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
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


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


def install(layout: Layout, python: Path, uid: int, *, replace_ai_bridge: bool = True,
            accept_main_record: bool = False, dry_run: bool = False) -> dict:
    if not python.is_absolute() or (not dry_run and not os.access(python, os.X_OK)):
        raise Refusal(f"Virtualenv interpreter is not executable: {python}")
    report = {"dry_run": dry_run, "installed": [], "updated": [], "replaced_server_launcher": [],
              "unchanged": [], "skipped": [], "backups": []}
    if not dry_run:
        layout.data.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not layout.bin.exists():
            layout.bin.mkdir(mode=0o755, parents=True)
    if layout.bin.exists():
        safe_dir(layout.bin, uid)
    record = load_record(layout, uid)
    for name in NAMES:
        target = layout.bin / name
        data = launcher_bytes(python, name)
        state = classify(layout, target, uid, record, accept_main_record)
        if state["state"] == "ours" and state["sha256"] == sha256(data):
            report["unchanged"].append(str(target))
            continue
        if state["state"] == "foreign":
            report["skipped"].append({"path": str(target), "reason": state["reason"]})
            continue
        if state["state"] == "main" and not replace_ai_bridge:
            report["skipped"].append({"path": str(target), "reason": "server launcher kept (--no-replace-ai-bridge)"})
            continue
        bucket = {"absent": "installed", "ours": "updated", "main": "replaced_server_launcher"}[state["state"]]
        report[bucket].append(str(target))
        if dry_run:
            continue
        if state["state"] == "main":
            report["backups"].append(str(backup(layout, target, state)))
        _publish(target, data, state.get("identity"))
        record["launchers"][str(target)] = sha256(data)
        save_record(layout, record)
    return report


def _latest_backup(layout: Layout, name: str) -> Path | None:
    if not layout.backups.is_dir():
        return None
    folders = sorted((p for p in layout.backups.iterdir() if p.name.endswith("-" + name) and p.is_dir()),
                     reverse=True)
    return folders[0] if folders else None


def rollback(layout: Layout, uid: int, dry_run: bool = False) -> dict:
    """Put the server's original ai-bridge launcher back (if we replaced it)."""
    record = load_record(layout, uid)
    target = layout.bin / "ai-bridge"
    folder = _latest_backup(layout, "ai-bridge")
    if not folder:
        return {"restored": None, "note": "No backed-up server launcher; nothing to roll back."}
    meta = json.loads(read_regular(folder / "backup.json", uid, 65536)[0])
    data, _ = read_regular(folder / "ai-bridge", uid)
    if sha256(data) != meta.get("sha256") or meta.get("original_path") != str(target):
        raise Refusal(f"Backup {folder} failed verification; restore it manually.")
    state = classify(layout, target, uid, record, False)
    if state["state"] not in ("ours", "absent"):
        raise Refusal(f"{target} is no longer the ai-bridge-cli launcher ({state.get('reason', state['state'])}); "
                      "not overwriting it.")
    if dry_run:
        return {"would_restore": str(target), "from": str(folder)}
    _publish(target, data, state.get("identity"))
    record["launchers"].pop(str(target), None)
    save_record(layout, record)
    return {"restored": str(target), "from": str(folder), "sha256": meta["sha256"]}


def uninstall(layout: Layout, uid: int, dry_run: bool = False) -> dict:
    report = {"removed": [], "kept": [], "rollback": None}
    try:
        report["rollback"] = rollback(layout, uid, dry_run)
    except Refusal as error:
        report["rollback"] = {"error": str(error)}
    record = load_record(layout, uid)
    for name in NAMES:
        target = layout.bin / name
        state = classify(layout, target, uid, record, False)
        if state["state"] == "ours":
            if not dry_run:
                info = os.lstat(target)
                if (info.st_dev, info.st_ino, sha256(target.read_bytes())) != state["identity"]:
                    raise Refusal(f"{target} changed; not removing it.")
                target.unlink()
                record["launchers"].pop(str(target), None)
                save_record(layout, record)
            report["removed"].append(str(target))
        elif state["state"] != "absent":
            report["kept"].append({"path": str(target), "reason": state.get("reason") or state["state"]})
    return report


def status(layout: Layout, uid: int) -> dict:
    record = load_record(layout, uid)
    return {name: {k: v for k, v in classify(layout, layout.bin / name, uid, record, False).items()
                   if k in ("state", "reason", "sha256")} for name in NAMES}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="ai-bridge-cli installer")
    parser.add_argument("command", choices=("install", "rollback", "uninstall", "status"))
    parser.add_argument("--venv-python", type=Path)
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
    try:
        if args.command == "install":
            if not args.venv_python:
                raise Refusal("--venv-python is required.")
            result = install(layout, args.venv_python, account.pw_uid,
                             replace_ai_bridge=not args.no_replace_ai_bridge,
                             accept_main_record=args.accept_main_record, dry_run=args.dry_run)
        elif args.command == "rollback":
            result = rollback(layout, account.pw_uid, args.dry_run)
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
