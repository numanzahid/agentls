"""Codex rollouts: $CODEX_HOME/sessions/**/rollout-*.jsonl (+ archived_sessions/).

Rollout files are indexed by Codex's own SQLite state, so this module only
ever reads them. Archive/delete go through the codex CLI itself (see trash.py).
Adapted from the MIT-licensed ai-bridge server (ai_bridge/agents.py).
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from .. import paths
from ..model import Session
from ..textsafe import UUID_RE, is_uuid
from .common import is_noise, iter_jsonl, model_name, tail_jsonl, tool_summary

HEAD_LINES = 300


def files(root: Path | None = None, include_archived: bool = True) -> list[tuple[Path, bool]]:
    root = root or paths.codex_dir()
    out: list[tuple[Path, bool]] = []
    for base, archived in ((root / "sessions", False), (root / "archived_sessions", True)):
        if archived and not include_archived:
            continue
        if not base.is_dir() or base.is_symlink():
            continue
        # os.walk does not follow directory symlinks by default.
        for directory, _dirs, names in os.walk(base):
            for name in names:
                if name.startswith("rollout-") and name.endswith(".jsonl"):
                    path = Path(directory) / name
                    if not path.is_symlink():
                        out.append((path, archived))
    return out


def titles(root: Path | None = None) -> dict[str, str]:
    root = root or paths.codex_dir()
    out = {}
    for entry in iter_jsonl(root / "session_index.jsonl"):
        if is_uuid(entry.get("id")) and entry.get("thread_name"):
            out[entry["id"]] = entry["thread_name"]  # later entries win (renames)
    return out


def entry_messages(entry: dict):
    payload = entry.get("payload") or {}
    kind = payload.get("type")
    if entry.get("type") == "event_msg":
        if kind == "user_message" and payload.get("message") and not is_noise(payload["message"]):
            yield "user", payload["message"]
        elif kind == "agent_message" and payload.get("message"):
            yield "assistant", payload["message"]
        elif kind == "item_completed":
            item = payload.get("item") or {}
            role = {"UserMessage": "user", "AgentMessage": "assistant"}.get(item.get("type"))
            if role:
                text = "\n".join(b.get("text", "") for b in item.get("content", []) if isinstance(b, dict))
                if text and not (role == "user" and is_noise(text)):
                    yield role, text
    elif entry.get("type") == "response_item":
        if kind in ("function_call", "custom_tool_call", "local_shell_call"):
            args = payload.get("arguments") or payload.get("input") or payload.get("action")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    pass
            if isinstance(args, dict) and isinstance(args.get("command"), list):
                args = {"command": " ".join(map(str, args["command"]))}
            yield "tool", tool_summary(payload.get("name") or kind, args)
        elif kind == "message":
            texts = [c.get("text", "") for c in payload.get("content") or []
                     if isinstance(c, dict) and c.get("type") in ("input_text", "output_text")]
            text = "\n".join(t for t in texts if t and not is_noise(t))
            if text and payload.get("role") in ("user", "assistant"):
                yield payload["role"], text


def _messages(entries):
    # Per turn, response items are canonical; matching event_msg copies are skipped.
    def decode(turn):
        response_roles = {role for e in turn if e.get("type") == "response_item"
                          for role, _ in entry_messages(e)}
        for e in turn:
            for role, text in entry_messages(e):
                if e.get("type") == "event_msg" and role in response_roles:
                    continue
                yield {"role": role, "text": text, "time": e.get("timestamp")}
    turn: list[dict] = []
    for entry in entries:
        if entry.get("type") == "event_msg" and (entry.get("payload") or {}).get("type") == "task_started":
            yield from decode(turn)
            turn = []
        turn.append(entry)
    yield from decode(turn)


def _timestamp(value) -> float | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def parse(path: Path, root: Path, info: os.stat_result, archived: bool,
          names: dict[str, str]) -> Session:
    sid = cwd = first = parent = None
    source = thread_source = None
    created = None
    for entry in iter_jsonl(path, max_lines=HEAD_LINES):
        payload = entry.get("payload") or {}
        if entry.get("type") == "session_meta":
            source = payload.get("source")
            thread_source = payload.get("thread_source")
            sid = payload.get("id") or sid
            cwd = payload.get("cwd") or cwd
            created = created or _timestamp(payload.get("timestamp") or entry.get("timestamp"))
            parent = payload.get("parent_thread_id") or parent
            if not parent and isinstance(source, dict):
                sub = source.get("subagent")
                spawn = sub.get("thread_spawn") if isinstance(sub, dict) else None
                if isinstance(spawn, dict):
                    parent = spawn.get("parent_thread_id")
        if first is None:
            for role, text in entry_messages(entry):
                if role == "user":
                    first = text
                    break
        if sid and first:
            break
    if not is_uuid(sid):
        match = UUID_RE.search(path.name)
        sid = match.group(0) if match else path.stem
    model = None
    for entry in reversed(tail_jsonl(path)):
        if entry.get("type") == "turn_context":
            payload = entry.get("payload")
            model = model_name(payload.get("model")) if isinstance(payload, dict) else None
            break
    subagent = bool(parent) or (isinstance(source, dict) and "subagent" in source)
    session = Session(
        agent="codex", session_id=sid, storage_path=str(path), storage_root=str(root),
        title=" ".join(str(names.get(sid) or first or "(no prompt)").split())[:200],
        cwd=cwd if isinstance(cwd, str) else None, updated=info.st_mtime, created=created,
        mode=("noninteractive" if source == "exec" else
              "interactive" if source in ("cli", "vscode") else "unknown"),
        native_source=source if isinstance(source, str) else ("subagent" if subagent else None),
        session_type="subagent" if subagent else "main" if thread_source == "user" or source in ("cli", "vscode", "exec") else "unknown",
        parent_session_id=parent if is_uuid(parent) else None,
        last_used_model=model, native_archived=archived,
    )
    session.resumable = bool(session.cwd and os.path.isdir(session.cwd)) and not archived
    if archived:
        session.resume_note = "Natively archived by Codex; unarchive (restore) it first."
    elif not session.resumable:
        session.resume_note = "Saved project directory is missing." if session.cwd else "No project directory recorded."
    return session


def history(session: Session) -> list[dict]:
    return list(_messages(iter_jsonl(Path(session.storage_path))))
