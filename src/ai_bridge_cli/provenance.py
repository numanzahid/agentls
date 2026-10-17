"""READ-ONLY reader for the MCP server's session provenance records.

Contract (owned by the ai-bridge server):
    $AI_BRIDGE_STATE_DIR/management/provenance/<sha256(agent + ':' + session_id)>.json
    {"schema": 1, "agent": ..., "session_id": ..., "origin": "mcp",
     "first_seen": <epoch>, "job_ids": [...]}

Only a record that is private, owned by this user, a single-link regular file
reached without symlinks, and whose agent/session_id match the lookup counts as
evidence. Native "headless"/exec mode is never evidence of MCP origin.
This module never creates, locks or writes anything in that directory.
"""
from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import re
from pathlib import Path

from . import paths
from .fsutil import UnsafePath, check_private, read_bytes_nofollow
from .textsafe import is_uuid

JOB_ID = re.compile(r"[a-z0-9-]{4,40}")
MAX_BYTES = 16384
MAX_JOBS = 64


def filename(agent: str, session_id: str) -> str:
    return hashlib.sha256(f"{agent}:{session_id}".encode("utf-8")).hexdigest() + ".json"


def validate(row, agent: str, session_id: str) -> dict:
    if (not isinstance(row, dict) or type(row.get("schema")) is not int or row["schema"] != 1 or
            row.get("agent") != agent or row.get("session_id") != session_id or
            row.get("origin") != "mcp" or
            type(row.get("first_seen")) not in (int, float) or not math.isfinite(row["first_seen"]) or
            row["first_seen"] < 0 or not isinstance(row.get("job_ids"), list) or
            not 1 <= len(row["job_ids"]) <= MAX_JOBS or
            any(not isinstance(j, str) or not JOB_ID.fullmatch(j) for j in row["job_ids"]) or
            len(set(row["job_ids"])) != len(row["job_ids"])):
        raise ValueError("record does not match the provenance schema or this session")
    return row


class ProvenanceReader:
    """Open the directory once per refresh; look up many sessions cheaply.

    state: "ok" (directory verified), "absent_bridge" (no bridge state at all),
    "missing" (bridge state exists but no provenance directory), "unsafe".
    """

    def __init__(self, state_dir: Path | None = None, install_root: Path | None = None):
        self.state_dir = Path(state_dir or paths.bridge_state_dir()).absolute()
        self.install_root = Path(install_root or paths.bridge_install_root()).absolute()
        self.fd: int | None = None
        self.state = "missing"
        self.note: str | None = None
        self.names: set[str] = set()
        # Earliest verified first_seen: provenance recording was demonstrably active from then on.
        self.epoch: float | None = None

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc):
        self.close()

    @staticmethod
    def _absent(path: Path) -> bool:
        """lstat-based: a dangling symlink is present (and suspicious), not absent."""
        try:
            os.lstat(path)
            return False
        except FileNotFoundError:
            return True

    def open(self):
        if self._absent(self.state_dir):
            if self._absent(self.install_root):
                self.state = "absent_bridge"
                self.note = "No ai-bridge state or installation on this account."
            else:
                self.state = "missing"
                self.note = "ai-bridge is installed but has no state directory; origin cannot be verified."
            return
        if os.path.islink(self.state_dir):
            self.state, self.note = "unsafe", "Bridge state directory is a symlink."
            return
        parts = (*self.state_dir.parts[1:], "management", "provenance")
        if ".." in parts:
            self.state, self.note = "unsafe", "State path contains '..'."
            return
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            for index, part in enumerate(parts):
                try:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                    dir_fd=fd)
                except FileNotFoundError:
                    self.state = "missing"
                    self.note = ("The bridge has not recorded MCP provenance on this account "
                                 "(none yet, or the server predates provenance records).")
                    return
                except OSError as error:
                    self.state = "unsafe"
                    self.note = ("Symlink in provenance path." if error.errno in (errno.ELOOP, errno.ENOTDIR)
                                 else f"Cannot open provenance path: {error.strerror}")
                    return
                os.close(fd)
                fd = child
                # Like the server: the state dir, management/ and provenance/ must be private.
                if index >= len(parts) - 3:
                    check_private(os.fstat(fd), directory=True)
            self.names = {n for n in os.listdir(fd) if n.endswith(".json") and len(n) == 69}
            self.fd, fd = fd, None
            self.state = "ok"
            self._compute_epoch()
        except (UnsafePath, OSError) as error:
            self.state, self.note = "unsafe", f"Provenance directory rejected: {error}"
        finally:
            if fd is not None:
                os.close(fd)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def _compute_epoch(self, limit: int = 10000) -> None:
        for name in sorted(self.names)[:limit]:
            try:
                row = json.loads(read_bytes_nofollow(Path(name), MAX_BYTES, private=True, dir_fd=self.fd))
                if isinstance(row, dict) and filename(row.get("agent"), row.get("session_id")) == name:
                    row = validate(row, row["agent"], row["session_id"])
                    self.epoch = row["first_seen"] if self.epoch is None else min(self.epoch, row["first_seen"])
            except (UnsafePath, OSError, ValueError, TypeError):
                continue

    def lookup(self, agent: str, session_id: str, created: float | None = None,
               bridge_referenced: bool = False, jobs_complete: bool = True) -> tuple[str, dict | None, str | None]:
        """(origin, record, note) with origin in mcp|direct|unknown.

        Direct must be *proven*; a missing record alone is not proof (sessions
        used through MCP before the server recorded provenance have none):
        * no ai-bridge state or installation exists on this account, or
        * no record, the session was created after the provenance epoch (the
          earliest verified first_seen, i.e. recording was active), no bridge
          job references it, and every bridge job record could be read.
        """
        if not is_uuid(session_id) or agent not in paths.AGENTS:
            return "unknown", None, "Session ID is not an exact UUID."
        if self.state == "absent_bridge":
            return "direct", None, self.note
        if self.state != "ok" or self.fd is None:
            return "unknown", None, self.note
        name = filename(agent, session_id)
        if name in self.names:
            try:
                row = validate(json.loads(read_bytes_nofollow(Path(name), MAX_BYTES, private=True,
                                                              dir_fd=self.fd)), agent, session_id)
            except (UnsafePath, OSError, ValueError) as error:  # incl. vanished/dangling: not "missing"
                return "unknown", None, f"Provenance record rejected: {error}"
            return "mcp", row, "Verified MCP provenance record."
        if bridge_referenced:
            return "unknown", None, "No provenance record, but bridge jobs reference this session."
        if not jobs_complete:
            return "unknown", None, "No provenance record; some bridge job records are unreadable."
        if self.epoch is None:
            return "unknown", None, "No provenance record, and recording has never been verified on this account."
        if created is None or created <= self.epoch:
            return "unknown", None, ("No provenance record, but the session predates verified provenance "
                                     "recording; it may have been used through MCP.")
        return "direct", None, "No MCP provenance record; created after provenance recording began."


def lookup(agent: str, session_id: str, state_dir: Path | None = None, **kwargs):
    with ProvenanceReader(state_dir) as reader:
        return reader.lookup(agent, session_id, **kwargs)
