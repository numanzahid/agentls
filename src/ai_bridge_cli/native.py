"""Open (resume) or fork a session in its native CLI, after a preflight.

Preflight never starts a turn or writes history: it checks the stored file,
the project directory, live use, and the installed CLI's advertised flags.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import activity as activity_mod
from . import paths
from .fsutil import UnsafePath, lstat_at, pinned_parent, private_primary_group
from .model import Session

_HELP_CACHE: dict[tuple, str] = {}


class PreflightError(ValueError):
    pass


def find_binary(agent: str) -> str | None:
    override = os.environ.get(f"AI_BRIDGE_{agent.upper()}_BIN", "").strip()
    if override:
        return shutil.which(override) or (override if os.access(override, os.X_OK) else None)
    if agent in ("claude", "codex"):
        return shutil.which(agent)
    for name in ("cursor-agent", "agent"):
        found = shutil.which(name)
        if not found:
            continue
        if name == "agent" and "cursor" not in os.path.realpath(found).lower():
            if "cursor" not in probe_help([found, "--help"]).lower():
                continue  # a generic "agent" binary that is not Cursor's
        return found
    return None


def probe_help(argv: list[str], timeout: float = 15) -> str:
    """Installed CLI help text, cached per binary identity for this process."""
    try:
        info = os.stat(argv[0])
        key = (tuple(argv), info.st_ino, info.st_mtime_ns)
    except OSError:
        key = None
    if key and key in _HELP_CACHE:
        return _HELP_CACHE[key]
    env = native_env()
    env.update(TERM="dumb", NO_COLOR="1")
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env,
                              stdin=subprocess.DEVNULL, cwd="/")
    except (OSError, subprocess.SubprocessError) as error:
        raise PreflightError(f"Cannot run {Path(argv[0]).name} to check capabilities: {error}") from None
    if proc.returncode != 0:
        shown = " ".join([Path(argv[0]).name, *argv[1:]])
        raise PreflightError(f"`{shown}` exited {proc.returncode}; native capability unverified.")
    text = (proc.stdout or "") + (proc.stderr or "")
    if key:
        _HELP_CACHE[key] = text
    return text


def native_env() -> dict[str, str]:
    # Bridge/CLI settings (some may reference secrets) are not for native CLIs.
    return {k: v for k, v in os.environ.items()
            if not k.startswith("AI_BRIDGE_") and k != "CREDENTIALS_DIRECTORY"}


def storage_safe(session: Session) -> os.stat_result:
    """The stored file is a regular, user-owned file reached without symlinks below its root."""
    path = Path(session.storage_path)
    try:
        with pinned_parent(Path(session.storage_root), path) as (fd, name):
            info = lstat_at(fd, name)
    except (UnsafePath, ValueError) as error:
        raise PreflightError(f"Session storage rejected: {error}") from None
    except FileNotFoundError:
        raise PreflightError("Session storage no longer exists; refresh the list.") from None
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o002:
        raise PreflightError("Session storage has unsafe type, ownership or permissions.")
    if info.st_mode & 0o020 and not private_primary_group(os.geteuid(), info.st_gid):
        raise PreflightError("Session storage is writable by a shared group.")
    return info


@dataclass
class LaunchPlan:
    agent: str
    session_id: str
    action: str
    argv: list[str]
    cwd: str
    storage_path: str
    storage_identity: tuple
    warnings: list[str] = field(default_factory=list)
    history_rewritten: bool = False
    method: str = "native_cli"

    def to_json(self) -> dict:
        row = asdict(self)
        row.pop("storage_identity")
        row["note"] = "Preflight only: the native CLI may still reject the saved state."
        return row


def plan(session: Session, action: str = "open", activity: activity_mod.Activity | None = None) -> LaunchPlan:
    if action not in ("open", "fork"):
        raise PreflightError("Action must be open or fork.")
    if session.error:
        raise PreflightError(session.error)
    if session.native_archived:
        raise PreflightError("Natively archived Codex session: restore it first (sessions trash restore).")
    activity = activity or activity_mod.snapshot()
    evidence = activity.evidence(session.agent, session.session_id, session.storage_path)
    if evidence or session.status in ("active", "uncertain"):
        raise PreflightError(f"Session appears to be in use ({evidence or session.status_note}); "
                             "opening it twice can corrupt or fork its history.")
    if session.agent == "cursor" and not paths.cursor_home_is_default():
        raise PreflightError("AI_BRIDGE_CURSOR_HOME points away from ~/.cursor; Cursor CLI would open "
                             "a different store. Unset it to resume Cursor chats.")
    info = storage_safe(session)
    cwd = session.cwd
    if not session.resumable or not cwd or not os.path.isdir(cwd):
        raise PreflightError(session.resume_note or "Saved project directory is unavailable.")
    binary = find_binary(session.agent)
    if not binary:
        raise PreflightError(f"{session.agent} CLI not found on PATH.")
    warnings = []
    if session.status == "recent":
        warnings.append("Modified moments ago; make sure no other terminal is using it.")
    sid = session.session_id
    if session.agent == "codex":
        if action == "fork":
            text = probe_help([binary, "fork", "--help"])
            if "SESSION_ID" not in text:
                raise PreflightError("Installed codex does not advertise `fork SESSION_ID`.")
            argv = [binary, "fork", sid]
        else:
            text = probe_help([binary, "resume", "--help"])
            if "SESSION_ID" not in text:
                raise PreflightError("Installed codex does not advertise `resume SESSION_ID`.")
            argv = [binary, "resume"]
            if "--include-non-interactive" in text:
                argv.append("--include-non-interactive")
            elif session.mode == "noninteractive":
                raise PreflightError("Installed codex cannot resume non-interactive (exec) sessions.")
            argv.append(sid)
    elif session.agent == "claude":
        text = probe_help([binary, "--help"])
        if "--resume" not in text:
            raise PreflightError("Installed claude does not advertise --resume.")
        argv = [binary, "--resume", sid]
        if action == "fork":
            if "--fork-session" not in text:
                raise PreflightError("Installed claude does not advertise --fork-session.")
            argv.append("--fork-session")
    else:
        if action == "fork":
            raise PreflightError("Cursor CLI has no native fork; the original chat is unchanged.")
        text = probe_help([binary, "--help"])
        if "--resume" not in text:
            raise PreflightError("Installed Cursor CLI does not advertise --resume.")
        argv = [binary, "--resume", sid]
    return LaunchPlan(agent=session.agent, session_id=sid, action=action, argv=argv, cwd=cwd,
                      storage_path=session.storage_path,
                      storage_identity=(info.st_dev, info.st_ino), warnings=warnings)


def recheck(plan_: LaunchPlan, session: Session) -> None:
    """Immediately before launch: same file, still not in use."""
    info = storage_safe(session)
    if (info.st_dev, info.st_ino) != plan_.storage_identity:
        raise PreflightError("Session storage was replaced since preflight; refresh and retry.")
    evidence = activity_mod.snapshot().evidence(session.agent, session.session_id, session.storage_path)
    if evidence:
        raise PreflightError(f"Session became active ({evidence}).")
    if not os.path.isdir(plan_.cwd):
        raise PreflightError("Project directory disappeared.")


def run(plan_: LaunchPlan) -> int:
    """Run attached to the current terminal and return its exit status."""
    try:
        return subprocess.call(plan_.argv, cwd=plan_.cwd, env=native_env())
    except OSError as error:
        raise PreflightError(f"Could not start {plan_.argv[0]}: {error}") from None


def exec_(plan_: LaunchPlan):
    os.chdir(plan_.cwd)
    os.execvpe(plan_.argv[0], plan_.argv, native_env())
