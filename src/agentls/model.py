"""Session record shared by the scanners, catalog, CLI and TUI."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

ORIGINS = ("mcp", "direct", "unknown")
# active: verified running MCP job or native process; uncertain: MCP job
# whose process cannot be verified; recent: modified moments ago (may be in use).
STATUSES = ("active", "uncertain", "recent", "idle")
SORT_KEYS = ("updated", "title", "agent", "project")


@dataclass
class Session:
    agent: str
    session_id: str
    storage_path: str
    storage_root: str
    title: str = "(no prompt)"
    cwd: str | None = None
    updated: float = 0.0
    created: float | None = None
    mode: str = "unknown"            # interactive | noninteractive | unknown
    session_type: str = "unknown"    # main | subagent | unknown
    parent_session_id: str | None = None
    native_source: str | None = None
    last_used_model: str | None = None
    messages_seen: int | None = None
    resumable: bool = False
    resume_note: str | None = None
    history_note: str | None = None
    native_archived: bool = False
    error: str | None = None
    # Filled in by the catalog.
    native_title: str | None = None
    alias: str | None = None
    archived: bool = False
    origin: str = "unknown"
    origin_note: str | None = None
    provenance: dict | None = None
    status: str = "idle"
    status_note: str | None = None
    active_job_ids: list = field(default_factory=list)
    job_ids: list = field(default_factory=list)

    @property
    def key(self) -> tuple[str, str]:
        return (self.agent, self.session_id)

    @property
    def selector(self) -> str:
        return f"{self.agent}:{self.session_id}"

    @property
    def display_title(self) -> str:
        return self.alias or self.title

    @property
    def busy(self) -> bool:
        return self.status in ("active", "uncertain")

    def to_json(self) -> dict:
        row = asdict(self)
        row["selector"] = self.selector
        row["title"] = self.display_title
        row["native_title"] = self.title
        row["title_source"] = "alias" if self.alias else "native"
        # Same field name/semantics as the MCP server's session output.
        return row
