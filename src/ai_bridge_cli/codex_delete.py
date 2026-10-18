"""TUI-only permanent deletion, delegated entirely to the native Codex CLI.

No native files are written here. Plans consent to exact session UUIDs
and their checked descendants; native thread/delete owns reference checks
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
    affected: tuple[Session, ...] = ()


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
    return PermanentPlan(session, root, binary, fingerprint, tuple(related))


def execute(plan: PermanentPlan) -> None:
    """Call only following external confirmation of this specific plan."""
    fresh = prepare(plan.session)
    if fresh != plan:
        raise UnsafePath('Thread, descendants, binary or store changed since confirmation; retry.')
    _run([fresh.binary, 'delete', '--force', fresh.session.session_id], fresh.root)
    # A zero status alone does not establish success. Index and files must agree.
    affected = {s.session_id.lower() for s in fresh.affected}
    if any(s.session_id.lower() in affected for s in _inventory(fresh.root)):
        raise UnsafePath('Native Codex returned success but thread remains; outcome unconfirmed.')
    if any(_index(fresh.root, sid, require_present=False) for sid in affected):
        raise UnsafePath('Native index still contains thread; outcome unconfirmed.')


@dataclass(frozen=True)
class ForcePlan:
    sessions: tuple[Session, ...]
    affected: tuple[Session, ...]
    root: Path
    binary: str
    fingerprint: str
    processes: tuple[str, ...]
    inspection_note: str | None
    selected: tuple[Session, ...]


def prepare_force(sessions: list[Session]) -> ForcePlan:
    """Consent to native session-scoped deletion, including active threads.

    Live content is allowed to change. Consent pins UUIDs, storage inodes,
    ancestry, index locations, executable and observed process identities.
    Native thread/delete alone arbitrates busy threads and reference safety.
    No process-inspection override is granted to filesystem deletion or signals.
    """
    if not sessions or any(s.agent != 'codex' or not is_uuid(s.session_id) or s.error for s in sessions):
        raise UnsafePath('Native force deletion requires exact, readable Codex UUIDs.')
    root = paths.codex_dir()
    _safe_root(root)
    binary = find_binary('codex')
    if not binary:
        raise UnsafePath('Native codex CLI not found. Install a Codex version supporting delete --force UUID.')
    binary = os.path.abspath(binary)
    help_text = _run([binary, 'delete', '--help'], root, timeout=10)
    if not (re.search(r'Usage:\s+codex delete\b', help_text) and '<SESSION>' in help_text
            and re.search(r'(?<![\w-])--force\b', help_text)):
        raise UnsafePath('Installed Codex does not advertise delete <SESSION> and --force; update Codex.')
    rows = _inventory(root)
    selected = {s.session_id.lower(): s for s in sessions}
    for sid, session in selected.items():
        matches = [s for s in rows if s.session_id.lower() == sid]
        if (Path(session.storage_root) != root or len(matches) != 1
                or matches[0].storage_path != session.storage_path):
            raise UnsafePath(f'{session.selector}: native session location changed or is ambiguous; refresh.')
    affected = set(selected)
    while True:
        expanded = affected | {s.session_id.lower() for s in rows
                               if (s.parent_session_id or '').lower() in affected}
        if expanded == affected:
            break
        affected = expanded
    related = sorted((s for s in rows if s.session_id.lower() in affected), key=lambda s: s.session_id)
    if len(related) != len(affected):
        raise UnsafePath('Ambiguous descendant UUIDs; refresh and inspect the native inventory.')
    act = activity.snapshot()
    state = BridgeState()
    bridge_note = None
    try:
        jobs = state.session_jobs()
        if state.problems:
            bridge_note = 'Bridge job inventory incomplete; native Codex decides deletion safety.'
    except (OSError, ValueError):
        jobs = {}
        bridge_note = 'Bridge job inventory unavailable; native Codex decides deletion safety.'
    proofs, processes = [], set()
    for row in related:
        if row.error:
            raise UnsafePath(f'{row.selector}: {row.error}')
        storage_safe(row)
        info = Path(row.storage_path).lstat()
        if info.st_nlink != 1 or not not_writable_by_others(Path(row.storage_path), info):
            raise UnsafePath(f'{row.selector}: hard-linked or shared-writable rollout refused.')
        indexed = _index(root, row.session_id)
        if indexed and indexed != [(row.session_id, row.storage_path, int(row.native_archived))]:
            raise UnsafePath(f'{row.selector}: native index disagrees with rollout; refresh in Codex.')
        key = info.st_dev, info.st_ino
        pids = set(act.file_holders.get(key, ()))
        if key in act.open_files:
            pids.add(act.open_files[key])
        if row.session_id.lower() in act.cmdline_ids:
            pids.add(act.cmdline_ids[row.session_id.lower()])
        observed, job_proofs = [], []
        for job in jobs.get(('codex', row.session_id.lower()), []):
            if job.get('status') not in ('running', 'cleanup_incomplete', 'lost'):
                continue
            view = state.job_view(job, detail=True)
            recorded = [(view.get('runner_pid'), view.get('runner_identity_verified')),
                        (view.get('target_pid'), view.get('target_identity_verified'))]
            recorded.extend((d['pid'], d['identity_verified']) for d in view.get('observed_descendants', []))
            job_proofs.append((job['id'], recorded))
            for pid, verified in recorded:
                if pid is None:
                    continue
                if verified:
                    pids.add(pid)
                processes.add(f"{row.selector}: bridge job {job['id']} · saved PID {pid} · " +
                              ('verified live lineage' if verified else 'not verified live; never signalled by this action'))
            if view.get('process_record') == 'unreadable':
                bridge_note = 'Bridge process lineage unreadable; native Codex decides deletion safety.'
        for pid in sorted(pids):
            try:
                cmd = one_line((activity.PROC / str(pid) / 'cmdline').read_bytes()
                               .replace(b'\0', b' ').decode(errors='replace'), 1000)
                start = activity.proc_start_ticks(pid)
            except OSError:
                cmd, start = '(command unavailable; native API decides safety)', None
            processes.add(f'{row.selector}: PID {pid} · {cmd}')
            observed.append((pid, start, cmd))
        proofs.append((row.session_id, row.parent_session_id, row.storage_path, row.native_archived,
                       (info.st_dev, info.st_ino, info.st_uid, info.st_mode, info.st_nlink), indexed, observed, job_proofs))
    fingerprint = hashlib.sha256(json.dumps((str(root), identity(os.stat(binary)),
                                            (root.stat().st_dev, root.stat().st_ino), proofs),
                                           sort_keys=True).encode()).hexdigest()
    # Selected descendants are covered by their selected ancestor's native call.
    parents = {s.session_id.lower(): (s.parent_session_id or '').lower() for s in related}
    for sid in parents:
        current, seen = sid, set()
        while current in parents:
            if current in seen:
                raise UnsafePath('Cyclic native ancestry; deletion refused.')
            seen.add(current)
            current = parents[current]
    roots = []
    for sid, session in sorted(selected.items()):
        parent, seen = parents.get(sid), {sid}
        covered = False
        while parent:
            if parent in seen:
                raise UnsafePath('Cyclic native ancestry; deletion refused.')
            if parent in selected:
                covered = True
                break
            seen.add(parent)
            parent = parents.get(parent)
        if not covered:
            roots.append(session)
    return ForcePlan(tuple(roots), tuple(related), root, binary, fingerprint,
                     tuple(sorted(processes)), '; '.join(n for n in (act.incompleteness(), bridge_note) if n) or None,
                     tuple(selected.values()))


def execute_force(plan: ForcePlan) -> list[str]:
    """Execute only after grouped confirmation; never kill a daemon or edit stores."""
    fresh = prepare_force(list(plan.sessions))
    if fresh.fingerprint != plan.fingerprint:
        raise UnsafePath('Affected sessions, process identities, binary or native index changed; confirm a fresh preview.')
    individual = [(s, prepare_force([s])) for s in fresh.sessions]
    # Building per-root guards must not authorize changes since grouped consent.
    if prepare_force(list(plan.sessions)).fingerprint != plan.fingerprint:
        raise UnsafePath('Grouped native scope changed while preparing calls; confirm a fresh preview.')
    removed = []
    for session, expected in individual:
        try:
            if prepare_force([session]).fingerprint != expected.fingerprint:
                raise UnsafePath('Native deletion scope changed; confirm a fresh preview.')
            _run([fresh.binary, 'delete', '--force', session.session_id], fresh.root)
            remaining = _inventory(fresh.root)
            descendants = {session.session_id.lower()}
            while True:
                expanded = descendants | {s.session_id.lower() for s in plan.affected
                                          if (s.parent_session_id or '').lower() in descendants}
                if expanded == descendants:
                    break
                descendants = expanded
            if any(s.session_id.lower() in descendants for s in remaining):
                raise UnsafePath('Native Codex returned success but affected threads remain; outcome unconfirmed.')
            if any(_index(fresh.root, sid, require_present=False) for sid in descendants):
                raise UnsafePath('Native index still contains affected threads; outcome unconfirmed.')
            removed.append(session.selector)
        except (OSError, ValueError) as error:
            raise UnsafePath(f'{error} Completed native roots: {", ".join(removed) or "none"}. '
                             'Batch is not atomic; refresh before retrying.') from error
    return removed
