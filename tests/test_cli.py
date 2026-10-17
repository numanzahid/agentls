import json
import os
import subprocess
import sys
from pathlib import Path

from conftest import CLAUDE_ID, CODEX_ID, CURSOR_ID

from ai_bridge_cli import cli

SRC = str(Path(__file__).parents[1] / "src")


def run(capsys, *argv):
    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def as_json(capsys, *argv):
    code, out, err = run(capsys, *argv)
    assert code == 0, err
    return json.loads(out)


def test_no_args_non_tty_shows_help_and_browse_refuses():
    proc = subprocess.run([sys.executable, "-m", "ai_bridge_cli"], capture_output=True, text=True,
                          env={**os.environ, "PYTHONPATH": SRC}, stdin=subprocess.DEVNULL)
    assert proc.returncode == 0 and "sessions" in proc.stdout and "examples:" in proc.stdout
    proc = subprocess.run([sys.executable, "-m", "ai_bridge_cli", "sessions", "browse"], capture_output=True,
                          text=True, env={**os.environ, "PYTHONPATH": SRC}, stdin=subprocess.DEVNULL)
    assert proc.returncode == 2 and "interactive terminal" in proc.stderr


def test_version_and_prog(capsys, monkeypatch):
    monkeypatch.setenv("AI_BRIDGE_CLI_PROG", "ai-bridge-cli")
    try:
        cli.main(["--version"])
    except SystemExit as stop:
        assert stop.code == 0
    assert capsys.readouterr().out.startswith("ai-bridge-cli ")


def test_list_json_shape_and_filters(populated, capsys):
    data = as_json(capsys, "sessions", "list")
    assert data["total"] == 4 and data["next_offset"] is None
    row = next(r for r in data["sessions"] if r["agent"] == "claude")
    assert row["selector"] == f"claude:{CLAUDE_ID}" and row["bridge_origin"] is None
    assert {r["agent"] for r in as_json(capsys, "sessions", "list", "--agent", "cursor")["sessions"]} == {"cursor"}
    page = as_json(capsys, "sessions", "list", "--limit", "1", "--offset", "1")
    assert len(page["sessions"]) == 1 and page["next_offset"] == 2
    assert as_json(capsys, "sessions", "list", "--search", "login")["total"] == 1
    assert as_json(capsys, "sessions", "list", "--type", "subagent")["total"] == 1
    assert as_json(capsys, "sessions", "list", "--mode", "noninteractive")["total"] == 1


def test_table_output(populated, capsys):
    code, out, _ = run(capsys, "sessions", "list", "--table")
    assert code == 0 and "AGENT" in out and "Sessions: 4 shown" in out


def test_inspect_prefix_and_history(populated, capsys):
    data = as_json(capsys, "sessions", "inspect", CLAUDE_ID[:8], "--history", "5")
    assert data["session_id"] == CLAUDE_ID and data["history"][0]["text"] == "fix the login bug"
    assert data["delete_support"]["method"] == "quarantine"


def test_rename_archive_unarchive(populated, capsys):
    as_json(capsys, "sessions", "rename", f"claude:{CLAUDE_ID}", "Login fix")
    assert as_json(capsys, "sessions", "inspect", f"claude:{CLAUDE_ID}")["title"] == "Login fix"
    as_json(capsys, "sessions", "archive", f"claude:{CLAUDE_ID}", f"cursor:{CURSOR_ID}")
    assert as_json(capsys, "sessions", "list")["total"] == 2
    assert as_json(capsys, "sessions", "list", "--archived", "only")["total"] == 2
    as_json(capsys, "sessions", "unarchive", f"cursor:{CURSOR_ID}")
    assert as_json(capsys, "sessions", "list")["total"] == 3
    as_json(capsys, "sessions", "rename", f"claude:{CLAUDE_ID}", "--clear")
    assert as_json(capsys, "sessions", "inspect", f"claude:{CLAUDE_ID}")["alias"] is None


