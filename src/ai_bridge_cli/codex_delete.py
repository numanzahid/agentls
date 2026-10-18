"""TUI-only permanent deletion, delegated entirely to the native Codex CLI.

No native files are written here. A plan is consent for one exact archived
thread and its checked descendants; native thread/delete owns reference checks
and consistency. Every check is repeated after consent, with no risk override.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import selectors
import sqlite3
import stat
import subprocess
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from . import activity, paths, trash
from .bridge import BridgeState
from .fsutil import UnsafePath, identity, not_writable_by_others, pinned_parent, lstat_at, read_json_nofollow
from .model import Session
from .native import find_binary, native_env, storage_safe
from .scanners import codex
from .scanners.common import HEAD_BYTES, iter_jsonl
from .textsafe import is_uuid, one_line

OUTPUT_LIMIT = 65536
TIMEOUT = 30.0


def _run(argv: list[str], root: Path, timeout: float = TIMEOUT) -> str:
    """No shell/terminal; bounded combined output and wall time, including help."""
    env = native_env()
    env.update(CODEX_HOME=str(root), TERM='dumb', NO_COLOR='1')
    output = bytearray()
    deadline = time.monotonic() + timeout
    with subprocess.Popen(argv, cwd='/', env=env, stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          start_new_session=True) as proc:
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(proc.stdout, selectors.EVENT_READ)
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise UnsafePath('Native Codex timed out; outcome unconfirmed. Refresh before retrying.')
                    for key, _ in selector.select(min(remaining, 0.1)):
                        chunk = os.read(key.fileobj.fileno(), 8192)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        output.extend(chunk)
                        if len(output) > OUTPUT_LIMIT:
                            raise UnsafePath('Native Codex output exceeded limit; outcome unconfirmed.')
                proc.wait(timeout=max(0.001, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            raise UnsafePath('Native Codex timed out; outcome unconfirmed. Refresh before retrying.') from None
        except BaseException:
            # Only our own subprocess, never the shared app-server or its group.
            proc.kill()
            proc.wait()
            raise
    text = output.decode(errors='replace')
    if proc.returncode:
        lines = text.strip().splitlines()
        detail = one_line(lines[-1], 300) if lines else f'exit {proc.returncode}'
        raise UnsafePath(f'Native Codex refused or failed: {detail}. Outcome unconfirmed; refreshed.')
    return text


def _safe_root(root: Path) -> None:
    if '..' in root.parts:
        raise UnsafePath('Traversal in CODEX_HOME.')
    for path in (*reversed(root.parents), root):
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise UnsafePath('Symlink or non-directory in CODEX_HOME.')
    if not not_writable_by_others(root) or root.stat().st_uid != os.geteuid():
        raise UnsafePath('CODEX_HOME has unsafe ownership or permissions.')


def _inventory(root: Path) -> list[Session]:
    """Uncached inventory; never use the scanner's duplicate-collapsing view."""
    rows = []
    for base, archived in ((root / 'sessions', False), (root / 'archived_sessions', True)):
        if not os.path.lexists(base):
            continue
        def failed(error):
            raise error
        for directory, dirs, names in os.walk(base, onerror=failed):
            directory_path = Path(directory)
            directory_info = directory_path.lstat()
            if (not stat.S_ISDIR(directory_info.st_mode) or directory_info.st_uid != os.geteuid()
                    or not not_writable_by_others(directory_path, directory_info)):
                raise UnsafePath('Unsafe ownership, permissions or symlink in native session directory.')
            for name in [*dirs, *names]:
                path = Path(directory) / name
                with pinned_parent(root, path) as (fd, leaf):
                    info = lstat_at(fd, leaf)
                if stat.S_ISLNK(info.st_mode):
                    raise UnsafePath('Symlink in native session inventory; archive state uncertain.')
            for name in names:
                if not name.startswith('rollout-') or not name.endswith('.jsonl'):
                    continue
                path = Path(directory) / name
                info = path.lstat()
                row = codex.parse(path, root, info, archived, {})
                storage_safe(row)
                meta = next((e.get('payload') for e in iter_jsonl(path, max_lines=300, max_bytes=HEAD_BYTES)
                             if e.get('type') == 'session_meta'), None)
                if not isinstance(meta, dict) or not is_uuid(meta.get('id')) or meta['id'] != row.session_id:
                    raise UnsafePath('Native rollout UUID metadata is uncertain.')
                # Forks and subagents both matter for live-use checks. Native
                # deletion decides whether references permit removing them.
                row.parent_session_id = meta.get('forked_from_id') or row.parent_session_id
                rows.append(row)
                if len(rows) > 100000:
                    raise UnsafePath('Native inventory exceeds inspection limit.')
    return rows


