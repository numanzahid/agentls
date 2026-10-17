"""JSONL helpers shared by the transcript scanners.

Adapted from the MIT-licensed ai-bridge server (ai_bridge/agents.py).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from ..fsutil import open_regular, read_regular

TAIL_BYTES = 262144

NOISE_PREFIXES = (
    "# AGENTS.md instructions",
    "<user_info>",
    "<environment_context>",
    "<user_instructions>",
    "<command-",
    "<local-command",
    "Caveat:",
    "<system-reminder>",
    "<permissions instructions>",
)


HEAD_BYTES = 8 << 20        # metadata scans read at most this much of a transcript
HISTORY_BYTES = 256 << 20   # previews/history read at most this much
MAX_LINE = 16 << 20         # longer records are skipped, never buffered whole


def iter_jsonl(path: Path, max_lines: int | None = None, max_bytes: int = HISTORY_BYTES):
    """JSON objects from a regular file, bounded in lines, bytes and line length.

    The file is opened O_NOFOLLOW|O_NONBLOCK and must be a regular file, so a
    symlink or FIFO named like a transcript raises instead of hanging. A
    missing file yields nothing.
    """
    try:
        fd, _info = open_regular(path)
    except FileNotFoundError:
        return
    with os.fdopen(fd, "rb") as stream:
        used = lines = 0
        while used < max_bytes and (max_lines is None or lines < max_lines):
            raw = stream.readline(min(MAX_LINE, max_bytes - used))
            if not raw:
                return
            used += len(raw)
            if not raw.endswith(b"\n") and len(raw) >= MAX_LINE:
                while True:  # discard the rest of an oversized record
                    rest = stream.readline(MAX_LINE)
                    used += len(rest)
                    if not rest or rest.endswith(b"\n") or used >= max_bytes:
                        break
                continue
            lines += 1
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(obj, dict):
                yield obj


def tail_jsonl(path: Path, max_bytes: int = TAIL_BYTES) -> list[dict]:
    """Records from the end of a (possibly huge) transcript, oldest first."""
    try:
        fd, info = open_regular(path)
    except FileNotFoundError:
        return []
    with os.fdopen(fd, "rb") as stream:
        offset = max(0, info.st_size - max_bytes)
        stream.seek(offset)
        lines = stream.read(max_bytes).splitlines()
    if offset:
        lines = lines[1:]  # the first record may be cut
    out = []
    for raw in lines:
        try:
            obj = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def read_json_file(path: Path, max_bytes: int = 65536):
    """Small JSON side files (meta.json, registries): bounded, regular, no-follow."""
    return json.loads(read_regular(path, max_bytes))


def is_noise(text: str) -> bool:
    return text.lstrip().startswith(NOISE_PREFIXES)


def tool_summary(name: str, inp) -> str:
    detail = ""
    if isinstance(inp, dict):
        for key in ("command", "cmd", "file_path", "path", "pattern", "url", "query", "description"):
            if inp.get(key):
                detail = str(inp[key])
                break
        if not detail and inp:
            detail = json.dumps(inp)[:200]
    elif isinstance(inp, str):
        detail = inp
    detail = " ".join(detail.split())[:200]
    return f"[{name}] {detail}".strip()


def blocks_text(content) -> tuple[list[str], list[str]]:
    texts, tools = [], []
    if isinstance(content, str):
        return [content], []
    for block in content or []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text" and block.get("text"):
            texts.append(block["text"])
        elif kind == "tool_use":
            tools.append(tool_summary(block.get("name", "tool"), block.get("input")))
    return texts, tools


def model_name(value) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    # "<synthetic>" placeholders are not model invocations.
    if (not value or len(value) > 512 or any(ord(c) < 32 for c in value) or
            (value.startswith("<") and value.endswith(">"))):
        return None
    return value
