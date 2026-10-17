import os

import fixtures
from conftest import CLAUDE_ID, CODEX_ID

from ai_bridge_cli.provenance import ProvenanceReader, filename, lookup


def tree(path):
    return sorted((str(p.relative_to(path)), p.stat().st_mtime_ns) for p in path.rglob("*")) if path.exists() else []


def test_filename_contract():
    import hashlib
    assert filename("claude", CLAUDE_ID) == hashlib.sha256(f"claude:{CLAUDE_ID}".encode()).hexdigest() + ".json"


def test_verified_record_is_mcp(env):
    fixtures.provenance(env.state, "claude", CLAUDE_ID)
    origin, row, _ = lookup("claude", CLAUDE_ID)
    assert origin == "mcp" and row["job_ids"] == ["1018-000000-abcdef"]


def test_no_record_is_direct_and_no_bridge_is_direct(env):
    assert lookup("claude", CLAUDE_ID)[0] == "direct"  # no bridge state at all
    fixtures.provenance(env.state, "codex", CODEX_ID)
    assert lookup("claude", CLAUDE_ID)[0] == "direct"


def test_missing_directory_with_bridge_state_is_unknown(env):
    (env.state / "jobs").mkdir(parents=True)
    assert lookup("claude", CLAUDE_ID)[0] == "unknown"


def test_record_for_other_session_or_agent_is_rejected(env):
    path = fixtures.provenance(env.state, "claude", CLAUDE_ID, session_id=CODEX_ID)
    assert lookup("claude", CLAUDE_ID)[0] == "unknown"
    path.unlink()
    fixtures.provenance(env.state, "claude", CLAUDE_ID, {"agent": "codex"})
    assert lookup("claude", CLAUDE_ID)[0] == "unknown"


def test_bad_schema_values_are_rejected(env):
    for bad in ({"origin": "native"}, {"schema": 2}, {"job_ids": []}, {"first_seen": -1},
                {"job_ids": ["BAD ID"]}, {"job_ids": ["a1b2", "a1b2"]}):
        path = fixtures.provenance(env.state, "claude", CLAUDE_ID, **bad)
        assert lookup("claude", CLAUDE_ID)[0] == "unknown", bad
        path.unlink()


def test_non_private_or_linked_records_are_rejected(env):
    path = fixtures.provenance(env.state, "claude", CLAUDE_ID)
    os.chmod(path, 0o644)
    assert lookup("claude", CLAUDE_ID)[0] == "unknown"
    os.chmod(path, 0o600)
    os.link(path, env.root / "hardlink")
    assert lookup("claude", CLAUDE_ID)[0] == "unknown"
    os.unlink(env.root / "hardlink")
    target = env.root / "elsewhere.json"
    path.rename(target)
    os.symlink(target, path)
    assert lookup("claude", CLAUDE_ID)[0] == "unknown"


def test_symlinked_or_shared_directory_is_unsafe(env):
    fixtures.provenance(env.state, "claude", CLAUDE_ID)
    os.chmod(env.state / "management", 0o755)
    reader = ProvenanceReader()
    reader.open()
    assert reader.state == "unsafe" and reader.lookup("claude", CLAUDE_ID)[0] == "unknown"
    os.chmod(env.state / "management", 0o700)
    real = env.state / "management" / "provenance"
    real.rename(env.root / "prov-real")
    os.symlink(env.root / "prov-real", real)
    assert lookup("claude", CLAUDE_ID)[0] == "unknown"


def test_reader_never_writes(env):
    (env.state / "jobs").mkdir(parents=True)
    before = tree(env.state)
    with ProvenanceReader() as reader:
        reader.lookup("claude", CLAUDE_ID)
    assert tree(env.state) == before
    fixtures.provenance(env.state, "claude", CLAUDE_ID)
    before = tree(env.state)
    lookup("claude", CLAUDE_ID)
    assert tree(env.state) == before


def test_headless_session_without_record_is_never_mcp(env):
    fixtures.claude_session(env.claude, CLAUDE_ID, env.project, sdk=True)
    fixtures.provenance(env.state, "codex", CODEX_ID)  # provenance dir exists and is valid
    from ai_bridge_cli.catalog import Catalog
    session = Catalog().refresh(probe_activity=False).get("claude", CLAUDE_ID)
    assert session.mode == "noninteractive" and session.origin == "direct"
    assert session.to_json()["bridge_origin"] is None
