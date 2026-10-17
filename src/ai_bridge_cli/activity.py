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
from .fsutil import UnsafePath, read_regular
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
    # Fail-closed bookkeeping: anything we could not inspect. Destructive
    # callers must refuse unless `complete` (see trash.py).
    problems: list = field(default_factory=list)       # str
    uninspectable: list = field(default_factory=list)  # (pid, comm) same-user, fd table unreadable
    tolerated: list = field(default_factory=list)      # (pid, comm) allow-listed session managers
    taken: float = 0.0

    @property
    def complete(self) -> bool:
        return not self.problems and not self.uninspectable

    def incompleteness(self) -> str | None:
        if self.complete:
            return None
        parts = list(self.problems)
        if self.uninspectable:
            shown = ", ".join(f"{pid} ({comm})" for pid, comm in self.uninspectable[:5])
            more = f" and {len(self.uninspectable) - 5} more" if len(self.uninspectable) > 5 else ""
            parts.append(f"cannot see open files of process(es) {shown}{more}")
        return "; ".join(parts)

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


# Same-user processes whose fd table is normally unreadable (non-dumpable) and
# which never hold agent session files. Matched by comm and always reported.
TOLERATED_COMMS = {"(sd-pam)", "sshd", "sshd-session", "systemd"}


def _locks(act: Activity) -> set:
    out = set()
    try:
        text = (PROC / "locks").read_text()
    except OSError as error:
        act.problems.append(f"/proc/locks unreadable ({error.strerror})")
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


def _claude_registry(act: Activity) -> dict:
    out = {}
    registry = paths.claude_dir() / "sessions"
    try:
        entries = list(registry.iterdir())
    except FileNotFoundError:
        return out
    except OSError as error:
        act.problems.append(f"Claude session registry unreadable ({error.strerror})")
        return out
    for entry in entries:
        if entry.suffix != ".json":
            continue
        try:
            row = json.loads(read_regular(entry, 65536))
        except FileNotFoundError:
            continue  # session ended while scanning
        except (OSError, ValueError, UnsafePath) as error:
            act.problems.append(f"Claude session registry entry {entry.name} unreadable ({error})"[:200])
            continue
        if not isinstance(row, dict):
            act.problems.append(f"Claude session registry entry {entry.name} unrecognized")
            continue
        sid, pid, start = row.get("sessionId"), row.get("pid"), row.get("procStart")
        if is_uuid(sid) and isinstance(pid, int) and start is not None and proc_start_ticks(pid) == str(start):
            out[sid.lower()] = pid
    return out


def _comm(base: Path) -> str:
    try:
        return (base / "comm").read_text().strip()[:32]
    except OSError:
        return "?"


def _exited(base: Path) -> bool:
    """Only a confirmed zombie/dead process can be ignored: it holds no files."""
    try:
        return (base / "stat").read_text().rsplit(") ", 1)[1].split()[0] in ("Z", "X", "x")
    except (OSError, IndexError):
        return False  # unreadable state is not proof of exit


def snapshot() -> Activity:
    act = Activity(taken=time.time())
    me = os.geteuid()
    skip = _ancestors()
    own = os.getpid()
    try:
        pids = [int(e.name) for e in PROC.iterdir() if e.name.isdigit()]
    except OSError as error:
        act.problems.append(f"/proc is unavailable ({error.strerror}); running processes cannot be checked")
        pids = []
    if not pids and not act.problems:
        act.problems.append("/proc lists no processes; running processes cannot be checked")
    for pid in pids:
        base = PROC / str(pid)
        try:
            if base.stat().st_uid != me:
                continue
        except FileNotFoundError:
            continue  # exited
        except OSError as error:
            act.problems.append(f"process {pid} unreadable ({error.strerror})")
            continue
        if pid == own:  # previews briefly open stores; that is not use by another client
            continue
        if _exited(base):
            continue
        if pid not in skip:
            try:
                cmd = (base / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
                for match in UUID_RE.finditer(cmd):
                    act.cmdline_ids.setdefault(match.group(0).lower(), pid)
            except FileNotFoundError:
                continue
            except OSError as error:
                act.problems.append(f"command line of process {pid} unreadable ({error.strerror})")
        try:
            fds = os.listdir(base / "fd")
        except PermissionError:
            if _exited(base):  # may have exited since the first state check
                continue
            comm = _comm(base)
            (act.tolerated if comm in TOLERATED_COMMS else act.uninspectable).append((pid, comm))
            continue
        except FileNotFoundError:
            continue
        except OSError as error:
            act.problems.append(f"open files of process {pid} unreadable ({error.strerror})")
            continue
        for fd in fds:
            try:
                info = os.stat(base / "fd" / fd)
            except FileNotFoundError:
                continue  # closed meanwhile
            except PermissionError:
                if not _exited(base):
                    act.uninspectable.append((pid, _comm(base)))
                break
            except OSError:
                continue  # e.g. anonymous inode targets without a stat-able path
            act.open_files.setdefault((info.st_dev, info.st_ino), pid)
    act.locked = _locks(act)
    act.claude_live = _claude_registry(act)
    return act
