"""Explicit native deletion: preview -> confirm -> reversible move. Never unlink.

Per agent:
* claude, cursor: "quarantine". Every file/dir that makes up the session is
  renamed (same filesystem, atomic) into a private batch under
  $AI_BRIDGE_CLI_DATA_DIR/trash/<batch>/ with a manifest, and can be restored.
  Parents are pinned with O_NOFOLLOW walks from the agent root, so symlinks or
  paths outside the root are refused; each entry's (dev, inode, size, mtime)
  must still match the preview at move time.
* codex: "codex-archive". Rollout files are indexed by Codex's SQLite state, so
  they are never moved or removed by this tool; the installed `codex archive`
  command does it (and `codex unarchive` restores). Permanent Codex deletion
  is intentionally unsupported here.

Sessions that are active, uncertain, or modified within RECENT_SECONDS are
blocked, re-checked at execution time with a fresh process snapshot.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import activity as activity_mod
from . import paths
from .bridge import BridgeState, uncertain
from .fsutil import (UnsafePath, atomic_write_json, ensure_private_dir, file_lock, identity, lstat_at,
                     pinned_parent, read_bytes_nofollow, relative_parts, rename_noreplace)
from .model import Session
from .native import PreflightError, find_binary, native_env, probe_help
from .scanners import claude as claude_scanner
from .scanners import cursor as cursor_scanner
from .textsafe import is_uuid

METHODS = {"claude": "quarantine", "cursor": "quarantine", "codex": "codex-archive"}
SUPPORT_NOTES = {
    "claude": "Moved to the ai-bridge-cli trash (transcript, sidecar folder, file-history, session-env); restorable.",
    "cursor": "Chat folder (store.db + WAL) and agent transcripts moved to the ai-bridge-cli trash; restorable.",
    "codex": "Archived with the installed `codex archive` (keeps Codex's SQLite index consistent); "
             "restorable with `codex unarchive`. Rollout files are never touched directly.",
}
BATCH_RE = re.compile(r"\d{8}T\d{6}Z-[0-9a-f]{8}")


@dataclass
class Entry:
    path: str
    root: str
    required: bool
    kind: str            # file | dir
    identity: dict


@dataclass
class Item:
    agent: str
    session_id: str
    title: str
    method: str
    entries: list[Entry] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def allowed(self) -> bool:
        return not self.blockers


@dataclass
class DeletePlan:
    items: list[Item]
    plan_id: str
    sessions: list[Session] = field(default_factory=list, repr=False)

    @property
    def allowed(self) -> list[Item]:
        return [i for i in self.items if i.allowed]

    def to_json(self) -> dict:
        return {"plan_id": self.plan_id, "dry_run": True,
                "allowed": len(self.allowed), "blocked": len(self.items) - len(self.allowed),
                "items": [{**{k: v for k, v in asdict(i).items() if k != "entries"},
                           "allowed": i.allowed,
                           "entries": [{"path": e.path, "kind": e.kind, "required": e.required,
                                        "size": e.identity.get("size")} for e in i.entries],
                           "note": SUPPORT_NOTES[i.agent]} for i in self.items]}


def _device_of(path: Path) -> int | None:
    for candidate in (path, *path.parents):
        try:
            return candidate.stat().st_dev
        except OSError:
            continue
    return None


def _related(session: Session) -> list[tuple[Path, bool]]:
    if session.agent == "claude":
        return claude_scanner.related_paths(session)
    if session.agent == "cursor":
        return cursor_scanner.related_paths(session)
    return [(Path(session.storage_path), True)]


def _plan_item(session: Session, act: activity_mod.Activity, now: float, trash_dev: int | None,
               jobs: dict) -> Item:
    item = Item(agent=session.agent, session_id=session.session_id, title=session.display_title,
                method=METHODS[session.agent])
    if not is_uuid(session.session_id):
        item.blockers.append("Session ID is not an exact UUID.")
    if session.error:
        item.blockers.append(session.error)
    evidence = act.evidence(session.agent, session.session_id, session.storage_path)
    if evidence:
        item.blockers.append(f"In use: {evidence}")
    elif session.status in ("active", "uncertain"):
        item.blockers.append(f"In use: {session.status_note or session.status}")
    busy = sorted({*session.active_job_ids,
                   *(j["id"] for j in jobs.get((session.agent, session.session_id.lower()), []) if uncertain(j))})
    if busy:
        item.blockers.append(f"Bridge job(s) still unfinished: {', '.join(busy)}")
    root = Path(session.storage_root)
    expected_root = paths.agent_root(session.agent)
    if os.path.realpath(root) != os.path.realpath(expected_root):
        item.blockers.append("Session belongs to a different store than the configured one.")
    for path, required in _related(session):
        try:
            relative_parts(path, root)
            with pinned_parent(root, path) as (fd, name):
                info = lstat_at(fd, name)
        except FileNotFoundError:
            if required:
                item.blockers.append(f"Missing on disk (refresh): {path}")
            continue
        except (UnsafePath, ValueError) as error:
            item.blockers.append(f"Refusing {path}: {error}")
            continue
        if stat.S_ISLNK(info.st_mode):
            item.blockers.append(f"Refusing symlink: {path}")
            continue
        kind = "dir" if stat.S_ISDIR(info.st_mode) else "file" if stat.S_ISREG(info.st_mode) else None
        if kind is None:
            item.blockers.append(f"Refusing special file: {path}")
            continue
        if info.st_uid != os.geteuid():
            item.blockers.append(f"Not owned by you: {path}")
            continue
        if kind == "file" and info.st_nlink != 1:
            item.blockers.append(f"Refusing hard-linked file: {path}")
            continue
        if now - info.st_mtime < activity_mod.RECENT_SECONDS:
            # Stable wording: the text is part of the plan ID.
            item.blockers.append(f"Modified within the last {activity_mod.RECENT_SECONDS}s; "
                                 f"wait until it is idle: {path.name}")
        if item.method == "quarantine" and trash_dev is not None and info.st_dev != trash_dev:
            item.blockers.append(f"On a different filesystem than the trash; refusing copy+delete: {path}")
        item.entries.append(Entry(path=str(path), root=str(root), required=required, kind=kind,
                                  identity=identity(info)))
    if item.method == "codex-archive":
        if session.native_archived:
            item.blockers.append("Already archived by Codex.")
        else:
            binary = find_binary("codex")
            if not binary:
                item.blockers.append("Unsupported: codex CLI not found, and rollout files are never deleted directly.")
            else:
                try:
                    if "<SESSION>" not in probe_help([binary, "archive", "--help"]):
                        item.blockers.append("Unsupported: installed codex does not advertise `archive <SESSION>`.")
                except PreflightError as error:
                    item.blockers.append(f"Unsupported: {error}")
    return item


def _plan_id(items: list[Item]) -> str:
    canon = [(i.agent, i.session_id, i.method, sorted(i.blockers),
              [(e.path, sorted(e.identity.items())) for e in i.entries]) for i in items]
    return hashlib.sha256(json.dumps(canon, default=str).encode()).hexdigest()[:16]


def build_plan(sessions: list[Session], act: activity_mod.Activity | None = None) -> DeletePlan:
    act = act or activity_mod.snapshot()
    try:
        jobs = BridgeState().session_jobs()  # fresh, not the catalog's refresh-time view
    except Exception:
        jobs = {}
    now = time.time()
    trash_dev = _device_of(paths.trash_dir())
    seen, items = set(), []
    for session in sessions:
        if session.key in seen:
            continue
        seen.add(session.key)
        items.append(_plan_item(session, act, now, trash_dev, jobs))
    return DeletePlan(items=items, plan_id=_plan_id(items), sessions=list(sessions))


# -- execution ----------------------------------------------------------------

def _batch_dir(batch_id: str) -> Path:
    if not BATCH_RE.fullmatch(batch_id or ""):
        raise ValueError("Use an exact trash batch ID.")
    return paths.trash_dir() / batch_id


def _write_manifest(batch: Path, manifest: dict) -> None:
    atomic_write_json(batch / "manifest.json", manifest)


def _move_entry(entry: Entry, dst_fd: int, dst_name: str) -> None:
    with pinned_parent(Path(entry.root), Path(entry.path)) as (fd, name):
        info = lstat_at(fd, name)
        now = identity(info)
        if now != entry.identity:
            raise UnsafePath(f"{entry.path} changed since the preview")
        rename_noreplace(fd, name, dst_fd, dst_name)
        moved = lstat_at(dst_fd, dst_name)
        if (moved.st_dev, moved.st_ino) != (info.st_dev, info.st_ino):
            raise UnsafePath(f"{entry.path}: unexpected object after move")


def _move_back(entry: dict, src_fd: int) -> None:
    with pinned_parent(Path(entry["root"]), Path(entry["path"])) as (fd, name):
        info = lstat_at(src_fd, entry["stored"])
        if (info.st_dev, info.st_ino) != (entry["identity"]["dev"], entry["identity"]["ino"]):
            raise UnsafePath(f"Trash copy of {entry['path']} is not the original object.")
        try:
            rename_noreplace(src_fd, entry["stored"], fd, name)
        except FileExistsError:
            raise UnsafePath(f"Something already exists at {entry['path']}; not overwriting it.") from None


def _codex(command: str, session_id: str) -> None:
    binary = find_binary("codex")
    if not binary:
        raise PreflightError("codex CLI not found.")
    proc = subprocess.run([binary, command, session_id], capture_output=True, text=True, timeout=120,
                          env=native_env(), stdin=subprocess.DEVNULL, cwd="/")
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:] or [f"exit {proc.returncode}"]
        raise PreflightError(f"codex {command} failed: {detail[0][:300]}")


def execute(plan: DeletePlan, expected_plan_id: str | None = None) -> dict:
    """Re-plan from scratch and only proceed if nothing changed since the preview."""
    fresh = build_plan(plan.sessions)
    if fresh.plan_id != plan.plan_id or (expected_plan_id and expected_plan_id != fresh.plan_id):
        raise ValueError("Sessions changed since the preview (files, activity or blockers). "
                         "Run the preview again.")
    allowed = fresh.allowed
    result = {"batch_id": None, "moved": [], "failed": [], "skipped": [
        {"selector": f"{i.agent}:{i.session_id}", "blockers": i.blockers} for i in fresh.items if not i.allowed]}
    if not allowed:
        return result
    trash = ensure_private_dir(paths.trash_dir())
    batch_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + secrets.token_hex(4)
    batch = trash / batch_id
    batch.mkdir(mode=0o700)
    manifest = {"schema": 1, "batch_id": batch_id, "created": time.time(), "items": []}
    result["batch_id"] = batch_id
    with file_lock(trash / ".lock"):
        _write_manifest(batch, manifest)
        for index, item in enumerate(allowed):
            record = {"agent": item.agent, "session_id": item.session_id, "title": item.title,
                      "method": item.method, "status": "pending", "entries": []}
            manifest["items"].append(record)
            try:
                if item.method == "codex-archive":
                    _codex("archive", item.session_id)
                    record["status"] = "trashed"
                    record["entries"] = [{"path": e.path, "root": e.root, "kind": e.kind} for e in item.entries]
                else:
                    item_dir = batch / str(index)
                    item_dir.mkdir(mode=0o700)
                    dst_fd = os.open(item_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                    try:
                        for number, entry in enumerate(item.entries):
                            stored = str(number)
                            _move_entry(entry, dst_fd, stored)
                            record["entries"].append({**asdict(entry), "stored": stored})
                            _write_manifest(batch, manifest)
                    except BaseException:
                        for moved in reversed(record["entries"]):  # undo this session's partial move
                            try:
                                _move_back(moved, dst_fd)
                            except Exception:
                                record["status"] = "partial"
                        raise
                    finally:
                        os.close(dst_fd)
                    record["status"] = "trashed"
                result["moved"].append(f"{item.agent}:{item.session_id}")
            except Exception as error:
                record["status"] = "partial" if record["status"] == "partial" else "failed"
                record["error"] = str(error)[:300]
                result["failed"].append({"selector": f"{item.agent}:{item.session_id}", "error": record["error"],
                                         "status": record["status"]})
            _write_manifest(batch, manifest)
    return result


# -- listing / restore / purge -------------------------------------------------

def _load_manifest(batch_id: str) -> tuple[Path, dict]:
    batch = _batch_dir(batch_id)
    manifest = json.loads(read_bytes_nofollow(batch / "manifest.json", 4 << 20, private=True))
    if not isinstance(manifest, dict) or manifest.get("schema") != 1 or manifest.get("batch_id") != batch_id:
        raise ValueError("Unrecognized trash manifest.")
    for item in manifest.get("items", []):
        if item.get("agent") not in paths.AGENTS or not is_uuid(item.get("session_id")):
            raise ValueError("Corrupt trash manifest entry.")
        for entry in item.get("entries", []):
            root = Path(entry["root"])
            if os.path.realpath(root) != os.path.realpath(paths.agent_root(item["agent"])):
                raise ValueError(f"Manifest root {root} is not the configured {item['agent']} store; "
                                 "set the same CLAUDE_CONFIG_DIR/CODEX_HOME/AI_BRIDGE_CURSOR_HOME as when deleting.")
            relative_parts(Path(entry["path"]), root)
    return batch, manifest


def list_batches() -> list[dict]:
    trash = paths.trash_dir()
    if not trash.is_dir():
        return []
    out = []
    for name in sorted(os.listdir(trash), reverse=True):
        if not BATCH_RE.fullmatch(name):
            continue
        try:
            _batch, manifest = _load_manifest(name)
        except (OSError, ValueError, UnsafePath, KeyError) as error:
            out.append({"batch_id": name, "error": str(error)})
            continue
        out.append({"batch_id": name, "created": manifest.get("created"),
                    "items": [{k: i.get(k) for k in ("agent", "session_id", "title", "method", "status", "error")}
                              for i in manifest.get("items", [])]})
    return out


def restore(batch_id: str, selectors: list[str] | None = None, dry_run: bool = False) -> dict:
    trash = ensure_private_dir(paths.trash_dir())
    with file_lock(trash / ".lock"):
        batch, manifest = _load_manifest(batch_id)
        wanted = set(selectors or [])
        result = {"batch_id": batch_id, "dry_run": dry_run, "restored": [], "failed": [], "would_restore": []}
        for index, item in enumerate(manifest["items"]):
            selector = f"{item['agent']}:{item['session_id']}"
            if wanted and selector not in wanted:
                continue
            if item.get("status") != "trashed":
                continue
            if dry_run:
                result["would_restore"].append({"selector": selector, "method": item["method"],
                                                "paths": [e["path"] for e in item["entries"]]})
                continue
            try:
                if item["method"] == "codex-archive":
                    _codex("unarchive", item["session_id"])
                else:
                    item_dir = batch / str(index)
                    fd = os.open(item_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                    try:
                        for entry in item["entries"]:
                            if entry.get("restored"):
                                continue  # resumable after a partial restore
                            _move_back(entry, fd)
                            entry["restored"] = True
                    finally:
                        os.close(fd)
                    try:
                        item_dir.rmdir()
                    except OSError:
                        pass
                item["status"] = "restored"
                item["restored_at"] = time.time()
                result["restored"].append(selector)
            except Exception as error:
                item["error"] = f"restore: {error}"[:300]
                result["failed"].append({"selector": selector, "error": item["error"]})
            _write_manifest(batch, manifest)
        return result


def restore_native_codex(session: Session) -> dict:
    """Unarchive a session archived by Codex itself (not via a trash batch)."""
    if session.agent != "codex" or not session.native_archived:
        raise ValueError("Only natively archived Codex sessions can be restored this way.")
    _codex("unarchive", session.session_id)
    return {"restored": [session.selector], "method": "codex-unarchive"}


def purge(batch_id: str) -> dict:
    """Permanently remove one trash batch (only data this tool moved there)."""
    trash = ensure_private_dir(paths.trash_dir())
    with file_lock(trash / ".lock"):
        batch, manifest = _load_manifest(batch_id)
        if not shutil.rmtree.avoids_symlink_attacks:
            raise ValueError("Safe recursive removal is unavailable on this platform.")
        kept = [f"{i['agent']}:{i['session_id']}" for i in manifest["items"] if i["method"] == "codex-archive"
                and i.get("status") == "trashed"]
        fd = os.open(trash, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            shutil.rmtree(batch_id, dir_fd=fd)
        finally:
            os.close(fd)
        return {"batch_id": batch_id, "purged": True,
                "codex_still_archived": kept,
                "note": "Codex sessions stay archived by Codex (no permanent Codex deletion is performed)."
                if kept else None}
