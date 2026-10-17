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
