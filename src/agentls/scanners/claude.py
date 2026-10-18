"""Claude Code transcripts: $CLAUDE_CONFIG_DIR/projects/<slug>/<uuid>.jsonl."""
from __future__ import annotations

import os
import stat
from datetime import datetime
from pathlib import Path

from .. import paths
from ..model import Session
from ..textsafe import is_uuid
from .common import HEAD_BYTES, blocks_text, is_noise, iter_jsonl, model_name, reverse_jsonl, tail_jsonl

HEAD_LINES = 400


def user_text(entry: dict) -> str | None:
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return None if is_noise(content) else content
    texts = [b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text"]
    text = "\n".join(t for t in texts if t and not is_noise(t))
    return text or None


def _timestamp(value) -> float | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _regular(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except OSError:
        return False


def files(root: Path | None = None) -> list[Path]:
    root = root or paths.claude_dir()
    projects = root / "projects"
    out = []
    try:
        project_dirs = [p for p in projects.iterdir() if p.is_dir() and not p.is_symlink()]
    except OSError:
        return []
    for project in project_dirs:
        try:
            for entry in project.iterdir():
                # lstat: symlinks, FIFOs and devices named like transcripts are never opened.
                if entry.suffix == ".jsonl" and is_uuid(entry.stem) and _regular(entry):
                    out.append(entry)
        except OSError:
            continue
    return out


def parse(path: Path, root: Path, info: os.stat_result) -> Session:
    sid = path.stem
    first = cwd = title = None
    created = None
    mode, source = "unknown", None
    count = 0
    for entry in iter_jsonl(path, max_lines=HEAD_LINES, max_bytes=HEAD_BYTES):
        if entry.get("type") in ("summary", "custom-title"):
            title = entry.get("customTitle") or entry.get("summary") or title
        if entry.get("isSidechain"):
            continue
        if created is None and entry.get("timestamp"):
            created = _timestamp(entry["timestamp"])
        if entry.get("entrypoint") == "sdk-cli" or entry.get("promptSource") == "sdk":
            mode, source = "noninteractive", "sdk-cli"
        elif mode == "unknown" and entry.get("entrypoint") == "cli":
            mode, source = "interactive", "cli"
        cwd = cwd or entry.get("cwd")
        if entry.get("type") == "user" and first is None and not entry.get("isMeta"):
            first = user_text(entry)
        if entry.get("type") in ("user", "assistant"):
            count += 1
    # Renames are cosmetic and bounded to the tail; model lookup continues back
    # through tool-heavy turns until a real assistant invocation is found.
    renamed = None
    for entry in reversed(tail_jsonl(path)):
        if renamed is None and entry.get("type") == "custom-title" and entry.get("customTitle"):
            renamed = entry["customTitle"]
        if renamed:
            break
    model = None
    for entry in reverse_jsonl(path):
        if entry.get("type") == "assistant" and not entry.get("isSidechain"):
            message = entry.get("message")
            model = model_name(message.get("model")) if isinstance(message, dict) else None
            if model:
                break
    title = renamed or title
    session = Session(
        agent="claude", session_id=sid, storage_path=str(path), storage_root=str(root),
        title=" ".join(str(title or first or "(no prompt)").split())[:200],
        cwd=cwd, updated=info.st_mtime, created=created, mode=mode, native_source=source,
        session_type="main", last_used_model=model,
        messages_seen=count if count < HEAD_LINES else None,
    )
    session.resumable = bool(cwd and os.path.isdir(cwd))
    if not session.resumable:
        session.resume_note = "Saved project directory is missing." if cwd else "No project directory recorded."
    return session


def history(session: Session) -> list[dict]:
    msgs = []
    for entry in iter_jsonl(Path(session.storage_path)):
        if entry.get("isSidechain") or entry.get("isMeta"):
            continue
        if entry.get("type") == "user":
            text = user_text(entry)
            if text:
                msgs.append({"role": "user", "text": text, "time": entry.get("timestamp")})
        elif entry.get("type") == "assistant":
            texts, tools = blocks_text((entry.get("message") or {}).get("content"))
            if texts:
                msgs.append({"role": "assistant", "text": "\n".join(texts), "time": entry.get("timestamp")})
            for tool in tools:
                msgs.append({"role": "tool", "text": tool, "time": entry.get("timestamp")})
    return msgs


def related_paths(session: Session) -> list[tuple[Path, bool]]:
    """(path, required) entries that make up one Claude session on disk."""
    root = Path(session.storage_root)
    transcript = Path(session.storage_path)
    sid = session.session_id
    return [
        (transcript, True),
        (transcript.with_suffix(""), False),          # <uuid>/ subagents, tool-results
        (root / "file-history" / sid, False),         # rewind checkpoints
        (root / "session-env" / sid, False),
    ]
