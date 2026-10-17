"""Dependency-free table/detail rendering for headless commands.

Adapted from the MIT-licensed ai-bridge server CLI (ai_bridge/cli_output.py).
JSON output never passes through here; values are escaped, never modified.
"""
from __future__ import annotations

import json
import math
import time
from datetime import datetime

from .textsafe import cell_width, clip, safe, wrap

IDENTITY_KEYS = {"session_id", "selector", "id", "job_id", "task_id", "parent_session_id", "batch_id",
                 "plan_id", "storage_path"}
COLORS = {"active": "32", "running": "32", "uncertain": "33", "recent": "33", "lost": "33",
          "cleanup_incomplete": "33", "failed": "31", "mcp": "35", "trashed": "36", "restored": "32"}


def relative(value, now: float | None = None) -> str:
    if value in (None, ""):
        return "-"
    now = time.time() if now is None else now
    try:
        stamp = float(value) if isinstance(value, (int, float)) else datetime.fromisoformat(
            str(value).replace("Z", "+00:00")).timestamp()
        if not math.isfinite(stamp):
            return safe(value)
        seconds = now - stamp
        amount = abs(seconds)
        for unit, size in (("y", 31536000), ("mo", 2592000), ("d", 86400), ("h", 3600), ("m", 60)):
            if amount >= size:
                age = f"{int(amount // size)}{unit}"
                return f"{age} ago" if seconds >= 0 else f"in {age}"
        return "just now" if seconds >= 0 else "in <1m"
    except (ValueError, TypeError, OverflowError, OSError):
        return safe(value)


def value_text(value) -> str:
    if value is None:
        return "-"
    if isinstance(value, (dict, list, bool)):
        return safe(json.dumps(value, ensure_ascii=False))
    return safe(value)


def short_id(value) -> str:
    text = value_text(value)
    return text[:8] if len(text) == 36 else text


def details(data, width: int) -> list[str]:
    if not isinstance(data, dict):
        return list(wrap(value_text(data), width))
    lines = []
    for key, value in data.items():
        label = safe(key) + ": "
        text = value_text(value)
        if key in IDENTITY_KEYS:
            lines.append(label + text)  # exact and copyable even when narrow
        else:
            lines.extend(wrap(label + text, width))
    return lines


def table(headers: list[str], rows: list[list[str]], width: int, color: bool = False) -> list[str]:
    if not rows:
        return ["(none)"]
    if width < 70:  # stacked records read better than a crushed grid
        lines = []
        for index, row in enumerate(rows):
            if index:
                lines.append("")
            for header, text in zip(headers, row):
                lines.extend(wrap(f"{header}: {text}", width))
        return lines
    widths = [max(cell_width(h), *(cell_width(r[i]) for r in rows)) for i, h in enumerate(headers)]
    while sum(widths) + 2 * (len(widths) - 1) > width and max(widths) > 4:
        index = max(range(len(widths)), key=widths.__getitem__)
        widths[index] -= 1

    def line(row):
        cells = []
        for text, size in zip(row, widths):
            shown = clip(text, size)
            padding = " " * (size - cell_width(shown))
            code = COLORS.get(text)
            if color and code:
                shown = f"\x1b[{code}m{shown}\x1b[0m"
            cells.append(shown + padding)
        return "  ".join(cells).rstrip()
    return [line(headers), line(["-" * n for n in widths]), *(line(r) for r in rows)]


