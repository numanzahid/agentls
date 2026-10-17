"""In-memory catalog: one refresh = one scan + one provenance/overlay/job/activity pass.

Everything after refresh() (filtering, sorting, selection, inspection) is a
lookup in memory, so selecting N rows never rescans N times.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from . import activity as activity_mod
from . import paths
from .bridge import BridgeState, identity_matches, uncertain
from .model import ORIGINS, SORT_KEYS, STATUSES, Session
from .overlay import Overlays
from .provenance import ProvenanceReader
from .scanners import Scanner
from .textsafe import is_uuid


@dataclass
class Query:
    agents: tuple[str, ...] = ()
    origin: str | None = None
    status: str | None = None
    project: str | None = None
    search: str = ""
    archived: str = "hide"          # hide | include | only
    native_archived: bool = False   # include Codex sessions archived via `codex archive`
    sort: str = "updated"
    reverse: bool = False

    def validate(self) -> "Query":
        if any(a not in paths.AGENTS for a in self.agents):
            raise ValueError(f"Unknown agent; choose from {', '.join(paths.AGENTS)}.")
        if self.origin not in (None, *ORIGINS):
            raise ValueError(f"Unknown origin; choose from {', '.join(ORIGINS)}.")
        if self.status not in (None, *STATUSES):
            raise ValueError(f"Unknown status; choose from {', '.join(STATUSES)}.")
        if self.sort not in SORT_KEYS:
            raise ValueError(f"Unknown sort key; choose from {', '.join(SORT_KEYS)}.")
        if self.archived not in ("hide", "include", "only"):
            raise ValueError("archived must be hide, include or only.")
        return self


def _sort_key(name: str):
    if name == "title":
        return lambda s: (s.display_title.casefold(), -s.updated)
    if name == "agent":
        return lambda s: (s.agent, -s.updated)
    if name == "project":
        return lambda s: ((s.cwd or "￿").casefold(), -s.updated)
    return lambda s: -s.updated


@dataclass
class Catalog:
    scanner: Scanner = field(default_factory=Scanner)
    overlays: Overlays = field(default_factory=Overlays)
    bridge: BridgeState = field(default_factory=BridgeState)
    sessions: list[Session] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    provenance_state: str = "unknown"
    provenance_note: str | None = None
    refreshed: float = 0.0
    scan_seconds: float = 0.0
    activity: activity_mod.Activity | None = None
    _index: dict = field(default_factory=dict)

    def refresh(self, probe_activity: bool = True) -> "Catalog":
        result = self.scanner.scan()
        self.errors = dict(result.errors)
        self.warnings = []
        self.scan_seconds = result.seconds
        sessions = result.sessions
        try:
            own = self.overlays.load()
        except Exception as error:  # defensive: never lose the listing over overlays
            own, self.overlays.error = {}, str(error)
        if self.overlays.error:
            self.warnings.append(self.overlays.error)
        jobs_complete = True
        try:
            jobs = self.bridge.session_jobs()
            if self.bridge.problems:
                jobs_complete = False
                self.warnings.append(f"{len(self.bridge.problems)} bridge job record(s) unreadable; "
                                     "status and origin are shown as uncertain where it matters.")
        except Exception as error:
            jobs, jobs_complete = {}, False
            self.warnings.append(f"Bridge job records unreadable: {error}")
        self.activity = activity_mod.snapshot() if probe_activity else activity_mod.Activity()
        now = time.time()
        with ProvenanceReader() as reader:
            self.provenance_state, self.provenance_note = reader.state, reader.note
            for s in sessions:
                s.origin, s.provenance, s.origin_note = reader.lookup(
                    s.agent, s.session_id, created=s.created,
                    bridge_referenced=(s.agent, s.session_id.lower()) in jobs, jobs_complete=jobs_complete)
                if is_uuid(s.session_id):
                    state = self.overlays.resolve(s.agent, s.session_id, own)
                    s.alias = state.get("alias") or None
                    s.archived = bool(state.get("archived"))
                self._apply_status(s, jobs.get((s.agent, s.session_id.lower()), []), now)
        if self.provenance_state == "unsafe" and self.provenance_note:
            self.warnings.append(self.provenance_note)
        self.sessions = sessions
        self._index = {}
        for s in sessions:
            self._index.setdefault(s.key, []).append(s)
        self.refreshed = now
        return self

    def _apply_status(self, s: Session, jobs: list[dict], now: float) -> None:
        s.job_ids = [j["id"] for j in jobs]
        busy = [j for j in jobs if uncertain(j)]
        s.active_job_ids = [j["id"] for j in busy]
        s.task_id = next((j.get("task_id") for j in jobs if j.get("task_id")), None) or \
            self.bridge.task_for_cwd(s.cwd)
        running = [j for j in busy if j.get("status") == "running" and
                   identity_matches(j.get("pid"), j.get("identity"))]
        evidence = self.activity.evidence(s.agent, s.session_id, s.storage_path) if self.activity else None
        if running:
            s.status, s.status_note = "active", f"Bridge job {running[0]['id']} is running this session."
        elif evidence:
            s.status, s.status_note = "active", evidence
        elif busy:
            s.status, s.status_note = "uncertain", (f"Bridge job {busy[0]['id']} is {busy[0].get('status')} "
                                                    "and its process cannot be verified.")
        elif now - s.updated < activity_mod.RECENT_SECONDS:
            s.status, s.status_note = "recent", "Modified moments ago; it may still be in use."
        else:
            s.status, s.status_note = "idle", None

    # -- lookups -------------------------------------------------------------
    def get(self, agent: str, session_id: str) -> Session:
        matches = self._index.get((agent, session_id), [])
        if not matches:
            raise KeyError(f"{agent} session not found locally: {session_id}")
        if len(matches) > 1:
            raise ValueError(f"Ambiguous {agent} session ID: {session_id}")
        return matches[0]

    def resolve(self, selector: str, agent: str | None = None, allow_prefix: bool = False) -> Session:
        """agent:UUID (or --agent + UUID). Prefixes (>= 8 chars) only where allowed."""
        value = selector.strip()
        if ":" in value:
            named, value = value.split(":", 1)
            if agent and named != agent:
                raise ValueError("Conflicting agent selectors.")
            agent = named
        if agent is not None and agent not in paths.AGENTS:
            raise ValueError(f"Unknown agent {agent!r}; use {', '.join(paths.AGENTS)}.")
        if is_uuid(value):
            if agent:
                return self.get(agent, value)
            hits = [s for s in self.sessions if s.session_id.lower() == value.lower()]
        elif allow_prefix and len(value) >= 8:
            hits = [s for s in self.sessions if s.session_id.lower().startswith(value.lower())
                    and (agent is None or s.agent == agent)]
        else:
            raise ValueError("Use agent:UUID with the complete session ID"
                             + (" (or a unique prefix of at least 8 characters)." if allow_prefix else "."))
        if not hits:
            raise KeyError(f"Session not found locally: {selector}")
        if len(hits) > 1:
            raise ValueError(f"Selector {selector!r} matches {len(hits)} sessions; use agent:UUID.")
        return hits[0]

    def query(self, q: Query, sessions: list[Session] | None = None) -> list[Session]:
        q.validate()
        tokens = q.search.casefold().split()
        project = q.project.rstrip("/") if q.project else None
        out = []
        for s in self.sessions if sessions is None else sessions:
            if q.agents and s.agent not in q.agents:
                continue
            if s.native_archived and not q.native_archived:
                continue
            if q.archived == "hide" and s.archived or q.archived == "only" and not s.archived:
                continue
            if q.origin and s.origin != q.origin:
                continue
            if q.status and s.status != q.status:
                continue
            if project and (s.cwd or "").rstrip("/") != project:
                continue
            if tokens:
                hay = " ".join(filter(None, (s.display_title, s.title, s.cwd, s.session_id, s.agent,
                                             s.last_used_model, s.origin, s.status))).casefold()
                if not all(t in hay for t in tokens):
                    continue
            out.append(s)
        out.sort(key=_sort_key(q.sort), reverse=q.reverse)
        return out

    def projects(self) -> list[str]:
        return sorted({s.cwd for s in self.sessions if s.cwd}, key=str.casefold)

    def summary(self) -> dict:
        counts = {a: 0 for a in paths.AGENTS}
        for s in self.sessions:
            counts[s.agent] += 1
        return {"total": len(self.sessions), "by_agent": counts, "errors": self.errors,
                "warnings": self.warnings, "provenance": self.provenance_state,
                "provenance_note": self.provenance_note, "scan_seconds": round(self.scan_seconds, 3)}
