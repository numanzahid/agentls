import os
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import fixtures  # noqa: E402
from agentls import activity as _activity  # noqa: E402

REAL_SNAPSHOT = _activity.snapshot  # for tests that probe the real (or a fake) /proc

# Fixtures create binaries and directories that the code refuses when group-writable;
# don't let a permissive login umask (e.g. 002) turn safety refusals into test failures.
os.umask(0o022)

CLAUDE_ID = "5c1f0e2a-7b3d-4c9e-8a61-2f4d9b0c7e13"
CODEX_ID = "01a00000-0000-7000-8000-000000000001"
CODEX_CHILD = "01a00000-0000-7000-8000-000000000002"
CURSOR_ID = "c7a90d14-3e6b-4f2a-9d58-b1e04f6a2c39"


@dataclass
class Env:
    root: Path
    claude: Path
    codex: Path
    cursor: Path
    state: Path
    cli_state: Path
    cli_data: Path
    mcp_root: Path
    project: Path
    log: Path

    def age(self):
        fixtures.age(self.claude, self.codex, self.cursor)

    def calls(self):
        import json
        return [json.loads(line) for line in self.log.read_text().splitlines() if line.strip()]


@pytest.fixture(autouse=True)
def quiet_processes(request, monkeypatch):
    """Other processes on this host must not influence results (they may share UUIDs).

    test_activity.py exercises the real /proc probe with its own child processes.
    """
    if request.module.__name__ != "test_activity":
        from agentls import activity
        monkeypatch.setattr(activity, "snapshot", lambda: activity.Activity())


@pytest.fixture
def env(tmp_path, monkeypatch):
    for key in list(os.environ):
        if key.startswith(("AGENTLS_", "AI_BRIDGE_", "CLAUDE_CONFIG_DIR", "CODEX_HOME")):
            monkeypatch.delenv(key, raising=False)
    e = Env(root=tmp_path, claude=tmp_path / "claude", codex=tmp_path / "codex", cursor=tmp_path / "cursor",
            state=tmp_path / "mcp-state", cli_state=tmp_path / "cli-state",
            cli_data=tmp_path / "cli-data", mcp_root=tmp_path / "mcp-install",
            project=tmp_path / "project", log=tmp_path / "calls.log")
    e.project.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    for path in (e.claude, e.codex, e.cursor):
        path.mkdir()
    for var, value in (("CLAUDE_CONFIG_DIR", e.claude), ("CODEX_HOME", e.codex),
                       ("AGENTLS_CURSOR_HOME", e.cursor), ("AI_BRIDGE_STATE_DIR", e.state),
                       ("AGENTLS_STATE_DIR", e.cli_state),
                       ("AGENTLS_DATA_DIR", e.cli_data), ("AGENTLS_MCP_ROOT", e.mcp_root),
                       ("FAKE_LOG", e.log)):
        monkeypatch.setenv(var, str(value))
    bins = fixtures.fake_bins(tmp_path / "bin", e.log)
    monkeypatch.setenv("PATH", f"{bins['codex'].parent}{os.pathsep}{os.environ.get('PATH', '')}")
    from agentls import native
    native._HELP_CACHE.clear()
    return e


@pytest.fixture
def populated(env):
    fixtures.claude_session(env.claude, CLAUDE_ID, env.project, "fix the login bug")
    fixtures.codex_session(env.codex, CODEX_ID, env.project, "refactor parser", name="Parser refactor")
    fixtures.codex_session(env.codex, CODEX_CHILD, env.project, "child task", source="exec", parent=CODEX_ID)
    fixtures.cursor_session(env.cursor, CURSOR_ID, env.project, "Cursor design chat")
    env.age()
    return env
