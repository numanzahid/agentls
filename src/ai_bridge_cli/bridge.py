"""Optional integration with an ai-bridge MCP server on this account.

Two strictly separated paths:

* BridgeState: READ-ONLY views of the server's job and task records. Status is
  derived in memory (the server's own Jobs.list() persists refreshes and may
  backfill provenance, so it is deliberately not used here).
* BridgeRuntime: mutating operations (start/continue a headless turn, stop a
  job, create/clean up a task) run the *installed server's own code* in a
  subprocess using its verified active release and its own virtualenv. Nothing
  from ai_bridge is imported into this process, and the server is never
  restarted or reconfigured.
"""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import time
from pathlib import Path

from . import paths
from .activity import PROC
from .fsutil import UnsafePath, check_private, not_writable_by_others, read_bytes_nofollow
from .textsafe import is_uuid

JOB_ID = re.compile(r"[a-z0-9-]{4,40}")
TASK_ID = re.compile(r"task-[0-9a-f]{24}")
UNCERTAIN = {"running", "cleanup_incomplete", "lost"}
MAX_JOB_BYTES = 1 << 20
MAX_OUTPUT_BYTES = 8 << 20

try:
    BOOT_ID = (PROC / "sys/kernel/random/boot_id").read_text().strip()
except OSError:
    BOOT_ID = None


class BridgeUnavailable(RuntimeError):
    pass


def process_identity(pid) -> dict | None:
    try:
        fields = (PROC / f"{int(pid)}/stat").read_text().rsplit(") ", 1)[1].split()
        if fields[0] == "Z":
            return None
        return {"boot_id": BOOT_ID, "start_ticks": fields[19], "pgid": int(fields[2])}
    except (OSError, ValueError, IndexError, TypeError):
        return None


def identity_matches(pid, identity) -> bool:
    return bool(identity and process_identity(pid) == identity)


def uncertain(job: dict) -> bool:
    return job.get("status") in UNCERTAIN and not (
        job.get("status") == "lost" and job.get("identity_state") == "previous_boot")


