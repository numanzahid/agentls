import hashlib
import json
import os
from dataclasses import replace

import fixtures
import pytest
from conftest import CLAUDE_ID, CODEX_ID, CURSOR_ID

from ai_bridge_cli import trash
from ai_bridge_cli.activity import Activity
from ai_bridge_cli.catalog import Catalog


def catalog():
    return Catalog().refresh(probe_activity=False)


def digest_tree(root):
    out = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            out[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def test_claude_round_trip(populated):
    before = digest_tree(populated.claude)
    session = catalog().get("claude", CLAUDE_ID)
    plan = trash.build_plan([session], Activity())
    item = plan.items[0]
    assert item.allowed and item.method == "quarantine"
    kinds = sorted((e.kind, os.path.basename(e.path)) for e in item.entries)
    assert ("file", f"{CLAUDE_ID}.jsonl") in kinds and ("dir", CLAUDE_ID) in kinds
    result = trash.execute(plan, plan.plan_id)
    assert result["moved"] == [f"claude:{CLAUDE_ID}"] and not result["failed"]
    assert not list(populated.claude.rglob(f"{CLAUDE_ID}*"))
    assert all(CLAUDE_ID != s.session_id for s in catalog().sessions)
    batch = populated.cli_data / "trash" / result["batch_id"]
    assert oct(batch.stat().st_mode & 0o777) == "0o700"
    listing = trash.list_batches()
    assert listing[0]["items"][0]["status"] == "trashed"
    preview = trash.restore(result["batch_id"], dry_run=True)
    assert preview["would_restore"] and not list(populated.claude.rglob(f"{CLAUDE_ID}*"))
    restored = trash.restore(result["batch_id"])
    assert restored["restored"] == [f"claude:{CLAUDE_ID}"]
    assert digest_tree(populated.claude) == before
    assert catalog().get("claude", CLAUDE_ID)


def test_cursor_round_trip_includes_transcripts(populated):
    before = digest_tree(populated.cursor)
    plan = trash.build_plan([catalog().get("cursor", CURSOR_ID)], Activity())
    assert plan.items[0].allowed and len(plan.items[0].entries) == 2
    result = trash.execute(plan)
    assert not list(populated.cursor.rglob(CURSOR_ID))
    trash.restore(result["batch_id"])
    assert digest_tree(populated.cursor) == before


def test_codex_uses_native_archive_never_moves_rollouts_itself(populated):
    session = catalog().get("codex", CODEX_ID)
    rollout = session.storage_path
    plan = trash.build_plan([session], Activity())
    assert plan.items[0].allowed and plan.items[0].method == "codex-archive"
    result = trash.execute(plan)
    assert ["archive", CODEX_ID] in populated.calls()
    assert not os.path.exists(rollout)  # moved by (fake) codex, not by us
    assert catalog().get("codex", CODEX_ID).native_archived
    trash.restore(result["batch_id"])
    assert ["unarchive", CODEX_ID] in populated.calls()
    assert not catalog().get("codex", CODEX_ID).native_archived


def test_codex_without_archive_command_is_unsupported(populated, monkeypatch):
    monkeypatch.setenv("FAKE_NO_ARCHIVE", "1")
    plan = trash.build_plan([catalog().get("codex", CODEX_ID)], Activity())
    assert not plan.items[0].allowed and "Unsupported" in plan.items[0].blockers[0]
    result = trash.execute(plan)
    assert result["batch_id"] is None and result["skipped"]
    assert ["archive", CODEX_ID] not in populated.calls()


def test_recently_modified_is_blocked(populated):
    fixtures.age(populated.claude, when=__import__("time").time())
    plan = trash.build_plan([catalog().get("claude", CLAUDE_ID)], Activity())
    assert not plan.items[0].allowed
    assert any("Modified within" in b for b in plan.items[0].blockers)


def test_active_sessions_are_blocked(populated):
    session = catalog().get("claude", CLAUDE_ID)
    live = Activity(claude_live={CLAUDE_ID: 4242})
    assert not trash.build_plan([session], live).items[0].allowed
    busy = replace(session, status="uncertain", status_note="bridge job", active_job_ids=["1018-1-a"])
    assert not trash.build_plan([busy], Activity()).items[0].allowed


def test_open_file_is_blocked(populated):
    session = catalog().get("claude", CLAUDE_ID)
    with open(session.storage_path):
        info = os.stat(session.storage_path)
        act = Activity(open_files={(info.st_dev, info.st_ino): 1234})
        assert any("open" in b for b in trash.build_plan([session], act).items[0].blockers)


def test_symlinks_and_out_of_root_are_refused(populated):
    session = catalog().get("claude", CLAUDE_ID)
    project_dir = os.path.dirname(session.storage_path)
    moved = populated.root / "real-project"
    os.rename(project_dir, moved)
    os.symlink(moved, project_dir)  # symlinked directory below the agent root
    plan = trash.build_plan([session], Activity())
    assert not plan.items[0].allowed and any("symlink" in b.lower() for b in plan.items[0].blockers)
    outside = replace(session, storage_path=str(populated.root / "elsewhere" / f"{CLAUDE_ID}.jsonl"))
    assert not trash.build_plan([outside], Activity()).items[0].allowed
    traversal = replace(session, storage_path=str(populated.claude / "projects" / ".." / ".." / "x.jsonl"))
    assert not trash.build_plan([traversal], Activity()).items[0].allowed


def test_symlinked_transcript_file_is_refused(populated):
    session = catalog().get("claude", CLAUDE_ID)
    real = populated.root / "real.jsonl"
    os.rename(session.storage_path, real)
    os.symlink(real, session.storage_path)
    plan = trash.build_plan([session], Activity())
    assert not plan.items[0].allowed
    assert real.exists()


def test_changed_file_after_preview_aborts(populated):
    session = catalog().get("claude", CLAUDE_ID)
    plan = trash.build_plan([session], Activity())
    with open(session.storage_path, "a") as stream:
        stream.write("{}\n")
    os.utime(session.storage_path, (fixtures.OLD + 5, fixtures.OLD + 5))
    with pytest.raises(ValueError, match="changed since the preview"):
        trash.execute(plan)
    assert os.path.exists(session.storage_path)


def test_wrong_plan_id_refused(populated):
    plan = trash.build_plan([catalog().get("claude", CLAUDE_ID)], Activity())
    with pytest.raises(ValueError):
        trash.execute(plan, "0000000000000000")
    assert os.path.exists(plan.items[0].entries[0].path)


def test_restore_never_overwrites(populated):
    session = catalog().get("claude", CLAUDE_ID)
    plan = trash.build_plan([session], Activity())
    result = trash.execute(plan)
    with open(session.storage_path, "w") as stream:
        stream.write("new content\n")
    out = trash.restore(result["batch_id"])
    assert out["failed"] and "already exists" in out["failed"][0]["error"]
    assert open(session.storage_path).read() == "new content\n"
    os.unlink(session.storage_path)
    assert trash.restore(result["batch_id"])["restored"]  # resumable after fixing the conflict


def test_tampered_manifest_root_is_rejected(populated):
    result = trash.execute(trash.build_plan([catalog().get("claude", CLAUDE_ID)], Activity()))
    manifest = populated.cli_data / "trash" / result["batch_id"] / "manifest.json"
    data = json.loads(manifest.read_text())
    data["items"][0]["entries"][0]["root"] = "/etc"
    manifest.write_text(json.dumps(data))
    os.chmod(manifest, 0o600)
    with pytest.raises(ValueError):
        trash.restore(result["batch_id"])


def test_purge_removes_only_the_batch(populated):
    result = trash.execute(trash.build_plan([catalog().get("claude", CLAUDE_ID)], Activity()))
    assert trash.purge(result["batch_id"])["purged"]
    assert trash.list_batches() == []
    with pytest.raises(ValueError):
        trash.purge("../../etc")
    assert catalog().get("codex", CODEX_ID)


def test_mixed_selection_moves_allowed_and_reports_blocked(populated):
    c = catalog()
    claude_s = c.get("claude", CLAUDE_ID)
    busy = replace(c.get("cursor", CURSOR_ID), status="active", status_note="in use")
    plan = trash.build_plan([claude_s, busy], Activity())
    result = trash.execute(plan)
    assert result["moved"] == [f"claude:{CLAUDE_ID}"] and result["skipped"][0]["selector"].startswith("cursor:")
    assert os.path.exists(busy.storage_path)
