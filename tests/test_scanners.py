import hashlib
import os
import sqlite3

import fixtures
from conftest import CLAUDE_ID, CODEX_CHILD, CODEX_ID, CURSOR_ID

from ai_bridge_cli.scanners import Scanner, claude, codex, cursor, history


def by_agent(result, agent):
    return {s.session_id: s for s in result.sessions if s.agent == agent}


def test_all_three_agents_are_listed_from_env_roots(populated):
    result = Scanner().scan()
    assert result.errors == {}
    assert set(by_agent(result, "claude")) == {CLAUDE_ID}
    assert set(by_agent(result, "codex")) == {CODEX_ID, CODEX_CHILD}
    assert set(by_agent(result, "cursor")) == {CURSOR_ID}
    assert result.roots["claude"] == str(populated.claude)


def test_claude_title_mode_model_and_resumable(populated):
    s = by_agent(Scanner().scan(), "claude")[CLAUDE_ID]
    assert s.title == "fix the login bug"  # noise/meta prompt skipped
    assert s.mode == "interactive" and s.cwd == str(populated.project) and s.resumable
    assert s.last_used_model == "claude-test-1"


def test_claude_rename_near_end_and_sdk_mode(env):
    fixtures.claude_session(env.claude, CLAUDE_ID, env.project, "prompt", sdk=True, title="Renamed")
    s = by_agent(Scanner().scan(), "claude")[CLAUDE_ID]
    assert s.title == "Renamed"
    assert s.mode == "noninteractive"  # headless; still says nothing about MCP origin
    assert s.origin == "unknown"


def test_claude_missing_project_is_not_resumable(env):
    fixtures.claude_session(env.claude, CLAUDE_ID, env.root / "gone")
    s = by_agent(Scanner().scan(), "claude")[CLAUDE_ID]
    assert not s.resumable and "missing" in s.resume_note


def test_codex_index_title_subagent_and_history(populated):
    rows = by_agent(Scanner().scan(), "codex")
    main, child = rows[CODEX_ID], rows[CODEX_CHILD]
    assert main.title == "Parser refactor" and main.session_type == "main" and main.mode == "interactive"
    assert child.session_type == "subagent" and child.parent_session_id == CODEX_ID
    assert child.mode == "noninteractive"
    assert main.last_used_model == "gpt-test"
    roles = [(m["role"], m["text"]) for m in history(main)]
    assert ("user", "refactor parser") in roles and ("assistant", "done") in roles
    assert roles.count(("user", "refactor parser")) == 1  # event_msg duplicate suppressed


def test_codex_archived_sessions_are_flagged(env):
    fixtures.codex_session(env.codex, CODEX_ID, env.project, archived=True)
    s = by_agent(Scanner().scan(), "codex")[CODEX_ID]
    assert s.native_archived and not s.resumable


def test_cursor_read_only_scan_and_history(populated):
    db = next((populated.cursor / "chats").rglob("store.db"))
    before = (hashlib.sha256(db.read_bytes()).hexdigest(), db.stat().st_mtime_ns)
    listing = sorted(p.name for p in db.parent.iterdir())
    s = by_agent(Scanner().scan(), "cursor")[CURSOR_ID]
    assert s.title == "Cursor design chat" and s.cwd == str(populated.project) and s.resumable
    assert [m["text"] for m in history(s)] == ["hello cursor", "remembered"]
    assert (hashlib.sha256(db.read_bytes()).hexdigest(), db.stat().st_mtime_ns) == before
    assert sorted(p.name for p in db.parent.iterdir()) == listing  # no -wal/-shm created


def test_cursor_subagent_hidden_and_corrupt_store_isolated(env):
    fixtures.cursor_session(env.cursor, CURSOR_ID, env.project, subagent=True)
    other = "bbbbbbbb-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    db = fixtures.cursor_session(env.cursor, other, env.project)
    con = sqlite3.connect(db)
    con.execute("delete from blobs")
    con.commit()
    con.close()
    rows = by_agent(Scanner().scan(), "cursor")
    assert CURSOR_ID not in rows
    assert rows[other].error and not rows[other].resumable


def test_unchanged_files_are_not_reparsed(populated, monkeypatch):
    scanner = Scanner()
    scanner.scan()
    calls = []
    for module in (claude, codex, cursor):
        original = module.parse
        monkeypatch.setattr(module, "parse", lambda *a, _o=original, **k: calls.append(1) or _o(*a, **k))
    scanner.scan()
    assert calls == []
    transcript = next(populated.claude.rglob(f"{CLAUDE_ID}.jsonl"))
    with transcript.open("a") as stream:
        stream.write("{}\n")
    scanner.scan()
    assert len(calls) == 1


def test_symlinked_transcripts_are_ignored(env):
    real = fixtures.claude_session(env.claude, CLAUDE_ID, env.project)
    other = "22222222-2222-4333-8444-555555555555"
    os.symlink(real, real.with_name(f"{other}.jsonl"))
    assert set(by_agent(Scanner().scan(), "claude")) == {CLAUDE_ID}


def test_duplicate_ids_are_ambiguous(env):
    fixtures.claude_session(env.claude, CLAUDE_ID, env.project, sidecar=False)
    fixtures.claude_session(env.claude, CLAUDE_ID, env.root, sidecar=False)
    rows = [s for s in Scanner().scan().sessions if s.agent == "claude"]
    assert len(rows) == 2 and all(r.error and not r.resumable for r in rows)