class BridgeState:
    def __init__(self, state_dir: Path | None = None, tasks_dir: Path | None = None):
        self.state_dir = Path(state_dir or paths.bridge_state_dir())
        self.tasks_dir = Path(tasks_dir or paths.bridge_tasks_dir())
        self.jobs_dir = self.state_dir / "jobs"
        self._cache: dict[str, tuple[tuple, dict]] = {}

    @property
    def available(self) -> bool:
        return self.jobs_dir.is_dir() and not self.jobs_dir.is_symlink()

    # -- job records ----------------------------------------------------
    def _read(self, path: Path, limit: int):
        return json.loads(read_bytes_nofollow(path, limit))

    def _sidecar(self, job_id: str, kind: str) -> Path | None:
        for candidate in (self.jobs_dir / ".sidecars" / f"{job_id}.{kind}.json",
                          self.jobs_dir / f"{job_id}.{kind}.json"):
            if candidate.exists() and not candidate.is_symlink():
                return candidate
        return None

    def _derive(self, meta: dict) -> dict:
        """Mirror the server's refresh logic without persisting anything."""
        meta = dict(meta)
        meta["status_derived"] = False
        if meta.get("status") != "running":
            return meta
        job_id = meta["id"]
        outcome = self._sidecar(job_id, "outcome")
        exit_file = self.jobs_dir / f"{job_id}.exit"
        try:
            if outcome:
                result = self._read(outcome, MAX_JOB_BYTES)
                meta.update(exit_code=result.get("exit_code"), ended=result.get("ended"),
                            cleanup=result.get("cleanup"), timed_out=result.get("timed_out", False))
                if (result.get("cleanup") or {}).get("complete") is False:
                    meta["status"] = "cleanup_incomplete"
                elif result.get("cancelled"):
                    meta["status"] = "killed"
                else:
                    meta["status"] = "done" if meta["exit_code"] == 0 else "failed"
                meta["status_derived"] = True
                return meta
            if exit_file.is_file() and not exit_file.is_symlink():
                try:
                    meta["exit_code"] = int(exit_file.read_text().strip() or "-1")
                except ValueError:
                    meta["exit_code"] = -1
                meta.update(status="done" if meta["exit_code"] == 0 else "failed",
                            ended=exit_file.stat().st_mtime, status_derived=True)
                return meta
        except (OSError, ValueError, UnsafePath):
            meta["identity_state"] = "unreadable_outcome"
            return meta
        if meta.get("identity"):
            if not identity_matches(meta.get("pid"), meta["identity"]):
                state = ("previous_boot" if meta["identity"].get("boot_id") != BOOT_ID
                         else "not_running_or_changed")
                meta.update(status="lost", identity_state=state, status_derived=True)
            else:
                meta["identity_state"] = "verified"
        else:
            meta["identity_state"] = "unverified_legacy"
        return meta

    def _observed_session(self, meta: dict) -> str | None:
        sid = meta.get("session_id")
        if is_uuid(sid):
            return sid
        path = self._sidecar(meta["id"], "output")
        if not path:
            return None
        try:
            parsed = (self._read(path, MAX_OUTPUT_BYTES).get("parsed") or {})
        except (OSError, ValueError, UnsafePath, AttributeError):
            return None
        sid = parsed.get("session_id") if isinstance(parsed, dict) else None
        return sid if is_uuid(sid) else None

    def jobs(self, kind: str | None = "agent") -> list[dict]:
        if not self.available:
            return []
        out = []
        try:
            names = os.listdir(self.jobs_dir)
        except OSError:
            return []
        for name in names:
            stem = name[:-5] if name.endswith(".json") else None
            if not stem or not JOB_ID.fullmatch(stem):
                continue  # skips *.output.json etc. (they contain dots)
            path = self.jobs_dir / name
            try:
                info = path.lstat()
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            key = (info.st_ino, info.st_size, info.st_mtime_ns)
            hit = self._cache.get(stem)
            if hit and hit[0] == key and hit[1].get("status") != "running":
                meta = hit[1]
            else:
                try:
                    raw = self._read(path, MAX_JOB_BYTES)
                except (OSError, ValueError, UnsafePath):
                    continue
                if not isinstance(raw, dict) or raw.get("id") != stem:
                    continue
                meta = self._derive(raw)
                if meta.get("kind") == "agent":
                    meta["observed_session_id"] = self._observed_session(meta)
                self._cache[stem] = (key, meta)
            if kind and meta.get("kind") != kind:
                continue
            out.append(meta)
        out.sort(key=lambda m: m.get("started") or 0, reverse=True)
        return out

    def job(self, job_id: str) -> dict:
        if not JOB_ID.fullmatch(job_id or ""):
            raise ValueError("Use an exact bridge job ID.")
        path = self.jobs_dir / f"{job_id}.json"
        try:
            raw = self._read(path, MAX_JOB_BYTES)
        except FileNotFoundError:
            raise KeyError(f"No such bridge job: {job_id}") from None
        meta = self._derive(raw)
        if meta.get("kind") == "agent":
            meta["observed_session_id"] = self._observed_session(meta)
        return meta

    def job_view(self, meta: dict, detail: bool = False) -> dict:
        view = {k: meta.get(k) for k in ("id", "agent", "status", "task_id", "cwd", "started", "ended",
                                          "exit_code", "permission", "model", "timeout", "identity_state",
                                          "invocation_origin")}
        view.update(job_id=meta.get("id"), session_id=meta.get("observed_session_id") or meta.get("session_id"),
                    runner_pid=meta.get("pid"),
                    runner_identity_verified=identity_matches(meta.get("pid"), meta.get("identity")),
                    status_scope="derived read-only from bridge job records; not persisted",
                    status_derived=meta.get("status_derived", False))
        if detail:
            view["display"] = meta.get("display")
            view["workspace_peers"] = meta.get("workspace_peers")
            view["cleanup"] = meta.get("cleanup")
            process = self._sidecar(meta["id"], "process")
            if process:
                try:
                    state = self._read(process, MAX_JOB_BYTES)
                    view["target_pid"] = state.get("pid")
                    view["target_identity_verified"] = identity_matches(state.get("pid"), state.get("identity"))
                    view["observed_descendants"] = [
                        {"pid": int(pid), "identity_verified": identity_matches(int(pid), ident)}
                        for pid, ident in (state.get("descendants") or {}).items()]
                except (OSError, ValueError, UnsafePath, TypeError):
                    view["process_record"] = "unreadable"
            output = self._sidecar(meta["id"], "output")
            if output:
                try:
                    parsed = self._read(output, MAX_OUTPUT_BYTES).get("parsed") or {}
                    view["final"] = parsed.get("final")
                    view["is_error"] = parsed.get("is_error")
                    view["progress_tail"] = (parsed.get("progress") or [])[-10:]
                except (OSError, ValueError, UnsafePath, AttributeError):
                    view["output_record"] = "unreadable"
        return view

    def session_jobs(self) -> dict[tuple, list[dict]]:
        index: dict[tuple, list[dict]] = {}
        for meta in self.jobs("agent"):
            sid = meta.get("observed_session_id")
            if meta.get("agent") in paths.AGENTS and sid:
                index.setdefault((meta["agent"], sid.lower()), []).append(meta)
        return index

    # -- tasks ------------------------------------------------------------
    def task(self, task_id: str) -> dict:
        if not TASK_ID.fullmatch(task_id or ""):
            raise ValueError("Use an exact task ID (task-<24 hex>).")
        base = self.tasks_dir / task_id
        if not base.exists():
            raise KeyError(f"Task not found: {task_id}")
        for directory in (self.tasks_dir, base, base / "workspace"):
            check_private(directory.lstat(), directory=True)
        row = json.loads(read_bytes_nofollow(base / ".task.json", 65536, private=True))
        if (not isinstance(row, dict) or row.get("task_id") != task_id or row.get("schema") != 1 or
                row.get("owner_uid") != os.geteuid() or row.get("workspace") != str(base / "workspace")):
            raise ValueError("Unrecognized task ownership metadata.")
        return row

    def tasks(self) -> list[dict]:
        if not self.tasks_dir.exists():
            return []
        check_private(self.tasks_dir.lstat(), directory=True)
        out = []
        for entry in sorted(os.listdir(self.tasks_dir)):
            if TASK_ID.fullmatch(entry):
                try:
                    out.append(self.task(entry))
                except (OSError, ValueError, KeyError, UnsafePath) as error:
                    out.append({"task_id": entry, "error": str(error)})
        return out

    def task_for_cwd(self, cwd: str | None) -> str | None:
        if not cwd:
            return None
        path = Path(cwd)
        if path.name == "workspace" and path.parent.parent == self.tasks_dir and TASK_ID.fullmatch(path.parent.name):
            return path.parent.name
        return None


