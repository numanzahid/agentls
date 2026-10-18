"""Fail-closed native process identity checks and legacy TUI stop/archive flow.

No PID is signalled by number: Linux pidfds pin the checked process identity.
Unsupported launch shapes and shared processes are deliberately refused.
"""
from __future__ import annotations

import os
import select
import signal
import stat
from dataclasses import dataclass, replace
from pathlib import Path

from .. import activity, trash
from ..fsutil import UnsafePath
from ..native import find_binary


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    start: str
    executable: tuple
    argv: tuple[bytes, ...]
    inode: tuple[int, int]


@dataclass
class StopPlan:
    deletion: trash.DeletePlan
    process: ProcessIdentity
    idle: trash.DeletePlan


def inspect(pid: int, session) -> ProcessIdentity:
    """Accept only an exclusive native resume of an exact session, owned by us."""
    if not hasattr(os, 'pidfd_open') or not hasattr(signal, 'pidfd_send_signal'):
        raise UnsafePath('Safe process signalling is unavailable on this system.')
    if pid in activity._ancestors() or pid <= 1:
        raise UnsafePath('Refusing this process or an ancestor.')
    base = activity.PROC / str(pid)
    start = activity.proc_start_ticks(pid)
    if not start or base.stat().st_uid != os.geteuid():
        raise UnsafePath('Process exited or is not owned by you.')
    uid_line = next((line for line in (base / 'status').read_text().splitlines()
                     if line.startswith('Uid:')), '')
    if len(uid_line.split()) != 5 or any(int(uid) != os.geteuid() for uid in uid_line.split()[1:]):
        raise UnsafePath('Process user identities do not all match your account.')
    executable = (base / 'exe').stat()
    binary = find_binary(session.agent)
    if not binary:
        raise UnsafePath('Native agent executable cannot be verified.')
    native = Path(binary).resolve()
    expected = native.stat()
    if ((executable.st_dev, executable.st_ino) != (expected.st_dev, expected.st_ino)
            or expected.st_mode & 0o022 or expected.st_uid not in (0, os.geteuid())):
        raise UnsafePath('Process is not the verified native agent executable.')
    with native.open('rb') as stream:
        if stream.read(4) != b'\x7fELF':
            raise UnsafePath('Wrapped or interpreted agent processes cannot be safely terminated.')
    argv = tuple((base / 'cmdline').read_bytes().rstrip(b'\0').split(b'\0'))
    expected_names = {'codex': ('codex',), 'claude': ('claude',), 'cursor': ('agent', 'cursor-agent')}[session.agent]
    verb = b'resume' if session.agent == 'codex' else b'--resume'
    if (len(argv) != 3 or Path(os.fsdecode(argv[0])).name not in expected_names
            or argv[1:] != (verb, session.session_id.encode())):
        raise UnsafePath('Cannot prove an exclusive native resume of this exact session; shared daemons and other launch modes are refused.')
    target = Path(session.storage_path).stat(follow_symlinks=False)
    inode = (target.st_dev, target.st_ino)
    opened = False
    for fd in (base / 'fd').iterdir():
        try:
            info = fd.stat()
        except FileNotFoundError as error:
            raise UnsafePath('Process files changed during inspection; retry.') from error
        if (info.st_dev, info.st_ino) == inode:
            opened = True
        elif not (fd.name in ('0', '1', '2') and
                  (stat.S_ISCHR(info.st_mode) or stat.S_ISFIFO(info.st_mode))):
            raise UnsafePath('Process holds another file, session or socket; shared impact cannot be proven safe.')
    # An agent with workers/children can have undisclosed session or MCP impact.
    for task in (base / 'task').iterdir():
        if (task / 'children').read_text().strip():
            raise UnsafePath('Process has child processes; their impact cannot be proven safe.')
    if not opened or activity.proc_start_ticks(pid) != start:
        raise UnsafePath('Exact open session inode or process identity changed.')
    return ProcessIdentity(pid, start, (executable.st_dev, executable.st_ino,
                                       executable.st_ctime_ns), argv, inode)


def prepare(plan: trash.DeletePlan) -> StopPlan:
    if len(plan.items) != 1 or len(plan.sessions) != 1:
        raise UnsafePath('Process termination requires one selected session; shared multi-session impact is refused.')
    session = plan.sessions[0]
    if session.agent != 'codex' or plan.allow_uninspectable:
        raise UnsafePath('Safe process termination is supported only for a verified native Codex session.')
    act = activity.snapshot()
    if not act.complete:
        raise UnsafePath('Running processes cannot be fully inspected.')
    info = Path(session.storage_path).stat(follow_symlinks=False)
    pid = act.open_files.get((info.st_dev, info.st_ino))
    if pid is None:
        raise UnsafePath('No verifiable process holds the selected rollout open.')
    if act.file_holders.get((info.st_dev, info.st_ino), {pid}) != {pid}:
        raise UnsafePath('Multiple processes hold this session; shared impact is refused.')
    process = inspect(pid, session)
    # Remove only evidence belonging to the verified process for eligibility.
    # Locks are NOT removed: ownership is not tracked by the activity snapshot.
    idle_act = replace(act,
                       open_files={k: v for k, v in act.open_files.items() if v != pid},
                       cmdline_ids={k: v for k, v in act.cmdline_ids.items() if v != pid})
    idle = trash.build_plan([replace(session, status='idle', status_note=None)], act=idle_act)
    if not all(item.allowed for item in idle.items):
        raise UnsafePath('Other deletion safeguards block this session: ' + '; '.join(idle.items[0].blockers))
    return StopPlan(plan, process, idle)


def execute(plan: StopPlan, timeout: float = 3.0) -> dict:
    """Recheck consent, SIGTERM the pinned process, wait, then ordinary deletion."""
    fresh = trash.build_plan(plan.deletion.sessions)
    if fresh.plan_id != plan.deletion.plan_id:
        raise UnsafePath('Deletion plan changed; nothing was stopped or deleted. Retry.')
    checked = prepare(fresh)
    if checked.process != plan.process or checked.idle.plan_id != plan.idle.plan_id:
        raise UnsafePath('Process identity or deletion plan changed; nothing was stopped or deleted.')
    pidfd = os.pidfd_open(plan.process.pid)
    try:
        # Pin first, then recheck PID/start/executable/argv/open inode immediately
        # before signalling. PID reuse cannot redirect a pidfd signal.
        final = prepare(trash.build_plan(plan.deletion.sessions))
        if final.process != plan.process or final.idle.plan_id != plan.idle.plan_id:
            raise UnsafePath('Process identity or deletion plan changed; nothing was stopped or deleted.')
        signal.pidfd_send_signal(pidfd, signal.SIGTERM)
        poller = select.poll()
        poller.register(pidfd, select.POLLIN)
        if not poller.poll(max(0, int(timeout * 1000))):
            raise UnsafePath(f'Process {plan.process.pid} remains after SIGTERM; nothing deleted. No SIGKILL sent.')
    finally:
        os.close(pidfd)
    fresh = trash.build_plan(plan.idle.sessions)
    if fresh.plan_id != plan.idle.plan_id or len(fresh.allowed) != len(fresh.items):
        raise UnsafePath('Process exited, but deletion safeguards or files changed; nothing deleted. Retry.')
    return trash.execute(fresh)
