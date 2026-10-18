"""Independent stop and recoverable force removal. No arbitrary PID inputs."""
from __future__ import annotations

import os
import select
import signal
import stat
from pathlib import Path
from dataclasses import dataclass, replace

from . import activity, trash
from .mcp import McpState, uncertain
from .fsutil import UnsafePath, lstat_at, pinned_parent
from .model import Session
from .native import storage_safe
from .scanners import Scanner
from .tui.process_delete import ProcessIdentity, inspect


@dataclass(frozen=True)
class StopTarget:
    session: Session
    process: ProcessIdentity | None = None
    details: tuple[str, ...] = ()


@dataclass(frozen=True)
class SessionStopPlan:
    selected: tuple[Session, ...]
    affected: tuple[Session, ...]
    targets: tuple[StopTarget, ...]


def _scope(sessions: list[Session]) -> list[Session]:
    agents = {s.agent for s in sessions}
    result = Scanner().scan(agents=tuple(sorted(agents)))
    if result.errors or any(s.error for s in result.sessions):
        raise UnsafePath('Native inventory is incomplete or ambiguous; refresh and inspect unreadable chats.')
    if 'codex' in agents:
        from .codex_delete import _inventory
        from .paths import codex_dir
        result.sessions = [s for s in result.sessions if s.agent != 'codex'] + _inventory(codex_dir())
    keys = {s.key for s in sessions}
    for selected in sessions:
        matches = [s for s in result.sessions if s.key == selected.key]
        if len(matches) != 1 or matches[0].storage_path != selected.storage_path:
            raise UnsafePath(f'{selected.selector}: session changed; refresh.')
    while True:
        expanded = keys | {s.key for s in result.sessions if (s.agent, s.parent_session_id) in keys}
        if expanded == keys:
            break
        keys = expanded
    related = sorted((s for s in result.sessions if s.key in keys), key=lambda s: s.key)
    if len(related) != len(keys):
        raise UnsafePath('Duplicate descendant session identities; inspect native storage before stopping.')
    return related


def _related_keys(row: Session) -> set[tuple[int, int]]:
    keys = set()
    for path, required in trash._related(row):
        try:
            with pinned_parent(Path(row.storage_root), path) as (fd, leaf):
                info = lstat_at(fd, leaf)
                keys.add((info.st_dev, info.st_ino))
                if stat.S_ISDIR(info.st_mode):
                    keys.update((e[2], e[3]) for e in trash._inspect_dir(fd, leaf)['entries'])
        except FileNotFoundError:
            if required:
                raise UnsafePath(f'{row.selector}: session storage disappeared; refresh.') from None
    return keys


def prepare_stop(sessions: list[Session]) -> SessionStopPlan:
    if not sessions:
        raise UnsafePath('Select at least one session.')
    related = _scope(sessions)
    state = McpState()
    jobs = state.session_jobs()
    if state.problems or state.unattributed():
        raise UnsafePath('MCP jobs are unreadable or unattributed; inspect them in the MCP server before stopping sessions.')
    act = activity.snapshot()
    if not act.complete:
        raise UnsafePath('Process inspection incomplete: ' + (act.incompleteness() or '') + '. Close the listed processes and refresh.')
    targets = []
    seen_pids = set()
    for row in related:
        info = storage_safe(row)
        busy = [j['id'] for j in jobs.get((row.agent, row.session_id.lower()), []) if uncertain(j)]
        if busy:
            raise UnsafePath(f'{row.selector}: MCP job {", ".join(busy)} may still be running; '
                             'stop it through the MCP server, then refresh.')
        keys = _related_keys(row) | {(info.st_dev, info.st_ino)}
        pids = set()
        for key in keys:
            pids.update(act.file_holders.get(key, ()))
            if key in act.open_files:
                pids.add(act.open_files[key])
            if key in act.locked:
                raise UnsafePath(f'{row.selector}: a related session file is locked; close its native client and refresh.')
        for index in (act.cmdline_ids, act.claude_live if row.agent == 'claude' else {}):
            if row.session_id.lower() in index:
                pids.add(index[row.session_id.lower()])
        if len(pids) > 1:
            raise UnsafePath(f'{row.selector}: multiple process holders; shared impact cannot be proven safe.')
        if pids:
            process = inspect(pids.pop(), row)
            if process.pid in seen_pids:
                raise UnsafePath('One process serves multiple sessions; shared daemon stop is refused.')
            seen_pids.add(process.pid)
            targets.append(StopTarget(row, process=process,
                                      details=(f'PID {process.pid} · ' + ' '.join(os.fsdecode(a) for a in process.argv),)))
        elif act.evidence(row.agent, row.session_id, row.storage_path):
            raise UnsafePath(f'{row.selector}: locked or ambiguous live session. Close its native client and refresh.')
    return SessionStopPlan(tuple(sessions), tuple(related), tuple(targets))