def render_sessions(result: dict, width: int, color: bool) -> list[str]:
    now = time.time()
    rows = result["sessions"]
    headers = ["ID", "AGENT", "ORIGIN", "STATUS", "UPDATED", "TITLE", "PROJECT"]
    cells = [[short_id(r["session_id"]), r["agent"], r["origin"], r["status"], relative(r["updated"], now),
              ("[A] " if r.get("archived") else "") + ("[native-archived] " if r.get("native_archived") else "")
              + value_text(r["title"]), value_text(r.get("cwd"))] for r in rows]
    lines = table(headers, cells, width, color)
    lines += list(wrap(f"Sessions: {len(rows)} shown, {result['total']} matching "
                       f"(offset {result['offset']}).", width))
    if result.get("next_offset") is not None:
        lines += list(wrap(f"Next page: --offset {result['next_offset']}", width))
    lines += list(wrap("IDs shortened; use --json for full IDs, or `sessions inspect agent:ID-PREFIX`.", width))
    for agent, error in (result.get("errors") or {}).items():
        lines += list(wrap(f"Warning: {safe(agent)} store unreadable: {safe(error)}", width))
    for warning in result.get("warnings") or []:
        lines += list(wrap(f"Warning: {safe(warning)}", width))
    return lines


def render_jobs(rows: list[dict], width: int, color: bool) -> list[str]:
    now = time.time()
    cells = [[value_text(r.get("job_id")), value_text(r.get("agent")), value_text(r.get("status")),
              short_id(r.get("session_id")), relative(r.get("started"), now), value_text(r.get("cwd"))]
             for r in rows]
    lines = table(["JOB ID", "AGENT", "STATUS", "SESSION", "STARTED", "WORKSPACE"], cells, width, color)
    lines += list(wrap(f"Bridge agent jobs: {len(rows)}. Status is derived read-only from job records.", width))
    return lines


def render_tasks(rows: list[dict], width: int, color: bool) -> list[str]:
    now = time.time()
    cells = [[value_text(r.get("task_id")), relative(r.get("created"), now),
              value_text(r.get("workspace") or r.get("error"))] for r in rows]
    return table(["TASK ID", "CREATED", "WORKSPACE"], cells, width, color) + [f"Tasks: {len(rows)}."]


def render_plan(plan: dict, width: int, color: bool) -> list[str]:
    lines = [f"Delete preview (plan {plan['plan_id']}): {plan['allowed']} allowed, {plan['blocked']} blocked."]
    for item in plan["items"]:
        mark = "OK " if item["allowed"] else "NO "
        lines += list(wrap(f"{mark}{item['agent']}:{item['session_id']}  {safe(item['title'])}", width))
        lines += list(wrap(f"    method: {item['method']} - {item['note']}", width))
        for entry in item["entries"]:
            lines += list(wrap(f"    {entry['kind']}: {safe(entry['path'])}", width))
        for blocker in item["blockers"]:
            lines += list(wrap(f"    blocked: {safe(blocker)}", width))
    if plan.get("next"):
        lines += ["", *wrap(f"Next: {plan['next']}", width)]
    return lines


def render_batches(rows: list[dict], width: int, color: bool) -> list[str]:
    if not rows:
        return ["Trash is empty."]
    now = time.time()
    lines = []
    for batch in rows:
        lines.append(f"{batch['batch_id']}  ({relative(batch.get('created'), now)})"
                     + (f"  ERROR: {safe(batch['error'])}" if batch.get("error") else ""))
        for item in batch.get("items", []):
            lines += list(wrap(f"    {item['status']:<9} {item['agent']}:{item['session_id']}  "
                               f"{safe(item.get('title') or '')}", width))
    return lines


def render(kind: str, result, width: int = 100, color: bool = False) -> str:
    width = max(20, width)
    renderers = {"sessions": render_sessions, "jobs": render_jobs, "tasks": render_tasks,
                 "plan": render_plan, "batches": render_batches}
    if kind in renderers:
        lines = renderers[kind](result, width, color)
    elif kind == "history":
        lines = []
        for message in result:
            lines += list(wrap(f"[{message.get('role')}] {safe(' '.join(str(message.get('text', '')).split()))}",
                               width))
    else:
        data = dict(result) if isinstance(result, dict) else result
        messages = data.pop("history", None) if isinstance(data, dict) else None
        lines = details(data, width)
        if messages:
            lines += ["", f"history (last {len(messages)}):"]
            lines += render("history", messages, width).splitlines()
    return "\n".join(lines)
