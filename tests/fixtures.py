"""Builders for synthetic native stores (no real user data is touched)."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

OLD = time.time() - 3600


def age(*roots: Path, when: float = OLD) -> None:
    """Make fixture files look idle (recently modified sessions are protected)."""
    for root in roots:
        if not root.exists():
            continue
        for directory, dirs, files in os.walk(root, topdown=False):
            for name in files + dirs:
                path = Path(directory) / name
                if not path.is_symlink():
                    os.utime(path, (when, when))
        os.utime(root, (when, when))


def write_jsonl(path: Path, rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return path


def claude_session(root: Path, sid: str, cwd: Path, prompt: str = "hello claude", *, sdk: bool = False,
                   title: str | None = None, model: str = "claude-test-1", sidecar: bool = True) -> Path:
    slug = "-" + str(cwd).strip("/").replace("/", "-")
    rows = [
        {"type": "user", "cwd": str(cwd), "sessionId": sid, "timestamp": "2026-01-01T00:00:00Z",
         "entrypoint": "sdk-cli" if sdk else "cli",
         "message": {"role": "user", "content": "<system-reminder>noise</system-reminder>"}, "isMeta": True},
        {"type": "user", "cwd": str(cwd), "sessionId": sid, "timestamp": "2026-01-01T00:00:01Z",
         "message": {"role": "user", "content": prompt}},
        {"type": "assistant", "cwd": str(cwd), "sessionId": sid, "timestamp": "2026-01-01T00:00:02Z",
         "message": {"role": "assistant", "model": model,
                     "content": [{"type": "text", "text": "hi there"},
                                 {"type": "tool_use", "name": "Bash", "input": {"command": "ls -la"}}]}},
    ]
    if title:
        rows.append({"type": "custom-title", "customTitle": title, "sessionId": sid})
    path = write_jsonl(root / "projects" / slug / f"{sid}.jsonl", rows)
    if sidecar:
        (path.with_suffix("") / "subagents").mkdir(parents=True)
        (path.with_suffix("") / "subagents" / "agent-1.jsonl").write_text("{}\n")
        (root / "session-env" / sid).mkdir(parents=True)
    return path


def codex_session(root: Path, sid: str, cwd: Path, prompt: str = "hello codex", *, source="cli",
                  parent: str | None = None, archived: bool = False, name: str | None = None) -> Path:
    base = root / ("archived_sessions" if archived else "sessions/2026/01/01")
    meta = {"id": sid, "cwd": str(cwd), "source": source, "timestamp": "2026-01-01T00:00:00Z"}
    if parent:
        meta["parent_thread_id"] = parent
    else:
        meta["thread_source"] = "user"
    rows = [
        {"type": "session_meta", "payload": meta},
        {"type": "response_item", "payload": {"type": "message", "role": "user",
                                              "content": [{"type": "input_text", "text": "<environment_context>x"}]}},
        {"type": "event_msg", "payload": {"type": "task_started"}},
        {"type": "response_item", "payload": {"type": "message", "role": "user",
                                              "content": [{"type": "input_text", "text": prompt}]}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": prompt}},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                                              "content": [{"type": "output_text", "text": "done"}]}},
        {"type": "turn_context", "payload": {"model": "gpt-test"}},
    ]
    path = write_jsonl(base / f"rollout-2026-01-01T00-00-00-{sid}.jsonl", rows)
    if name:
        with open(root / "session_index.jsonl", "a") as stream:
            stream.write(json.dumps({"id": sid, "thread_name": name}) + "\n")
    return path


def _varint(value: int) -> bytes:
    data = bytearray()
    while value > 127:
        data.append((value & 127) | 128)
        value >>= 7
    return bytes(data + bytes([value]))


def pb(number: int, value) -> bytes:
    if isinstance(value, str):
        value = value.encode()
    return _varint(number * 8 + 2) + _varint(len(value)) + value


def cursor_session(root: Path, sid: str, project: Path, name: str = "Fixture chat",
                   subagent: bool = False, model: str | None = None) -> Path:
    """Same layout and protobuf graph as Cursor CLI."""
    db = root / "chats" / hashlib.md5(str(project).encode()).hexdigest() / sid / "store.db"
    db.parent.mkdir(parents=True)
    con = sqlite3.connect(db)
    con.executescript("CREATE TABLE blobs(id TEXT PRIMARY KEY, data BLOB);"
                      "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);")

    def blob(data: bytes) -> bytes:
        ref = hashlib.sha256(data).digest()
        con.execute("insert or ignore into blobs values (?,?)", (ref.hex(), data))
        return ref

    user = blob(pb(1, "hello cursor"))
    answer = blob(pb(1, pb(1, "remembered")))
    turn = blob(pb(1, pb(1, user) + pb(2, answer)))
    state = blob(pb(8, turn) + pb(9, project.as_uri()))
    meta = {"agentId": sid, "latestRootBlobId": state.hex(), "name": name}
    if model is not None:
        meta["lastUsedModel"] = model
    if subagent:
        meta["subagentInfo"] = {"parent": "x"}
    con.execute("insert into meta values (?,?)", ("0", json.dumps(meta).encode().hex()))
    con.commit()
    con.close()
    (db.parent / "meta.json").write_text(json.dumps({"schemaVersion": 1, "isSubagent": subagent}))
    transcripts = root / "projects" / "proj" / "agent-transcripts" / sid
    transcripts.mkdir(parents=True)
    (transcripts / f"{sid}.jsonl").write_text("{}\n")
    return db


def provenance(state_dir: Path, agent: str, sid: str, overrides: dict | None = None, **more) -> Path:
    directory = state_dir / "management" / "provenance"
    for path in (state_dir, state_dir / "management", directory):
        path.mkdir(mode=0o700, exist_ok=True)
        os.chmod(path, 0o700)
    row = {"schema": 1, "agent": agent, "session_id": sid, "origin": "mcp", "first_seen": 1.0,
           "job_ids": ["1018-000000-abcdef"]}
    row.update(overrides or {}, **more)
    path = directory / (hashlib.sha256(f"{agent}:{sid}".encode()).hexdigest() + ".json")
    path.write_text(json.dumps(row))
    os.chmod(path, 0o600)
    return path


FAKE_CODEX = r'''#!{python}
import json, os, sys, pathlib
args = sys.argv[1:]
log = pathlib.Path(os.environ["FAKE_LOG"])
with log.open("a") as f:
    f.write(json.dumps(args) + "\n")
helps = {{"resume": "Usage: codex resume [OPTIONS] [SESSION_ID]\n --include-non-interactive",
         "fork": "Usage: codex fork [OPTIONS] [SESSION_ID]",
         "archive": "Usage: codex archive [OPTIONS] <SESSION>" if not os.environ.get("FAKE_NO_ARCHIVE") else "nope",
         "unarchive": "Usage: codex unarchive [OPTIONS] <SESSION>",
         "delete": "nope" if os.environ.get("FAKE_NO_DELETE") else "Usage: codex delete [OPTIONS] <SESSION>\n" +
                   ("" if os.environ.get("FAKE_NO_FORCE") else " --force")}}
if args[-1:] == ["--help"]:
    print(helps.get(args[0] if len(args) > 1 else "", "codex help"))
    raise SystemExit(0)
home = pathlib.Path(os.environ["CODEX_HOME"])
if args and args[0] == "delete":
    import time, sqlite3, uuid
    assert args[1] == "--force" and str(uuid.UUID(args[2])) == args[2]
    if os.environ.get("FAKE_ENV_LOG"):
        pathlib.Path(os.environ["FAKE_ENV_LOG"]).write_text(json.dumps({{"home": str(home), "stdin": sys.stdin.read()}}))
    if os.environ.get("FAKE_DELETE_SLEEP"):
        time.sleep(float(os.environ["FAKE_DELETE_SLEEP"]))
    if os.environ.get("FAKE_DELETE_FLOOD"):
        print("x" * 100000, flush=True)
        raise SystemExit(0)
    if os.environ.get("FAKE_DELETE_ERROR"):
        print(os.environ["FAKE_DELETE_ERROR"], file=sys.stderr)
        raise SystemExit(1)
    if os.environ.get("FAKE_DELETE_NOOP"):
        raise SystemExit(0)
    sid = args[2]
    rollouts = []
    for base in (home / "sessions", home / "archived_sessions"):
        for p in base.rglob("rollout-*.jsonl"):
            meta = json.loads(p.read_text().splitlines()[0])["payload"]
            rollouts.append((p, meta))
    if any(m.get("forked_from_id") == sid for p, m in rollouts):
        print("unsafe fork references", file=sys.stderr)
        raise SystemExit(1)
    ids = {{sid}}
    while True:
        expanded = ids | {{m["id"] for p, m in rollouts if m.get("parent_thread_id") in ids}}
        if expanded == ids:
            break
        ids = expanded
    for p, m in rollouts:
        if m["id"] in ids:
            p.unlink()
    for db in home.glob("state_*.sqlite"):
        with sqlite3.connect(db) as con:
            for deleted in ids:
                if not os.environ.get("FAKE_DELETE_KEEP_INDEX"):
                    con.execute("DELETE FROM threads WHERE id=?", (deleted,))
    index = home / "session_index.jsonl"
    if index.exists():
        index.write_text("".join(line + "\n" for line in index.read_text().splitlines() if json.loads(line)["id"] not in ids))
    raise SystemExit(1 if os.environ.get("FAKE_DELETE_EXIT_AFTER") else 0)
if args and args[0] in ("archive", "unarchive"):
    sid = args[1]
    src, dst = (home / "sessions", home / "archived_sessions") if args[0] == "archive" else \
               (home / "archived_sessions", home / "sessions/2026/01/01")
    found = [p for p in src.rglob("rollout-*" + sid + ".jsonl")]
    if not found:
        print("not found", file=sys.stderr); raise SystemExit(1)
    dst.mkdir(parents=True, exist_ok=True)
    found[0].rename(dst / found[0].name)
    for db in home.glob("state_*.sqlite"):
        import sqlite3
        with sqlite3.connect(db) as con:
            con.execute("UPDATE threads SET rollout_path=?, archived=? WHERE id=?",
                        (str(dst / found[0].name), int(args[0] == "archive"), sid))
    raise SystemExit(0)
raise SystemExit(0)
'''

FAKE_HELP = r'''#!{python}
import sys
if "--help" in sys.argv:
    print("{text}")
'''


def fake_bins(directory: Path, log: Path) -> dict:
    directory.mkdir(parents=True, exist_ok=True)
    bins = {}
    for name, body in (("codex", FAKE_CODEX.format(python=sys.executable)),
                       ("claude", FAKE_HELP.format(python=sys.executable, text="--resume <id> --fork-session")),
                       ("cursor-agent", FAKE_HELP.format(python=sys.executable, text="cursor agent --resume [chatId]"))):
        path = directory / name
        path.write_text(body)
        path.chmod(0o755)
        bins[name] = path
    log.touch()
    return bins
