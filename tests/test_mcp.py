import json

from agentls.mcp import McpState


def test_job_derivation_from_outcome_sidecar(env):
    jobs = env.state / "jobs"
    (jobs / ".sidecars").mkdir(parents=True)
    (jobs / "1018-000001-aaaaaa.json").write_text(json.dumps(
        {"id": "1018-000001-aaaaaa", "kind": "agent", "agent": "codex", "status": "running", "pid": 1,
         "identity": {"boot_id": "x"}}))
    (jobs / ".sidecars" / "1018-000001-aaaaaa.outcome.json").write_text(json.dumps({"exit_code": 0, "ended": 5}))
    (jobs / ".sidecars" / "1018-000001-aaaaaa.output.json").write_text(json.dumps(
        {"parsed": {"session_id": "01a00000-0000-7000-8000-000000000001", "final": "ok"}}))
    state = McpState()
    [meta] = state.jobs()
    assert meta["status"] == "done" and meta["status_derived"]
    assert meta["observed_session_id"].startswith("01a00000")
    assert ("codex", "01a00000-0000-7000-8000-000000000001") in state.session_jobs()
    assert state.lineage_view(meta) == {"runner_pid": 1, "runner_identity_verified": False}