# Runs inside the installed server's own interpreter (python -I -c BOOT APP).
BOOT = r"""
import json, os, sys
from pathlib import Path
sys.path.insert(0, sys.argv.pop(1))
request = json.loads(sys.stdin.read())

def run():
    import ai_bridge
    from ai_bridge.core import Config, Jobs
    from ai_bridge import management as m
    env = os.environ.get
    cfg = Config(token='', state_dir=Path(env('AI_BRIDGE_STATE_DIR') or Path.home() / '.local/state/ai-bridge'),
                 tasks_dir=Path(env('AI_BRIDGE_TASKS_DIR') or Path.home() / '.ai-bridge-tasks'),
                 agent_default_permission=env('AI_BRIDGE_AGENT_DEFAULT_PERMISSION', 'edit'))
    op, p = request['op'], request.get('params') or {}
    if op == 'probe':
        return {'version': getattr(ai_bridge, '__version__', None),
                'start_agent': hasattr(m, 'start_agent'), 'stop_agent': hasattr(m, 'stop_agent'),
                'tasks': hasattr(m, 'Tasks')}
    m.private_dir(cfg.state_dir)
    m.private_dir(cfg.state_dir / 'jobs')
    jobs = Jobs(cfg)
    if op == 'agents.stop':
        return m.stop_agent(jobs, p['job_id'])
    if op == 'tasks.create':
        with m.management_lock(cfg):
            return m.Tasks(cfg).create()
    if op == 'tasks.cleanup':
        return m.Tasks(cfg).cleanup(p['task_id'], jobs, p['dry_run'],
                                    allow_uninspectable=p['allow_uninspectable'], confirmed=p['confirmed'])
    if op == 'sessions.start':
        return m.start_agent(cfg, jobs, p['agent'], p['prompt'], p.get('cwd'), p.get('session_id'),
                             p.get('permission'), p.get('model'), p.get('task_id'), p.get('timeout'))
    raise ValueError('Unsupported bridge operation: ' + str(op))

try:
    out = {'ok': True, 'result': run()}
except Exception as error:
    out = {'ok': False, 'error': type(error).__name__ + ': ' + str(error)}
sys.stdout.write(json.dumps(out, default=str))
"""


