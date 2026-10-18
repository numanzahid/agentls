"""Explicit native deletion: preview -> confirm -> journaled, reversible move. Never unlink.

Per agent:
* claude, cursor: "quarantine". Every file/dir that makes up the session is
  renamed (same filesystem, atomic, no-replace) into a private batch under
  $AGENTLS_DATA_DIR/trash/<batch>/ and can be restored.
* codex: "codex-archive". Rollout files are indexed by Codex's SQLite state, so
  they are never moved or removed by this tool; the installed `codex archive`
  does it (and `codex unarchive` restores). The separate TUI-only permanent
  deletion flow lives in codex_delete.py.

Safety model
* Plan: each entry records its identity (dev, inode, type, size, mtime,
  ctime); directories also get a bounded recursive fingerprint of every object
  inside (no symlink following), so in-place store.db/WAL writes or nested
  file changes are detected. Anything modified within RECENT_SECONDS, sessions
  in use or with unfinished/unreadable MCP jobs, and any incomplete
  process inspection block the plan (fail closed).
* Execute: re-plans from scratch and requires the same plan ID, then for each
  session takes a fresh process snapshot and, immediately before each rename,
  re-validates identity and fingerprint.
* Write-ahead journal: every entry (with its stored name and identity) is
  durably recorded in the manifest *before* it is renamed; states move
  planned -> moving -> in_trash (and restoring -> restored), each fsynced
  together with both directories. After a crash, `recover` resolves each
  entry by locating the object physically, so nothing can be stranded
  untracked. Restore resumes partial batches; purge refuses unresolved ones.
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
from .mcp import McpState, uncertain
from .fsutil import (TreeTooLarge, UnsafePath, atomic_write_json, ensure_private_dir, file_lock, fsync_dir,
                     identity, lstat_at, pinned_parent, read_bytes_nofollow, relative_parts, rename_noreplace,
                     tree_fingerprint)
from .model import Session
from .native import PreflightError, find_binary, native_env, probe_help
from .scanners import claude as claude_scanner
from .scanners import cursor as cursor_scanner
from .textsafe import is_uuid

SCHEMA = 2
METHODS = {"claude": "quarantine", "cursor": "quarantine", "codex": "codex-archive"}
SUPPORT_NOTES = {
    "claude": "Moved to the agentls trash (transcript, sidecar folder, file-history, session-env); restorable.",
    "cursor": "Chat folder (store.db + WAL) and agent transcripts moved to the agentls trash; restorable.",
    "codex": "Archived with the installed `codex archive` (keeps Codex's SQLite index consistent); "
             "restorable with `codex unarchive`. Rollout files are never touched directly.",
}
BATCH_RE = re.compile(r"\d{8}T\d{6}Z-[0-9a-f]{8}")
UNRESOLVED = {"moving", "restoring", "lost", "archiving", "unarchiving"}


def _fault(point: str) -> None:
    """Fault-injection hook for tests (no-op in normal operation)."""


@dataclass
class Entry:
    path: str
    root: str
    required: bool
    kind: str            # file | dir
    identity: dict
    tree: str | None = None   # digest of the recursive fingerprint (dirs)
    files: int = 0


@dataclass
class Item:
    agent: str
    session_id: str
    title: str
    method: str
    entries: list[Entry] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    activity_proof: tuple = ()

    @property
    def allowed(self) -> bool:
        return not self.blockers


@dataclass
class DeletePlan:
    items: list[Item]
    plan_id: str
    sessions: list[Session] = field(default_factory=list, repr=False)
    allow_uninspectable: bool = False

    @property
    def allowed(self) -> list[Item]:
        return [i for i in self.items if i.allowed]

    def to_json(self) -> dict:
        return {"plan_id": self.plan_id, "dry_run": True,
                "allowed": len(self.allowed), "blocked": len(self.items) - len(self.allowed),
                "allow_uninspectable": self.allow_uninspectable,
                "items": [{**{k: v for k, v in asdict(i).items() if k != "entries"},
                           "allowed": i.allowed,
                           "entries": [{"path": e.path, "kind": e.kind, "required": e.required,
                                        "size": e.identity.get("size"), "files": e.files} for e in i.entries],
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


def _digest(fingerprint: dict) -> str:
    return hashlib.sha256(json.dumps(fingerprint["entries"]).encode()).hexdigest()


def _inspect_dir(parent_fd: int, name: str) -> dict:
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
    try:
        return tree_fingerprint(fd)
    finally:
        os.close(fd)


def _check_entry(path: Path, root: Path, required: bool, now: float, trash_dev: int | None,
                 quarantine: bool, blockers: list[str], act: activity_mod.Activity | None = None) -> Entry | None:
    try:
        relative_parts(path, root)
        with pinned_parent(root, path) as (fd, name):
            info = lstat_at(fd, name)
            tree = _inspect_dir(fd, name) if stat.S_ISDIR(info.st_mode) else None
    except FileNotFoundError:
        if required:
            blockers.append(f"Missing on disk (refresh): {path}")
        return None
    except TreeTooLarge as error:
        blockers.append(f"Too large to verify safely ({error}): {path}")
        return None
    except (UnsafePath, ValueError, OSError) as error:
        blockers.append(f"Refusing {path}: {error}")
        return None
    if stat.S_ISLNK(info.st_mode):
        blockers.append(f"Refusing symlink: {path}")
        return None
    kind = "dir" if stat.S_ISDIR(info.st_mode) else "file" if stat.S_ISREG(info.st_mode) else None
    if kind is None:
        blockers.append(f"Refusing special file: {path}")
        return None
    if info.st_uid != os.geteuid():
        blockers.append(f"Not owned by you: {path}")
        return None
    if kind == "file" and info.st_nlink != 1:
        blockers.append(f"Refusing hard-linked file: {path}")
        return None
    newest = info.st_mtime
    if tree:
        newest = max(newest, tree["newest_mtime"])
        if tree["foreign_owner"]:
            blockers.append(f"Contains files not owned by you: {path}")
        if tree["special"]:
            blockers.append(f"Contains special files (FIFO/socket/device): {path}")
        if any(stat.S_ISLNK(e[1]) for e in tree["entries"]):
            blockers.append(f"Contains symlinks; refusing to move it: {path}")
    if now - newest < activity_mod.RECENT_SECONDS:
        # Stable wording: blocker text is part of the plan ID.
        blockers.append(f"Modified within the last {activity_mod.RECENT_SECONDS}s; "
                        f"wait until it is idle: {path.name}")
    if quarantine and act is not None:
        related = [(str(path), info.st_dev, info.st_ino)]
        related.extend((str(path / e[0]), e[2], e[3]) for e in (tree or {}).get('entries', []))
        for label, dev, ino in related:
            if (dev, ino) in act.open_files:
                blockers.append(f'In use: related session file {label} is open by PID {act.open_files[(dev, ino)]}.')
            if (dev, ino) in act.locked:
                blockers.append(f'In use: related session file {label} is locked.')
    if quarantine and trash_dev is not None and info.st_dev != trash_dev:
        blockers.append(f"On a different filesystem than the trash; refusing copy+delete: {path}")
    return Entry(path=str(path), root=str(root), required=required, kind=kind, identity=identity(info),
                 tree=_digest(tree) if tree else None, files=len(tree["entries"]) if tree else 0)


def _plan_item(session: Session, act: activity_mod.Activity, now: float, trash_dev: int | None,
               mcp: McpState, jobs: dict, allow_uninspectable: bool,
               *, codex_permanent: bool = False) -> Item:
    item = Item(agent=session.agent, session_id=session.session_id, title=session.display_title,
                method=METHODS[session.agent])
    if not is_uuid(session.session_id):
        item.blockers.append("Session ID is not an exact UUID.")
    if session.error:
        item.blockers.append(session.error)
    evidence = act.evidence(session.agent, session.session_id, session.storage_path)
    item.activity_proof = act.managed_idle.get((session.session_id, session.storage_path), ())
    if item.activity_proof:
        item.warnings.append('Verified managed shared Codex handle: idle chat. '
                             'Native archive rechecks activity; daemon will stay running.')
    if evidence:
        item.blockers.append(f"In use: {evidence}")
    elif session.status in ("active", "uncertain", "recent"):
        item.blockers.append(f"Status is {session.status}: {session.status_note or 'possibly in use'}")
    incomplete = act.incompleteness()
    if incomplete:
        if allow_uninspectable:
            item.warnings.append(f"HIGH RISK: deletion allowed although {incomplete}.")
        else:
            item.blockers.append(f"Running processes cannot be fully inspected ({incomplete}); "
                                 "close them, or use the CLI with --allow-uninspectable (high risk).")
    busy = sorted({*session.active_job_ids,
                   *(j["id"] for j in jobs.get((session.agent, session.session_id.lower()), []) if uncertain(j))})
    if busy:
        item.blockers.append(f"MCP job(s) still unfinished: {', '.join(busy)}")
    if mcp.problems:
        item.blockers.append(f"{len(mcp.problems)} MCP job record(s) unreadable; a running job "
                             "cannot be ruled out.")
    orphans = sorted(j["id"] for j in mcp.unattributed() if j.get("agent") == session.agent)
    if orphans:
        item.blockers.append(f"Unfinished {session.agent} MCP job(s) with no known session yet: "
                             f"{', '.join(orphans)}")
    root = Path(session.storage_root)
    if os.path.realpath(root) != os.path.realpath(paths.agent_root(session.agent)):
        item.blockers.append("Session belongs to a different store than the configured one.")
    for path, required in _related(session):
        entry = _check_entry(path, root, required, now, trash_dev, item.method == "quarantine", item.blockers, act)
        if entry:
            item.entries.append(entry)
    if item.method == "codex-archive" and not codex_permanent:
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


def _plan_id(items: list[Item], allow_uninspectable: bool) -> str:
    canon = [allow_uninspectable] + [
        (i.agent, i.session_id, i.method, sorted(i.blockers), i.activity_proof,
         [(e.path, sorted(e.identity.items()), e.tree) for e in i.entries]) for i in items]
    return hashlib.sha256(json.dumps(canon, default=str).encode()).hexdigest()[:16]


def build_plan(sessions: list[Session], act: activity_mod.Activity | None = None,
               allow_uninspectable: bool = False) -> DeletePlan:
    act = act or activity_mod.snapshot()
    mcp = McpState()
    try:
        jobs = mcp.session_jobs()  # fresh, not the catalog's refresh-time view
    except Exception as error:  # fail closed: unknown MCP state blocks everything
        jobs = {}
        mcp.problems = [f"MCP state unreadable: {error}"]
    now = time.time()
    trash_dev = _device_of(paths.trash_dir())
    seen, items = set(), []
    for session in sessions:
        if session.key in seen:
            continue
        seen.add(session.key)
        items.append(_plan_item(session, act, now, trash_dev, mcp, jobs, allow_uninspectable))
    return DeletePlan(items=items, plan_id=_plan_id(items, allow_uninspectable), sessions=list(sessions),
                      allow_uninspectable=allow_uninspectable)


# -- journal ------------------------------------------------------------------

def _batch_dir(batch_id: str) -> Path:
    if not BATCH_RE.fullmatch(batch_id or ""):
        raise ValueError("Use an exact trash batch ID.")
    return paths.trash_dir() / batch_id


def _write_manifest(batch: Path, manifest: dict) -> None:
    """Durable: file fsync + directory fsync (atomic_write_json)."""
    atomic_write_json(batch / "manifest.json", manifest)


def _revalidate(entry: dict, fd: int, name: str) -> os.stat_result:
    """Immediately before a rename: same object, same contents, still idle."""
    info = lstat_at(fd, name)
    if identity(info) != entry["identity"]:
        raise UnsafePath(f"{entry['path']} changed since the preview")
    if entry["kind"] == "dir":
        tree = _inspect_dir(fd, name)
        if _digest(tree) != entry["tree"]:
            raise UnsafePath(f"contents of {entry['path']} changed since the preview")
        if time.time() - tree["newest_mtime"] < activity_mod.RECENT_SECONDS:
            raise UnsafePath(f"{entry['path']} was modified moments ago")
    return info


def _move_in(entry: dict, item_fd: int, batch: Path, manifest: dict) -> None:
    entry["state"] = "moving"
    _write_manifest(batch, manifest)   # intent is durable before the rename
    _fault("before_rename")
    with pinned_parent(Path(entry["root"]), Path(entry["path"])) as (fd, name):
        info = _revalidate(entry, fd, name)
        rename_noreplace(fd, name, item_fd, entry["stored"])
        fsync_dir(fd)
        fsync_dir(item_fd)
        moved = lstat_at(item_fd, entry["stored"])
        if (moved.st_dev, moved.st_ino) != (info.st_dev, info.st_ino):
            raise UnsafePath(f"{entry['path']}: unexpected object after move")
    _fault("after_rename")
    entry["state"] = "in_trash"
    _write_manifest(batch, manifest)


def _move_out(entry: dict, item_fd: int, batch: Path, manifest: dict) -> None:
    entry["state"] = "restoring"
    _write_manifest(batch, manifest)
    _fault("before_restore_rename")
    with pinned_parent(Path(entry["root"]), Path(entry["path"])) as (fd, name):
        info = lstat_at(item_fd, entry["stored"])
        if (info.st_dev, info.st_ino) != (entry["identity"]["dev"], entry["identity"]["ino"]):
            entry["state"] = "in_trash"
            raise UnsafePath(f"Trash copy of {entry['path']} is not the original object.")
        try:
            rename_noreplace(item_fd, entry["stored"], fd, name)
        except FileExistsError:
            entry["state"] = "in_trash"
            raise UnsafePath(f"Something already exists at {entry['path']}; not overwriting it.") from None
        except OSError:
            entry["state"] = "in_trash"
            raise
        fsync_dir(item_fd)
        fsync_dir(fd)
    entry["state"] = "restored"
    _write_manifest(batch, manifest)


def _locate(entry: dict, item_dir: Path) -> str:
    """Where is this entry's object right now? trash | original | absent | conflict."""
    ident = (entry["identity"]["dev"], entry["identity"]["ino"])
    try:
        info = (item_dir / entry["stored"]).lstat()
        if (info.st_dev, info.st_ino) == ident:
            return "trash"
    except FileNotFoundError:
        pass
    try:
        with pinned_parent(Path(entry["root"]), Path(entry["path"])) as (fd, name):
            info = lstat_at(fd, name)
        return "original" if (info.st_dev, info.st_ino) == ident else "conflict"
    except FileNotFoundError:
        return "absent"
    except (UnsafePath, ValueError, OSError):
        return "conflict"