def _index(root: Path, sid: str, *, require_present: bool = True) -> list[tuple]:
    """Read only: an indexed thread must agree with the exact archived rollout."""
    databases = sorted(root.glob('state_*.sqlite'))
    if len(databases) > 1:
        raise UnsafePath('Multiple native state indexes; archive state uncertain.')
    result = []
    for db in databases:
        for candidate in (db, Path(str(db) + '-wal'), Path(str(db) + '-shm')):
            if os.path.lexists(candidate):
                info = candidate.lstat()
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                        or info.st_nlink != 1 or not not_writable_by_others(candidate, info)):
                    raise UnsafePath('Unsafe native state index path.')
        with closing(sqlite3.connect(db.as_uri() + '?mode=ro', uri=True, timeout=1)) as con:
            con.execute('PRAGMA query_only=ON')
            result.extend(con.execute('SELECT id, rollout_path, archived FROM threads WHERE id=?', (sid,)))
        if require_present and not result:
            raise UnsafePath('Thread missing from native state index; archive state uncertain.')
    return result


def _startup_guard(binary: str) -> None:
    """An unidentified Codex startup may acquire a thread after the fd snapshot."""
    expected = os.stat(binary)
    ancestors = activity._ancestors()
    for base in activity.PROC.iterdir():
        if not base.name.isdigit() or int(base.name) in ancestors:
            continue
        recognized = False
        try:
            if base.stat().st_uid != os.geteuid() or activity._exited(base):
                continue
            executable = (base / 'exe').stat()
            executable_name = Path(os.readlink(base / 'exe')).name
            if ((expected.st_dev, expected.st_ino) != (executable.st_dev, executable.st_ino)
                    and executable_name != 'codex'):
                continue
            recognized = True
            environment = dict(part.split(b'=', 1) for part in (base / 'environ').read_bytes().split(b'\0')
                               if b'=' in part)
            configured = os.fsdecode(environment.get(b'CODEX_HOME', b''))
            process_root = Path(configured) if configured else Path.home() / '.codex'
            if not process_root.is_absolute():
                raise UnsafePath(f'Codex process {base.name} has uncertain CODEX_HOME.')
            if process_root.resolve() != paths.codex_dir().resolve():
                continue
            argv = (base / 'cmdline').read_bytes().rstrip(b'\0').decode(errors='replace').split('\0')
            if ((len(argv) >= 3 and argv[1] in ('resume', 'fork') and is_uuid(argv[2]))
                    or (len(argv) >= 4 and argv[1:3] == ['exec', 'resume'] and is_uuid(argv[3]))):
                continue  # Exact session IDs are checked by Activity.evidence.
            if 'app-server' in argv and '--managed-daemon' in argv:
                from .codex_idle import _process
                _process(int(base.name))  # verified ready daemon; never stop it
                continue
            if argv[1:] == ['app-server', 'daemon', 'pid-update-loop']:
                from .codex_idle import _process
                record = read_json_nofollow(paths.codex_dir() / 'app-server-daemon/daemon.pid')
                child = record['pid']
                verified = _process(child)
                parent = (activity.PROC / str(child) / 'stat').read_text().rsplit(') ', 1)[1].split()[1]
                if parent != base.name or verified[1] != identity(executable):
                    raise UnsafePath('Managed Codex supervisor identity is uncertain.')
                continue
            raise UnsafePath(f'Codex process {base.name} has no exact session UUID; startup state uncertain.')
        except FileNotFoundError:
            if recognized and base.exists() and not activity._exited(base):
                raise UnsafePath(f'Codex process {base.name} startup identity is unavailable.') from None
            continue
        except PermissionError:
            if recognized:
                raise UnsafePath(f'Codex process {base.name} startup identity is unreadable.') from None
            # Activity's complete-process requirement covers unreadable holders.
            continue


