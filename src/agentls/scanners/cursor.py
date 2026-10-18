"""Cursor CLI chats: $AGENTLS_CURSOR_HOME/chats/<md5(workspace)>/<chat-uuid>/store.db.

SQLite stores are opened read-only (immutable when the WAL is empty).
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

from .. import paths
from ..model import Session
from ..textsafe import is_uuid
from .common import model_name, read_json_file
from .cursor_store import Store


def chat_dirs(root: Path | None = None) -> list[Path]:
    chats = (root or paths.cursor_home()) / "chats"
    out = []
    try:
        buckets = [b for b in chats.iterdir() if b.is_dir() and not b.is_symlink()]
    except OSError:
        return []
    for bucket in buckets:
        try:
            for chat in bucket.iterdir():
                if is_uuid(chat.name) and chat.is_dir() and not chat.is_symlink():
                    try:
                        if stat.S_ISREG((chat / "store.db").lstat().st_mode):
                            out.append(chat)
                    except OSError:
                        continue
        except OSError:
            continue
    return out


def fingerprint(chat: Path) -> tuple:
    """Cache key: committed data may live only in the WAL."""
    parts = []
    for name in ("store.db", "store.db-wal", "meta.json"):
        try:
            info = (chat / name).lstat()
            parts.append((info.st_ino, info.st_size, info.st_mtime_ns))
        except OSError:
            parts.append(None)
    return tuple(parts)


def _meta_json(chat: Path) -> dict:
    try:
        value = read_json_file(chat / "meta.json")
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def parse(chat: Path, root: Path) -> Session | None:
    """None for subagent chats (they are internal to a parent chat)."""
    db = chat / "store.db"
    info = db.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("Cursor store is not a regular file.")
    side = _meta_json(chat)
    if side.get("isSubagent"):
        return None
    updated = info.st_mtime
    if isinstance(side.get("updatedAtMs"), (int, float)):
        updated = max(updated, side["updatedAtMs"] / 1000)
    try:
        updated = max(updated, (chat / "store.db-wal").lstat().st_mtime)
    except OSError:
        pass
    session = Session(
        agent="cursor", session_id=chat.name, storage_path=str(db), storage_root=str(root),
        title="(unreadable chat)", updated=updated,
        created=side["createdAtMs"] / 1000 if isinstance(side.get("createdAtMs"), (int, float)) else None,
        session_type="main",
    )
    try:
        with Store(db) as store:
            if store.meta.get("subagentInfo"):
                return None
            session.cwd = store.workspace()
            session.title = " ".join(str(store.meta.get("name") or "(untitled chat)").split())[:200]
            session.last_used_model = model_name(store.meta.get("lastUsedModel"))
            session.resumable = store.can_resume(session.cwd) and bool(session.cwd and os.path.isdir(session.cwd))
            if not session.resumable:
                session.resume_note = ("Project path does not match this chat store, is missing, or the chat "
                                       "is empty. No automatic migration is attempted.")
            if store.root.get(6) or store.root.get(13):
                session.history_note = "Cursor compacted this chat; older turns may only remain as summaries."
    except Exception as error:  # schema drift must stay local to this row
        session.error = f"{type(error).__name__}: {error}"[:300]
        session.resume_note = "Chat store could not be read."
    return session


def history(session: Session) -> list[dict]:
    with Store(Path(session.storage_path)) as store:
        return store.messages()


def related_paths(session: Session) -> list[tuple[Path, bool]]:
    root = Path(session.storage_root)
    chat_dir = Path(session.storage_path).parent
    out = [(chat_dir, True)]
    projects = root / "projects"
    try:
        for project in projects.iterdir():
            if project.is_symlink() or not project.is_dir():
                continue
            transcript = project / "agent-transcripts" / session.session_id
            if transcript.exists() or transcript.is_symlink():
                out.append((transcript, False))
    except OSError:
        pass
    return out