def _resolve_entry(entry: dict, item_dir: Path) -> None:
    """Set the entry's state from where its object physically is."""
    where = _locate(entry, item_dir)
    state = entry.get("state", "planned")
    if where == "trash":
        entry["state"] = "in_trash"
    elif where == "original":
        entry["state"] = "restored" if state in ("restoring", "restored") else "not_moved"
    elif state in ("restored", "not_moved", "planned"):
        # Never moved by us, or already handed back: later changes there are the user's.
        entry["state"] = "not_moved" if state == "planned" else state
    else:
        entry["state"] = "lost"  # journal says it is ours, but it is nowhere to be found


def _resolve_codex(item: dict) -> None:
    """After an interrupted/failed codex call, infer the outcome from the rollout's location."""
    present = any(os.path.lexists(e["path"]) for e in item["entries"])
    state = item.get("codex_state")
    if state == "archiving":
        item["codex_state"] = "not_archived" if present else "archived"
    elif state == "unarchiving":
        item["codex_state"] = "unarchived" if present else "archived"


def _item_status(item: dict) -> str:
    if item["method"] == "codex-archive":
        return {"archived": "trashed", "unarchived": "restored", "not_archived": "failed",
                "archiving": "unresolved", "unarchiving": "unresolved"}.get(item.get("codex_state"), "failed")
    states = {e.get("state", "planned") for e in item["entries"]}
    if states & UNRESOLVED:
        return "unresolved"
    if states == {"in_trash"}:
        return "trashed"
    if states <= {"not_moved", "planned"}:
        return "failed"
    if states <= {"restored", "not_moved"}:
        return "restored"
    return "partial"   # some objects in the trash, some back in place