def test_errors_are_json_on_stderr(populated, capsys):
    code, out, err = run(capsys, "sessions", "inspect", "claude:not-a-uuid")
    assert code == 1 and out == "" and "error" in json.loads(err)
    code, _, err = run(capsys, "sessions", "open", f"codex:{CLAUDE_ID}")
    assert code == 1 and "not found" in json.loads(err)["error"]


def test_delete_requires_preview_plan_id(populated, capsys):
    selector = f"claude:{CLAUDE_ID}"
    plan = as_json(capsys, "sessions", "delete", selector)  # non-TTY without --yes: preview only
    assert plan["dry_run"] and plan["allowed"] == 1 and "--plan-id" in plan["next"]
    assert Path(plan["items"][0]["entries"][0]["path"]).exists()
    code, _, err = run(capsys, "sessions", "delete", selector, "--yes")
    assert code == 1 and "--plan-id" in json.loads(err)["error"]
    code, _, err = run(capsys, "sessions", "delete", selector, "--yes", "--plan-id", "deadbeefdeadbeef")
    assert code == 1 and "changed" in json.loads(err)["error"]
    result = as_json(capsys, "sessions", "delete", selector, "--yes", "--plan-id", plan["plan_id"])
    assert result["moved"] == [selector]
    batches = as_json(capsys, "sessions", "trash", "list")
    assert batches[0]["batch_id"] == result["batch_id"]
    restored = as_json(capsys, "sessions", "trash", "restore", result["batch_id"])
    assert restored["restored"] == [selector]
    code, _, err = run(capsys, "sessions", "trash", "purge", result["batch_id"])
    assert code == 1 and "--yes" in json.loads(err)["error"]


def test_blocked_delete_exits_partial(populated, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_NO_ARCHIVE", "1")
    plan = as_json(capsys, "sessions", "delete", f"codex:{CODEX_ID}", "--dry-run")
    assert plan["allowed"] == 0 and plan["next"] == "Nothing can be deleted."


def test_open_dry_run_and_non_tty_refusal(populated, capsys):
    assert as_json(capsys, "sessions", "promote", f"claude:{CLAUDE_ID}", "--dry-run")["action"] == "open"
    plan = as_json(capsys, "sessions", "open", f"claude:{CLAUDE_ID}", "--dry-run")
    assert plan["argv"][1:] == ["--resume", CLAUDE_ID] and plan["history_rewritten"] is False
    code, _, err = run(capsys, "sessions", "open", f"claude:{CLAUDE_ID}")
    assert code == 1 and "interactive terminal" in json.loads(err)["error"]


def test_bridge_commands_without_runtime(populated, capsys):
    assert as_json(capsys, "agents", "status") == []
    assert as_json(capsys, "tasks", "list") == []
    status = as_json(capsys, "bridge", "status")
    assert status["runtime"]["available"] is False and status["stores"]["claude"] == str(populated.claude)
    code, _, err = run(capsys, "sessions", "continue", f"claude:{CLAUDE_ID}", "hello")
    assert code == 1 and "no ai-bridge server installation" in json.loads(err)["error"].lower()
    code, _, err = run(capsys, "tasks", "cleanup", "task-" + "a" * 24)
    assert code == 1 and "--dry-run" in json.loads(err)["error"]


def test_bridge_continue_through_runtime(populated, capsys):
    from test_bridge import make_release
    make_release(populated)
    data = as_json(capsys, "sessions", "continue", f"claude:{CLAUDE_ID}", "next step", "--permission", "readonly")
    assert data["session_id"] == CLAUDE_ID and data["prompt_seen"] == "next step"
    assert "agents inspect" in data["next"]


def test_usage_error_is_json(capsys):
    try:
        cli.main(["sessions", "list", "--agent", "nope"])
    except SystemExit as stop:
        assert stop.code == 2
    assert json.loads(capsys.readouterr().err)["usage"] is True
