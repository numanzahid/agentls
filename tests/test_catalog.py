import json
import os

import fixtures
import pytest
from conftest import CLAUDE_ID, CODEX_CHILD, CODEX_ID, CURSOR_ID

from agentls.mcp import process_identity
from agentls.catalog import Catalog, Query
from agentls.overlay import Overlays


def catalog():
    return Catalog().refresh(probe_activity=False)


def ids(rows):
    return [s.session_id for s in rows]


def write_job(env, job_id, sid, agent="claude", **fields):
    jobs = env.state / "jobs"
    jobs.mkdir(parents=True, exist_ok=True)
    row = {"id": job_id, "kind": "agent", "agent": agent, "session_id": sid, "status": "running",
           "cwd": str(env.project), "started": 1.0}
    row.update(fields)
    (jobs / f"{job_id}.json").write_text(json.dumps(row))
    return jobs / f"{job_id}.json"


def test_filters_search_and_sort(populated):
    c = catalog()
    assert len(c.query(Query())) == 4
    assert set(ids(c.query(Query(agents=("codex",))))) == {CODEX_ID, CODEX_CHILD}
    assert ids(c.query(Query(search="LOGIN bug"))) == [CLAUDE_ID]
    assert ids(c.query(Query(search="parser"))) == [CODEX_ID]
    assert ids(c.query(Query(search=CURSOR_ID[:8]))) == [CURSOR_ID]
    titles = [s.display_title for s in c.query(Query(sort="title"))]
    assert titles == sorted(titles, key=str.casefold)
    assert [s.agent for s in c.query(Query(sort="agent"))][0] == "claude"
    assert len(c.query(Query(project=str(populated.project)))) == 4
    assert c.query(Query(project="/nowhere")) == []
    assert c.query(Query(origin="mcp")) == []
    with pytest.raises(ValueError):
        c.query(Query(sort="bogus"))


def test_origin_from_provenance(populated):
    fixtures.provenance(populated.state, "codex", CODEX_ID)
    c = catalog()
    assert ids(c.query(Query(origin="mcp"))) == [CODEX_ID]
    assert c.get("claude", CLAUDE_ID).origin == "direct"
    assert c.get("codex", CODEX_ID).to_json()["origin"] == "mcp"


def test_resolve_selectors(populated):
    c = catalog()
    assert c.resolve(f"claude:{CLAUDE_ID}").session_id == CLAUDE_ID
    assert c.resolve(CURSOR_ID).agent == "cursor"
    assert c.resolve(CLAUDE_ID[:8], allow_prefix=True).session_id == CLAUDE_ID
    with pytest.raises(ValueError):
        c.resolve(CLAUDE_ID[:8])  # prefixes not allowed for actions
    with pytest.raises(ValueError):
        c.resolve("01a00000", allow_prefix=True)  # two codex sessions share the prefix
    with pytest.raises(ValueError):
        c.resolve(f"codex:{CLAUDE_ID}", agent="claude")
    with pytest.raises(KeyError):
        c.resolve(f"codex:{CLAUDE_ID}")


def test_alias_and_archive_overlay(populated):
    overlays = Overlays()
    overlays.update([("claude", CLAUDE_ID)], alias="My alias")
    overlays.update([("cursor", CURSOR_ID), ("codex", CODEX_ID)], archived=True)
    c = catalog()
    s = c.get("claude", CLAUDE_ID)
    assert s.display_title == "My alias" and s.title == "fix the login bug"
    assert set(ids(c.query(Query()))) == {CLAUDE_ID, CODEX_CHILD}
    assert set(ids(c.query(Query(archived="only")))) == {CURSOR_ID, CODEX_ID}
    record = populated.cli_state / "overlays.json"
    assert oct(record.stat().st_mode & 0o777) == "0o600"
    overlays.update([("claude", CLAUDE_ID)], alias=None)
    assert catalog().get("claude", CLAUDE_ID).alias is None
    with pytest.raises(ValueError):
        overlays.update([("claude", CLAUDE_ID)], alias="bad\x1bname")
    # Native transcript untouched.
    assert "My alias" not in next(populated.claude.rglob(f"{CLAUDE_ID}.jsonl")).read_text()


def test_mcp_job_status(populated):
    write_job(populated, "1018-000001-aaaaaa", CLAUDE_ID, pid=os.getpid(), identity=process_identity(os.getpid()))
    write_job(populated, "1018-000002-bbbbbb", CURSOR_ID, agent="cursor")  # legacy: no identity
    write_job(populated, "1018-000003-cccccc", CODEX_ID, agent="codex", pid=999999999,
              identity={"boot_id": "other-boot", "start_ticks": "1", "pgid": 1})
    c = catalog()
    assert c.get("claude", CLAUDE_ID).status == "active"
    assert c.get("cursor", CURSOR_ID).status == "uncertain"
    assert c.get("codex", CODEX_ID).status == "idle"  # lost on a previous boot
    assert c.get("codex", CODEX_ID).job_ids == ["1018-000003-cccccc"]
    assert ids(c.query(Query(status="active"))) == [CLAUDE_ID]


def test_mcp_state_is_never_modified(populated):
    path = write_job(populated, "1018-000001-aaaaaa", CLAUDE_ID, pid=999999999,
                     identity={"boot_id": "x", "start_ticks": "1", "pgid": 1})
    before = (path.read_bytes(), path.stat().st_mtime_ns, sorted(os.listdir(path.parent)))
    catalog()
    from agentls.mcp import McpState
    McpState().jobs()
    assert (path.read_bytes(), path.stat().st_mtime_ns, sorted(os.listdir(path.parent))) == before
    assert not (populated.state / "management").exists()


def test_store_errors_do_not_hide_other_agents(populated, monkeypatch):
    from agentls.scanners import cursor
    monkeypatch.setattr(cursor, "chat_dirs", lambda root=None: (_ for _ in ()).throw(RuntimeError("boom")))
    c = catalog()
    assert "cursor" in c.errors and c.get("claude", CLAUDE_ID)


def test_noninteractive_filter_and_subagent_mode_inheritance(env):
    exec_parent, guardian = "01a00000-0000-7000-8000-000000000003", "01a00000-0000-7000-8000-000000000004"
    fixtures.codex_session(env.codex, CODEX_ID, env.project, source="cli")
    fixtures.codex_session(env.codex, exec_parent, env.project, source="exec")
    # Subagent rows record no launch mode; they inherit their parent's.
    fixtures.codex_session(env.codex, CODEX_CHILD, env.project, source={"subagent": {"other": "guardian"}},
                           parent=CODEX_ID)
    fixtures.codex_session(env.codex, guardian, env.project, source={"subagent": {"other": "guardian"}},
                           parent=exec_parent)
    env.age()
    c = catalog()
    modes = {s.session_id: s.mode for s in c.sessions}
    assert modes == {CODEX_ID: "interactive", CODEX_CHILD: "interactive",
                     exec_parent: "noninteractive", guardian: "noninteractive"}
    assert set(ids(c.query(Query()))) == set(modes)
    assert set(ids(c.query(Query(noninteractive="hide")))) == {CODEX_ID, CODEX_CHILD}
    assert set(ids(c.query(Query(noninteractive="only")))) == {exec_parent, guardian}
    with pytest.raises(ValueError):
        c.query(Query(noninteractive="sometimes"))