def recover(batch: Path, manifest: dict) -> bool:
    """Resolve every entry by locating objects physically. Returns True if anything changed."""
    changed = False
    for index, item in enumerate(manifest["items"]):
        if item["method"] == "codex-archive":
            before = item.get("codex_state")
            _resolve_codex(item)
            changed |= item.get("codex_state") != before
        else:
            for entry in item["entries"]:
                before = entry.get("state")
                _resolve_entry(entry, batch / str(index))
                changed |= entry["state"] != before
        status = _item_status(item)
        if status != item.get("status"):
            item["status"], changed = status, True
    return changed


def _codex(command: str, session_id: str) -> None:
    binary = find_binary("codex")
    if not binary:
        raise PreflightError("codex CLI not found.")
    proc = subprocess.run([binary, command, session_id], capture_output=True, text=True, timeout=120,
                          env=native_env(), stdin=subprocess.DEVNULL, cwd="/")
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:] or [f"exit {proc.returncode}"]
        raise PreflightError(f"codex {command} failed: {detail[0][:300]}")


def _session_still_free(item: Item, session: Session, allow_uninspectable: bool) -> None:
    act = activity_mod.snapshot()
    evidence = act.evidence(session.agent, session.session_id, session.storage_path)
    if evidence:
        raise UnsafePath(f"Session became active ({evidence}).")
    if act.managed_idle.get((session.session_id, session.storage_path), ()) != item.activity_proof:
        raise UnsafePath('Session holder identities changed since the preview.')
    if act.incompleteness() and not allow_uninspectable:
        raise UnsafePath(f"Running processes cannot be fully inspected ({act.incompleteness()}).")


