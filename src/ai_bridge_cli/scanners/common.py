"""JSONL helpers shared by the transcript scanners.

Adapted from the MIT-licensed ai-bridge server (ai_bridge/agents.py).
"""
from __future__ import annotations

import json
from pathlib import Path

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


def iter_jsonl(path: Path, max_lines: int | None = None):
    try:
        with open(path, "rb") as stream:
            for index, raw in enumerate(stream):
                if max_lines is not None and index >= max_lines:
                    break
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    continue
                if isinstance(obj, dict):
                    yield obj
    except OSError:
        return


def tail_jsonl(path: Path, max_bytes: int = TAIL_BYTES) -> list[dict]:
    """Records from the end of a (possibly huge) transcript, oldest first."""
    try:
        with open(path, "rb") as stream:
            stream.seek(0, 2)
            offset = max(0, stream.tell() - max_bytes)
            stream.seek(offset)
            lines = stream.read(max_bytes).splitlines()
    except OSError:
        return []
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
