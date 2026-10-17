"""The real /proc probe, against child processes this test controls."""
import json
import os
import subprocess
import sys
import time
import uuid

from ai_bridge_cli import activity

HOLD = "import sys, time; f = open(sys.argv[1]); print('ready', flush=True); time.sleep(30)"


def test_cmdline_open_file_and_claude_registry(env):
    sid = str(uuid.uuid4())
    held = env.root / "held.jsonl"
    held.write_text("{}\n")
    named = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", f"--resume={sid}"])
    holder = subprocess.Popen([sys.executable, "-c", HOLD, str(held)], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "ready"
        time.sleep(0.2)
        registry = env.claude / "sessions"
        registry.mkdir()
        other = str(uuid.uuid4())
        (registry / f"{named.pid}.json").write_text(json.dumps(
            {"pid": named.pid, "procStart": activity.proc_start_ticks(named.pid), "sessionId": other}))
        stale = str(uuid.uuid4())
        (registry / "1.json").write_text(json.dumps({"pid": named.pid, "procStart": "0", "sessionId": stale}))
        snap = activity.snapshot()
        assert "was started with this session ID" in snap.evidence("codex", sid, str(env.root / "x"))
        assert "open" in snap.evidence("claude", str(uuid.uuid4()), str(held))
        assert "Claude Code process" in snap.evidence("claude", other, str(env.root / "x"))
        assert snap.evidence("claude", stale, str(env.root / "x")) is None  # pid reused / start time differs
        assert snap.evidence("claude", str(uuid.uuid4()), str(env.root / "nothing")) is None
    finally:
        named.kill()
        holder.kill()
        named.wait()
        holder.wait()


def test_own_process_is_not_evidence(env):
    held = env.root / "mine.jsonl"
    held.write_text("{}\n")
    with open(held):
        assert activity.snapshot().evidence("claude", str(uuid.uuid4()), str(held)) is None


def test_flock_is_detected(env):
    lock = env.codex / "thread-writer-locks"
    lock.mkdir()
    sid = str(uuid.uuid4())
    path = lock / f"{sid}.lock"
    path.touch()
    # Path via the environment so only the lock (not the command line) is evidence.
    code = ("import fcntl, os, time; f = open(os.environ['LOCK_PATH']); fcntl.flock(f, fcntl.LOCK_EX); "
            "f2 = open(os.devnull); print('ready', flush=True); time.sleep(30)")
    child = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True,
                             env={**os.environ, "LOCK_PATH": str(path)})
    try:
        assert child.stdout.readline().strip() == "ready"
        evidence = activity.snapshot().evidence("codex", sid, str(env.codex / "sessions" / "r.jsonl"))
        assert evidence and ("locked" in evidence or "open" in evidence) and "started with" not in evidence
    finally:
        child.kill()
        child.wait()