def execute(plan: DeletePlan, expected_plan_id: str | None = None) -> dict:
    """Re-plan from scratch and only proceed if nothing changed since the preview."""
    fresh = build_plan(plan.sessions, allow_uninspectable=plan.allow_uninspectable)
    if fresh.plan_id != plan.plan_id or (expected_plan_id and expected_plan_id != fresh.plan_id):
        raise ValueError("Sessions changed since the preview (files, activity or blockers). "
                         "Run the preview again.")
    sessions = {s.key: s for s in plan.sessions}
    result = {"batch_id": None, "moved": [], "failed": [], "skipped": [
        {"selector": f"{i.agent}:{i.session_id}", "blockers": i.blockers} for i in fresh.items if not i.allowed]}
    allowed = fresh.allowed
    if not allowed:
        return result
    trash = ensure_private_dir(paths.trash_dir())
    batch_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + secrets.token_hex(4)
    batch = trash / batch_id
    with file_lock(trash / ".lock"):
        batch.mkdir(mode=0o700)
        fsync_dir(trash)
        manifest = {"schema": SCHEMA, "batch_id": batch_id, "created": time.time(), "items": []}
        _write_manifest(batch, manifest)
        result["batch_id"] = batch_id
        for index, item in enumerate(allowed):
            selector = f"{item.agent}:{item.session_id}"
            record = {"agent": item.agent, "session_id": item.session_id, "title": item.title,
                      "method": item.method, "status": "pending", "entries": [
                          {**asdict(e), "stored": str(n), "state": "planned"} for n, e in enumerate(item.entries)]}
            manifest["items"].append(record)
            try:
                _write_manifest(batch, manifest)   # the whole item is journaled before anything moves
                _session_still_free(item, sessions[(item.agent, item.session_id)], plan.allow_uninspectable)
                if item.method == "codex-archive":
                    record["codex_state"] = "archiving"
                    _write_manifest(batch, manifest)
                    for entry in record['entries']:
                        with pinned_parent(Path(entry['root']), Path(entry['path'])) as (fd, name):
                            _revalidate(entry, fd, name)
                    _session_still_free(item, sessions[(item.agent, item.session_id)], plan.allow_uninspectable)
                    _codex("archive", item.session_id)
                    record["codex_state"] = "archived"
                else:
                    item_dir = batch / str(index)
                    item_dir.mkdir(mode=0o700)
                    fsync_dir(batch)
                    item_fd = os.open(item_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                    try:
                        try:
                            for entry in record["entries"]:
                                _move_in(entry, item_fd, batch, manifest)
                        except Exception:
                            for entry in reversed(record["entries"]):  # undo this session's partial move
                                if entry["state"] in ("in_trash", "moving"):
                                    _resolve_entry(entry, item_dir)
                                if entry["state"] == "in_trash":
                                    try:
                                        _move_out(entry, item_fd, batch, manifest)
                                        entry["state"] = "not_moved"
                                    except Exception:
                                        pass  # stays in_trash: the item is partial and restorable
                            raise
                    finally:
                        os.close(item_fd)
                record["status"] = _item_status(record)
                result["moved"].append(selector)
            except Exception as error:
                if item.method == "codex-archive":
                    if record.get("codex_state") == "archiving":
                        _resolve_codex(record)
                    else:
                        record["codex_state"] = "not_archived"
                record["status"] = _item_status(record)
                record["error"] = str(error)[:300]
                result["failed"].append({"selector": selector, "error": record["error"],
                                         "status": record["status"]})
            try:
                _write_manifest(batch, manifest)
            except OSError as error:
                result["failed"].append({"selector": selector, "error": f"journal write failed: {error}",
                                         "status": "unresolved"})
                break  # recovery resolves from the last durable journal state
    return result


# -- listing / restore / purge -------------------------------------------------

def _load_manifest(batch_id: str) -> tuple[Path, dict]:
    batch = _batch_dir(batch_id)
    manifest = json.loads(read_bytes_nofollow(batch / "manifest.json", 4 << 20, private=True))
    if (not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA or
            manifest.get("batch_id") != batch_id or not isinstance(manifest.get("items"), list)):
        raise ValueError("Unrecognized trash manifest.")
    for item in manifest["items"]:
        if item.get("agent") not in paths.AGENTS or not is_uuid(item.get("session_id")):
            raise ValueError("Corrupt trash manifest entry.")
        for entry in item.get("entries", []):
            root = Path(entry["root"])
            if os.path.realpath(root) != os.path.realpath(paths.agent_root(item["agent"])):
                raise ValueError(f"Manifest root {root} is not the configured {item['agent']} store; "
                                 "set the same CLAUDE_CONFIG_DIR/CODEX_HOME/AGENTLS_CURSOR_HOME as when deleting.")
            relative_parts(Path(entry["path"]), root)
            if not re.fullmatch(r"\d{1,4}", str(entry.get("stored", ""))):
                raise ValueError("Corrupt trash manifest entry.")
    return batch, manifest


def list_batches() -> list[dict]:
    """Read-only: statuses are resolved in memory, nothing is written."""
    trash = paths.trash_dir()
    if not trash.is_dir():
        return []
    out = []
    for name in sorted(os.listdir(trash), reverse=True):
        if not BATCH_RE.fullmatch(name):
            continue
        try:
            batch, manifest = _load_manifest(name)
            recover(batch, manifest)
        except (OSError, ValueError, UnsafePath, KeyError) as error:
            out.append({"batch_id": name, "error": str(error)})
            continue
        out.append({"batch_id": name, "created": manifest.get("created"),
                    "items": [{k: i.get(k) for k in ("agent", "session_id", "title", "method", "status", "error")}
                              for i in manifest["items"]]})
    return out


def restore(batch_id: str, selectors: list[str] | None = None, dry_run: bool = False) -> dict:
    """Restore every object still in the trash (trashed, partial or interrupted items)."""
    trash = ensure_private_dir(paths.trash_dir())
    with file_lock(trash / ".lock"):
        batch, manifest = _load_manifest(batch_id)
        if recover(batch, manifest) and not dry_run:
            _write_manifest(batch, manifest)
        wanted = set(selectors or [])
        result = {"batch_id": batch_id, "dry_run": dry_run, "restored": [], "failed": [], "would_restore": []}
        for index, item in enumerate(manifest["items"]):
            selector = f"{item['agent']}:{item['session_id']}"
            if wanted and selector not in wanted:
                continue
            if item["status"] == "unresolved":
                result["failed"].append({"selector": selector, "error": "Journal cannot locate some objects; "
                                         "inspect the batch folder manually."})
                continue
            if item["status"] not in ("trashed", "partial"):
                continue
            pending = [e for e in item["entries"] if e.get("state") == "in_trash"]
            if dry_run:
                result["would_restore"].append({"selector": selector, "method": item["method"],
                                                "paths": [e["path"] for e in pending] or
                                                [e["path"] for e in item["entries"]]})
                continue
            try:
                if item["method"] == "codex-archive":
                    item["codex_state"] = "unarchiving"
                    _write_manifest(batch, manifest)
                    _codex("unarchive", item["session_id"])
                    item["codex_state"] = "unarchived"
                else:
                    item_dir = batch / str(index)
                    fd = os.open(item_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                    try:
                        for entry in pending:
                            _move_out(entry, fd, batch, manifest)
                    finally:
                        os.close(fd)
                    try:
                        item_dir.rmdir()
                    except OSError:
                        pass
                item["status"] = _item_status(item)
                item["restored_at"] = time.time()
                item.pop("error", None)
                result["restored"].append(selector)
            except Exception as error:
                if item["method"] == "codex-archive":
                    _resolve_codex(item)
                item["status"] = _item_status(item)
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
    """Permanently remove one fully resolved trash batch (only data this tool moved there)."""
    trash = ensure_private_dir(paths.trash_dir())
    with file_lock(trash / ".lock"):
        batch, manifest = _load_manifest(batch_id)
        if recover(batch, manifest):
            _write_manifest(batch, manifest)
        blocked = [f"{i['agent']}:{i['session_id']} ({i['status']})" for i in manifest["items"]
                   if i["status"] in ("unresolved", "partial", "pending")]
        if blocked:
            raise ValueError("Refusing to purge a batch with unresolved or partially restored sessions: "
                             + ", ".join(blocked) + ". Restore them first.")
        if not shutil.rmtree.avoids_symlink_attacks:
            raise ValueError("Safe recursive removal is unavailable on this platform.")
        kept = [f"{i['agent']}:{i['session_id']}" for i in manifest["items"] if i["method"] == "codex-archive"
                and i.get("status") == "trashed"]
        fd = os.open(trash, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            shutil.rmtree(batch_id, dir_fd=fd)
            os.fsync(fd)
        finally:
            os.close(fd)
        return {"batch_id": batch_id, "purged": True,
                "codex_still_archived": kept,
                "note": "Codex sessions stay archived by Codex (no permanent Codex deletion is performed)."
                if kept else None}
