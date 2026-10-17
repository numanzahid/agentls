"""One pass over every native store, with a per-file parse cache.

A refresh only re-parses files whose (inode, size, mtime) changed; selection,
preview and actions use the resulting records instead of rescanning.
"""
from __future__ import annotations

import dataclasses
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from .. import paths
from ..model import Session
from . import claude, codex, cursor


@dataclass
class ScanResult:
    sessions: list[Session]
    errors: dict[str, str] = field(default_factory=dict)
    roots: dict[str, str] = field(default_factory=dict)
    seconds: float = 0.0


def _stat_key(info: os.stat_result) -> tuple:
    return (info.st_ino, info.st_size, info.st_mtime_ns)


class Scanner:
    def __init__(self):
        self._cache: dict[tuple, tuple[tuple, Session | None]] = {}

    def _cached(self, key: tuple, fingerprint: tuple, build):
        hit = self._cache.get(key)
        if hit and hit[0] == fingerprint:
            return hit[1]
        value = build()
        self._cache[key] = (fingerprint, value)
        return value

    def _claude(self, root: Path) -> list[Session]:
        out = []
        for path in claude.files(root):
            try:
                info = path.stat()
            except OSError:
                continue
            out.append(self._cached(("claude", str(path)), _stat_key(info),
                                    lambda: claude.parse(path, root, info)))
        return out

    def _codex(self, root: Path, include_archived: bool) -> list[Session]:
        index = root / "session_index.jsonl"
        try:
            index_key = _stat_key(index.stat())
        except OSError:
            index_key = None
        names: dict | None = None

        def titles():
            nonlocal names
            if names is None:  # read the index only if some rollout needs parsing
                names = codex.titles(root)
            return names

        out = []
        for path, archived in codex.files(root, include_archived):
            try:
                info = path.stat()
            except OSError:
                continue
            out.append(self._cached(("codex", str(path)), (_stat_key(info), index_key, archived),
                                    lambda: codex.parse(path, root, info, archived, titles())))
        return out

    def _cursor(self, root: Path) -> list[Session]:
        out = []
        for chat in cursor.chat_dirs(root):
            try:
                session = self._cached(("cursor", str(chat)), cursor.fingerprint(chat),
                                       lambda: cursor.parse(chat, root))
            except OSError:
                continue
            if session is not None:
                out.append(session)
        return out

    def scan(self, agents=paths.AGENTS, include_native_archived: bool = True) -> ScanResult:
        started = time.monotonic()
        result = ScanResult(sessions=[])
        for agent in agents:
            root = paths.agent_root(agent)
            result.roots[agent] = str(root)
            if not root.exists():
                continue
            try:
                if agent == "claude":
                    rows = self._claude(root)
                elif agent == "codex":
                    rows = self._codex(root, include_native_archived)
                else:
                    rows = self._cursor(root)
            except Exception as error:  # one broken store must not hide the others
                result.errors[agent] = f"{type(error).__name__}: {error}"[:300]
                continue
            result.sessions.extend(dataclasses.replace(row) for row in rows)
        _mark_duplicates(result.sessions)
        result.seconds = time.monotonic() - started
        return result


def _mark_duplicates(sessions: list[Session]) -> None:
    """Two stores claiming one ID make every action on it ambiguous."""
    seen: dict[tuple, list[Session]] = {}
    for session in sessions:
        seen.setdefault(session.key, []).append(session)
    for group in seen.values():
        if len(group) > 1:
            live = [s for s in group if not s.native_archived]
            if len(live) == 1 and all(s.native_archived for s in group if s is not live[0]):
                # Codex keeps a live copy while an archived copy lingers: hide the stale one.
                for stale in group:
                    if stale is not live[0]:
                        sessions.remove(stale)
                continue
            for session in group:
                session.error = "Ambiguous: this ID appears in more than one native store file."
                session.resumable = False
                session.resume_note = session.error


HISTORY = {"claude": claude.history, "codex": codex.history, "cursor": cursor.history}


def history(session: Session) -> list[dict]:
    return HISTORY[session.agent](session)
