"""Best-effort detection of sessions that are in use right now.

One snapshot per refresh (not per row):
* Claude Code registers live sessions in $CLAUDE_CONFIG_DIR/sessions/<pid>.json
  with pid + process start time; both must match /proc to count.
* same-user processes holding a session file open (/proc/<pid>/fd),
* file locks on session files or Codex thread-writer locks (/proc/locks),
* same-user command lines naming the session UUID (e.g. `claude --resume ID`),
  excluding this process and its ancestors.
Absence of evidence is not proof of idleness: callers additionally refuse
files modified within RECENT_SECONDS.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import paths
from .textsafe import UUID_RE, is_uuid

RECENT_SECONDS = 120
PROC = Path("/proc")


def proc_start_ticks(pid: int) -> str | None:
    try:
        fields = (PROC / str(pid) / "stat").read_text().rsplit(") ", 1)[1].split()
        return None if fields[0] == "Z" else fields[19]
    except (OSError, IndexError, ValueError):
        return None


def _ancestors() -> set[int]:
    out, pid = set(), os.getpid()
    for _ in range(64):
        out.add(pid)
        try:
            fields = (PROC / str(pid) / "stat").read_text().rsplit(") ", 1)[1].split()
            pid = int(fields[1])
        except (OSError, IndexError, ValueError):
            break
        if pid <= 1:
            break
    return out


@dataclass
class Activity:
    open_files: dict = field(default_factory=dict)    # (dev, ino) -> pid
    locked: set = field(default_factory=set)          # (dev, ino)
    cmdline_ids: dict = field(default_factory=dict)   # uuid(lower) -> pid
    claude_live: dict = field(default_factory=dict)   # session uuid -> pid
    uninspectable: int = 0
    taken: float = 0.0

    def evidence(self, agent: str, session_id: str, storage_path: str) -> str | None:
        sid = session_id.lower()
        if agent == "claude" and sid in self.claude_live:
            return f"Claude Code process {self.claude_live[sid]} has this session open."
        if sid in self.cmdline_ids:
            return f"Process {self.cmdline_ids[sid]} was started with this session ID."
        for candidate in _storage_files(agent, session_id, storage_path):
            try:
                info = os.stat(candidate, follow_symlinks=False)
            except OSError:
                continue
            key = (info.st_dev, info.st_ino)
            if key in self.open_files:
                return f"Process {self.open_files[key]} has {candidate.name} open."
            if key in self.locked:
                return f"{candidate.name} is locked by another process."
        return None


def _storage_files(agent: str, session_id: str, storage_path: str) -> list[Path]:
    path = Path(storage_path)
    if agent == "cursor":
        return [path, path.with_name("store.db-wal")]
    if agent == "codex":
        return [path, paths.codex_dir() / "thread-writer-locks" / f"{session_id}.lock"]
    return [path]


def _locks() -> set:
    out = set()
    try:
        text = (PROC / "locks").read_text()
    except OSError:
        return out
    for line in text.splitlines():
        # "1: FLOCK  ADVISORY  WRITE 1234 08:02:131 0 EOF" (blocked waiters use "->")
        columns = line.replace("->", "").split()
        for column in columns:
            if column.count(":") == 2:
                try:
                    major, minor, inode = column.split(":")
                    out.add((os.makedev(int(major, 16), int(minor, 16)), int(inode)))
                except ValueError:
                    pass
                break
    return out


def _claude_registry() -> dict:
    out = {}
    registry = paths.claude_dir() / "sessions"
    try:
        entries = list(registry.iterdir())
    except OSError:
        return out
    for entry in entries:
        if entry.suffix != ".json" or entry.is_symlink():
            continue
        try:
            if entry.stat().st_size > 65536:
                continue
            row = json.loads(entry.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(row, dict):
            continue
        sid, pid, start = row.get("sessionId"), row.get("pid"), row.get("procStart")
        if is_uuid(sid) and isinstance(pid, int) and start is not None and proc_start_ticks(pid) == str(start):
            out[sid.lower()] = pid
    return out


def snapshot() -> Activity:
    act = Activity(taken=time.time())
    me = os.geteuid()
    skip = _ancestors()
    own = os.getpid()
    try:
        pids = [int(e.name) for e in PROC.iterdir() if e.name.isdigit()]
    except OSError:
        pids = []
    for pid in pids:
        base = PROC / str(pid)
        try:
            if base.stat().st_uid != me:
                continue
        except OSError:
            continue
        if pid == own:  # previews briefly open stores; that is not use by another client
            continue
        if pid not in skip:
            try:
                cmd = (base / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
                for match in UUID_RE.finditer(cmd):
                    act.cmdline_ids.setdefault(match.group(0).lower(), pid)
            except OSError:
                pass
        try:
            fds = os.listdir(base / "fd")
        except PermissionError:
            act.uninspectable += 1
            continue
        except OSError:
            continue
        for fd in fds:
            try:
                info = os.stat(base / "fd" / fd)
            except OSError:
                continue
            act.open_files.setdefault((info.st_dev, info.st_ino), pid)
    act.locked = _locks()
    act.claude_live = _claude_registry()
    return act
