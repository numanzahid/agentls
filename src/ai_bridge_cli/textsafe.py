"""Terminal-safe text: stored titles and paths are untrusted input."""
from __future__ import annotations

import re
import unicodedata

UUID_RE = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")


def is_uuid(value) -> bool:
    return isinstance(value, str) and UUID_RE.fullmatch(value) is not None


def safe(value) -> str:
    """Escape control/format characters so data cannot inject escape sequences."""
    text = "" if value is None else str(value)
    return "".join(
        c if not unicodedata.category(c).startswith("C") else f"\\u{ord(c):04x}"
        for c in text
    )


def one_line(value, limit: int = 160) -> str:
    """Collapse whitespace (including newlines) then escape the remainder."""
    return safe(" ".join(str(value or "").split()))[:limit]


def cell_width(text: str) -> int:
    return sum(
        0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1
        for c in text
    )


def clip(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if cell_width(text) <= width:
        return text
    out = ""
    for c in text:
        if cell_width(out + c) > width - 1:
            break
        out += c
    return out + "…"


def wrap(text: str, width: int):
    """Wrap by terminal cells, including paths without spaces."""
    width = max(1, width)
    line = ""
    for c in text:
        if line and cell_width(line + c) > width:
            yield line
            line = ""
        line += c
    yield line
