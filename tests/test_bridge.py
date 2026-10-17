import json
import os
import sys

import pytest

from ai_bridge_cli.bridge import BridgeRuntime, BridgeState, BridgeUnavailable

TASK = "task-" + "a" * 24


def make_task(env, task_id=TASK):
    base = env.tasks / task_id
    (base / "workspace").mkdir(parents=True, mode=0o700)
    for p in (env.tasks, base, base / "workspace"):
        os.chmod(p, 0o700)
    meta = base / ".task.json"
    meta.write_text(json.dumps({"task_id": task_id, "schema": 1, "owner_uid": os.geteuid(), "created": 1.0,
                                "workspace": str(base / "workspace")}))
    os.chmod(meta, 0o600)
    return base


def test_tasks_read_only_validation(env):
    assert BridgeState().tasks() == []
    make_task(env)
    assert BridgeState().task(TASK)["workspace"].endswith("workspace")
    os.chmod(env.tasks / TASK, 0o755)
    with pytest.raises(ValueError):
        BridgeState().task(TASK)
    with pytest.raises(ValueError):
        BridgeState().task("task-nothex")


def test_job_derivation_from_outcome_sidecar(env):
    jobs = env.state / "jobs"
    (jobs / ".sidecars").mkdir(parents=True)
    (jobs / "1018-000001-aaaaaa.json").write_text(json.dumps(
        {"id": "1018-000001-aaaaaa", "kind": "agent", "agent": "codex", "status": "running", "pid": 1,
         "identity": {"boot_id": "x"}}))
    (jobs / ".sidecars" / "1018-000001-aaaaaa.outcome.json").write_text(json.dumps({"exit_code": 0, "ended": 5}))
    (jobs / ".sidecars" / "1018-000001-aaaaaa.output.json").write_text(json.dumps(
        {"parsed": {"session_id": "01a00000-0000-7000-8000-000000000001", "final": "ok"}}))
    state = BridgeState()
    meta = state.job("1018-000001-aaaaaa")
    assert meta["status"] == "done" and meta["status_derived"]
    view = state.job_view(meta, detail=True)
    assert view["final"] == "ok" and view["session_id"].startswith("01a00000")
    assert ("codex", "01a00000-0000-7000-8000-000000000001") in state.session_jobs()
    with pytest.raises(KeyError):
        state.job("1018-000009-ffffff")


FAKE_CORE = '''
from dataclasses import dataclass, field
from pathlib import Path
@dataclass
class Config:
    token: str
    state_dir: Path = None
    tasks_dir: Path = None
    agent_default_permission: str = "edit"
class Jobs:
    def __init__(self, cfg): self.cfg = cfg
'''
FAKE_MANAGEMENT = '''
import contextlib, os
def private_dir(path): os.makedirs(path, mode=0o700, exist_ok=True); return path
@contextlib.contextmanager
def management_lock(cfg): yield
class Tasks:
    def __init__(self, cfg): self.cfg = cfg
    def create(self): return {"task_id": "task-" + "b" * 24}
    def cleanup(self, task_id, jobs, dry_run, allow_uninspectable=False, confirmed=False):
        return {"task_id": task_id, "dry_run": dry_run, "confirmed": confirmed}
def start_agent(cfg, jobs, agent, prompt, cwd, session_id, permission, model, task_id, timeout):
    return {"id": "1018-000001-cccccc", "agent": agent, "prompt_seen": prompt, "session_id": session_id,
            "state_dir": str(cfg.state_dir)}
def stop_agent(jobs, job_id): return {"job_id": job_id, "cancel_requested": True}
'''


def make_release(env):
    release = env.bridge_root / "releases" / "r1"
    package = release / "app" / "ai_bridge"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("__version__ = 'fake'\n")
    (package / "core.py").write_text(FAKE_CORE)
    (package / "management.py").write_text(FAKE_MANAGEMENT)
    (release / "READY").write_text("ok\n")
    (release / "venv" / "bin").mkdir(parents=True)
    os.symlink(sys.executable, release / "venv" / "bin" / "python")
    os.symlink(release, env.bridge_root / "current")
    return release


def test_runtime_locate_and_call(env):
    with pytest.raises(BridgeUnavailable):
        BridgeRuntime().locate()
    make_release(env)
    runtime = BridgeRuntime()
    assert runtime.locate()["release"].endswith("r1")
    assert runtime.call("probe")["version"] == "fake"
    result = runtime.call("sessions.start", {"agent": "codex", "prompt": "-p hi", "session_id": None})
    assert result["prompt_seen"] == "-p hi" and result["state_dir"] == str(env.state)
    assert runtime.call("agents.stop", {"job_id": "1018-000001-cccccc"})["cancel_requested"]
    with pytest.raises(RuntimeError):
        runtime.call("bogus")


def test_runtime_refuses_writable_release(env):
    release = make_release(env)
    os.chmod(release, 0o777)
    with pytest.raises(BridgeUnavailable):
        BridgeRuntime().locate()
    os.chmod(release, 0o755)
    (env.bridge_root / "current").unlink()
    os.symlink(env.root, env.bridge_root / "current")  # outside releases/
    with pytest.raises(BridgeUnavailable):
        BridgeRuntime().locate()