class BridgeRuntime:
    """Locate and call the active ai-bridge release (current -> releases/<id>)."""

    def __init__(self, root: Path | None = None):
        self.root = Path(root or paths.bridge_install_root()).absolute()

    def locate(self) -> dict:
        root = self.root
        def require(path: Path, directory: bool, what: str):
            try:
                info = path.lstat()
            except FileNotFoundError:
                raise BridgeUnavailable(f"{what} not found: {path}") from None
            kind = stat.S_ISDIR if directory else stat.S_ISREG
            if not kind(info.st_mode) or not not_writable_by_others(path, info):
                raise BridgeUnavailable(f"{what} has unsafe type, ownership or permissions: {path}")
        if not root.exists():
            raise BridgeUnavailable(f"No ai-bridge server installation for this account ({root}); "
                                    "bridge commands are unavailable. Session browsing still works.")
        require(root, True, "Bridge install root")
        releases = root / "releases"
        require(releases, True, "Bridge releases directory")
        current = root / "current"
        if not current.is_symlink() or current.lstat().st_uid not in (os.geteuid(), 0):
            raise BridgeUnavailable("No verified 'current' release symlink; the bridge server is not "
                                    "installed with versioned releases on this account.")
        release = current.resolve(strict=True)
        if release.parent != releases.resolve():
            raise BridgeUnavailable("Active bridge release is outside its releases directory.")
        for path, directory, what in ((release, True, "Release"), (release / "READY", False, "Release READY marker"),
                                      (release / "app", True, "Release app"),
                                      (release / "app/ai_bridge", True, "Release package"),
                                      (release / "app/ai_bridge/management.py", False, "Release management module"),
                                      (release / "venv", True, "Release virtualenv"),
                                      (release / "venv/bin", True, "Release virtualenv bin")):
            require(path, directory, what)
        python = release / "venv/bin/python"
        if not os.access(python, os.X_OK):
            raise BridgeUnavailable("Active bridge release has no executable Python.")
        return {"release": str(release), "python": str(python), "app": str(release / "app")}

    def call(self, op: str, params: dict | None = None, timeout: float = 120) -> dict:
        where = self.locate()
        try:
            proc = subprocess.run([where["python"], "-I", "-c", BOOT, where["app"]],
                                  input=json.dumps({"op": op, "params": params or {}}),
                                  capture_output=True, text=True, timeout=timeout, cwd="/")
        except subprocess.TimeoutExpired:
            raise BridgeUnavailable(f"Bridge runtime did not answer within {timeout:.0f}s.") from None
        try:
            reply = json.loads(proc.stdout)
        except ValueError:
            detail = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:] or ["no output"]
            raise BridgeUnavailable(f"Bridge runtime failed (exit {proc.returncode}): {detail[0][:300]}") from None
        if not reply.get("ok"):
            raise RuntimeError(reply.get("error") or "Bridge operation failed.")
        return reply["result"]


def wait_for_job(state: BridgeState, job_id: str, seconds: float, poll: float = 1.0) -> dict:
    deadline = time.monotonic() + max(0.0, seconds)
    while True:
        meta = state.job(job_id)
        if not uncertain(meta) or time.monotonic() >= deadline:
            return meta
        time.sleep(poll)