def _signature(plan):
    return (tuple((s.key, s.storage_path, s.parent_session_id) for s in plan.affected),
            tuple((t.session.key, t.process, t.details) for t in plan.targets))


def execute_stop(plan: SessionStopPlan, timeout: float = 3.0) -> list[str]:
    fresh = prepare_stop(list(plan.selected))
    if _signature(fresh) != _signature(plan):
        raise UnsafePath('Session scope or process identity changed; confirm a fresh preview.')
    # Pin and validate every native target before the first signal.
    handles = []
    completed = []
    try:
        for target in fresh.targets:
            if target.process:
                handles.append((target, os.pidfd_open(target.process.pid)))
        if _signature(prepare_stop(list(plan.selected))) != _signature(plan):
            raise UnsafePath('Process identity changed after pidfd pinning; nothing stopped.')
        for target, fd in handles:
            if inspect(target.process.pid, target.session) != target.process:
                raise UnsafePath('Process identity changed before signal; retry.')
            signal.pidfd_send_signal(fd, signal.SIGTERM)
            poller = select.poll()
            poller.register(fd, select.POLLIN)
            if not poller.poll(max(0, int(timeout * 1000))):
                raise UnsafePath(f'PID {target.process.pid} remains after SIGTERM. No SIGKILL sent; files retained. Inspect the native client.')
            completed.append(target.session.selector)
    except (OSError, ValueError, RuntimeError) as error:
        raise UnsafePath(f'{error} Completed stops: {", ".join(completed) or "none"}. Refresh before retrying.') from error
    finally:
        for _, fd in handles:
            os.close(fd)
    return completed


@dataclass(frozen=True)
class QuarantinePlan:
    stop: SessionStopPlan
    paths: tuple[str, ...]


def prepare_quarantine(sessions: list[Session]) -> QuarantinePlan:
    if any(s.agent == 'codex' for s in sessions):
        raise UnsafePath('Select Codex chats separately for native force deletion; Claude/Cursor use recoverable quarantine.')
    stop = prepare_stop(sessions)
    paths = tuple(sorted({str(path) + ('/ (all contents, including embedded subagent transcripts)' if path.is_dir() else '')
                          for row in stop.affected for path, _ in trash._related(row) if os.path.lexists(path)}))
    return QuarantinePlan(stop, paths)


def execute_quarantine(plan: QuarantinePlan) -> dict:
    execute_stop(plan.stop)
    expected = tuple((s.key, s.storage_path, s.parent_session_id) for s in plan.stop.affected)
    current = tuple((s.key, s.storage_path, s.parent_session_id) for s in _scope(list(plan.stop.selected)))
    if current != expected:
        raise UnsafePath('Stop completed, but descendant sessions changed; confirm a new quarantine preview.')
    # A stop can flush new writes. Keep the normal recent-write guard; never
    # silently move a still-changing transcript or SQLite/WAL after cancellation.
    rows = [replace(s, status='idle', status_note=None, active_job_ids=[]) for s in plan.stop.affected]
    deletion = trash.build_plan(rows)
    if len(deletion.allowed) != len(deletion.items):
        raise UnsafePath('Stop completed; quarantine blocked: ' + '; '.join(
            b for item in deletion.items for b in item.blockers) + '. Wait 120s, refresh and retry ordinary delete.')
    return trash.execute(deletion)