@dataclass(frozen=True)
class PermanentPlan:
    session: Session
    root: Path
    binary: str
    fingerprint: str


def prepare(session: Session) -> PermanentPlan:
    if session.agent != 'codex' or not is_uuid(session.session_id) or not session.native_archived:
        raise UnsafePath('Permanent deletion requires one already natively archived Codex UUID.')
    if session.error or session.status in ('active', 'uncertain', 'recent'):
        raise UnsafePath(session.error or f'Session status is {session.status}; permanent deletion blocked.')
    root = paths.codex_dir()
    _safe_root(root)
    if Path(session.storage_root) != root or '..' in Path(session.storage_path).parts:
        raise UnsafePath('Session store changed or contains traversal.')
    binary = find_binary('codex')
    if not binary:
        raise UnsafePath('Native codex CLI not found.')
    binary = os.path.abspath(binary)  # Help, identity checks and cwd=/ execution must use the same executable.
    help_text = _run([binary, 'delete', '--help'], root, timeout=10)
    if not (re.search(r'Usage:\s+codex delete\b', help_text) and '<SESSION>' in help_text
            and re.search(r'(?<![\w-])--force\b', help_text)):
        raise UnsafePath('Installed Codex does not advertise delete <SESSION> and --force.')
    _startup_guard(binary)
    rows = _inventory(root)
    matches = [s for s in rows if s.session_id.lower() == session.session_id.lower()]
    if (len(matches) != 1 or not matches[0].native_archived
            or matches[0].storage_path != session.storage_path):
        raise UnsafePath('Exact native archive state changed or is ambiguous; refresh.')
    indexed = _index(root, session.session_id)
    if indexed and indexed != [(session.session_id, session.storage_path, 1)]:
        raise UnsafePath('Native index disagrees with archived rollout; deletion blocked.')
    affected = {session.session_id}
    while True:
        expanded = affected | {s.session_id for s in rows if s.parent_session_id in affected}
        if expanded == affected:
            break
        affected = expanded
    related = [s for s in rows if s.session_id in affected]
    act = activity.snapshot()
    bridge = BridgeState()
    jobs = bridge.session_jobs()
    proofs = []
    for row in related:
        item = trash._plan_item(row, act, time.time(), None, bridge, jobs, False, codex_permanent=True)
        if item.blockers:
            raise UnsafePath(f'{row.selector}: ' + '; '.join(item.blockers))
        # Archive's stale-handle exemption relies on archive-specific writer-lock
        # evidence. Do not silently extend it to irreversible deletion.
        if item.activity_proof:
            raise UnsafePath('Managed daemon still holds this thread; permanent deletion blocked.')
        proofs.append((row.session_id, row.parent_session_id, row.native_archived,
                       [(e.path, e.identity) for e in item.entries]))
    fingerprint = hashlib.sha256(json.dumps((str(root), identity(root.stat()),
                                           identity(os.stat(binary)), indexed, proofs), sort_keys=True).encode()).hexdigest()
    return PermanentPlan(session, root, binary, fingerprint)


def execute(plan: PermanentPlan) -> None:
    """Call only following external confirmation of this specific plan."""
    fresh = prepare(plan.session)
    if fresh != plan:
        raise UnsafePath('Thread, descendants, binary or store changed since confirmation; retry.')
    _run([fresh.binary, 'delete', '--force', fresh.session.session_id], fresh.root)
    # A zero status alone does not establish success. Index and files must agree.
    if any(s.session_id.lower() == fresh.session.session_id.lower() for s in _inventory(fresh.root)):
        raise UnsafePath('Native Codex returned success but thread remains; outcome unconfirmed.')
    indexed = _index(fresh.root, fresh.session.session_id, require_present=False)
    if indexed:
        raise UnsafePath('Native index still contains thread; outcome unconfirmed.')
