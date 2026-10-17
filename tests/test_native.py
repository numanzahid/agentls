import os
from dataclasses import replace

import pytest
from conftest import CLAUDE_ID, CODEX_CHILD, CODEX_ID, CURSOR_ID

from ai_bridge_cli import native, paths
from ai_bridge_cli.activity import Activity
from ai_bridge_cli.catalog import Catalog


def get(agent, sid):
    return Catalog().refresh(probe_activity=False).get(agent, sid)


def test_claude_open_and_fork_plans(populated):
    s = get("claude", CLAUDE_ID)
    plan = native.plan(s, "open", Activity())
    assert plan.argv[1:] == ["--resume", CLAUDE_ID] and plan.cwd == str(populated.project)
    assert native.plan(s, "fork", Activity()).argv[-1] == "--fork-session"
    native.recheck(plan, s)


def test_codex_resume_includes_noninteractive_flag(populated):
    plan = native.plan(get("codex", CODEX_CHILD), "open", Activity())
    assert plan.argv[1:] == ["resume", "--include-non-interactive", CODEX_CHILD]
    assert native.plan(get("codex", CODEX_ID), "fork", Activity()).argv[1:] == ["fork", CODEX_ID]


def test_cursor_requires_default_home(populated, monkeypatch):
    s = get("cursor", CURSOR_ID)
    with pytest.raises(native.PreflightError, match="AI_BRIDGE_CURSOR_HOME"):
        native.plan(s, "open", Activity())
    monkeypatch.setattr(paths, "cursor_home_is_default", lambda: True)
    assert native.plan(s, "open", Activity()).argv[1:] == ["--resume", CURSOR_ID]
    with pytest.raises(native.PreflightError):
        native.plan(s, "fork", Activity())


def test_refuses_active_missing_project_and_replaced_storage(populated):
    s = get("claude", CLAUDE_ID)
    with pytest.raises(native.PreflightError, match="in use"):
        native.plan(s, "open", Activity(cmdline_ids={CLAUDE_ID: 99}))
    with pytest.raises(native.PreflightError):
        native.plan(replace(s, resumable=False, resume_note="gone"), "open", Activity())
    plan = native.plan(s, "open", Activity())
    os.rename(s.storage_path, s.storage_path + ".bak")
    with open(s.storage_path, "w") as stream:
        stream.write(open(s.storage_path + ".bak").read())
    with pytest.raises(native.PreflightError, match="replaced"):
        native.recheck(plan, s)


def test_world_writable_storage_refused(populated):
    s = get("claude", CLAUDE_ID)
    os.chmod(s.storage_path, 0o666)
    with pytest.raises(native.PreflightError):
        native.plan(s, "open", Activity())


def test_missing_binary(populated, monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    with pytest.raises(native.PreflightError, match="not found"):
        native.plan(get("claude", CLAUDE_ID), "open", Activity())


def test_native_env_strips_bridge_settings(monkeypatch):
    monkeypatch.setenv("AI_BRIDGE_TOKEN", "secret")
    monkeypatch.setenv("CODEX_HOME", "/x")
    env = native.native_env()
    assert "AI_BRIDGE_TOKEN" not in env and env["CODEX_HOME"] == "/x"


def test_exec_handoff_preserves_pid_cwd_argv_and_environment(populated):
    """Real exec into a fake native CLI: no agent turns or native-store writes."""
    import json
    import subprocess
    import sys

    script = populated.root / "native-agent"
    result = populated.root / "exec-result.json"
    script.write_text(f"#!{sys.executable}\n" +
                      "import json, os, sys\n" +
                      f"with open({str(result)!r}, 'w') as stream:\n" +
                      " json.dump({'pid': os.getpid(), 'cwd': os.getcwd(), 'argv': sys.argv[1:], "
                      "'secret': os.environ.get('AI_BRIDGE_TOKEN')}, stream)\n")
    script.chmod(0o700)
    plan = native.plan(get("claude", CLAUDE_ID), "open", Activity())
    code = ("import os; from ai_bridge_cli.native import LaunchPlan, exec_; "
            f"plan = LaunchPlan(**{dict(plan.__dict__, argv=[str(script), '--resume', CLAUDE_ID])!r}); "
            "exec_(plan)")
    child = subprocess.Popen([sys.executable, "-c", code],
                             env={**os.environ, "AI_BRIDGE_TOKEN": "not-for-agent"})
    assert child.wait(timeout=10) == 0
    row = json.loads(result.read_text())
    assert row == {"pid": child.pid, "cwd": str(populated.project),
                   "argv": ["--resume", CLAUDE_ID], "secret": None}


def test_exec_failure_restores_cwd(populated):
    from pathlib import Path
    plan = native.plan(get("claude", CLAUDE_ID), "open", Activity())
    before = Path.cwd()
    plan.argv = [str(populated.root / "missing-agent")]
    with pytest.raises(native.PreflightError, match="Could not start"):
        native.exec_(plan)
    assert Path.cwd() == before


def test_run_uses_native_project_cwd(populated):
    from pathlib import Path
    plan = native.plan(get("claude", CLAUDE_ID), "open", Activity())
    before = Path.cwd()
    marker = populated.root / "cwd.txt"
    plan.argv = ["/bin/sh", "-c", 'pwd > "$1"; exit 7', "agent", str(marker)]
    assert native.run(plan) == 7
    assert marker.read_text().strip() == str(populated.project)
    assert Path.cwd() == before
