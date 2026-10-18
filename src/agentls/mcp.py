"""READ-ONLY view of the ai-bridge MCP server's agent job records.

Used for the Origin column (a job that references a session rules out Direct)
and as a safety check before stopping, archiving or deleting a session that an
MCP job may still be using. Status is derived in memory and never written back;
nothing is ever written to or run from the server's state or installation.
"""
from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path

from . import paths
from .activity import PROC
from .fsutil import UnsafePath, read_bytes_nofollow
from .textsafe import is_uuid

JOB_ID = re.compile(r"[a-z0-9-]{4,40}")
UNCERTAIN = {"running", "cleanup_incomplete", "lost"}
MAX_JOB_BYTES = 1 << 20
MAX_OUTPUT_BYTES = 8 << 20

try:
    BOOT_ID = (PROC / "sys/kernel/random/boot_id").read_text().strip()
except OSError:
    BOOT_ID = None


def process_identity(pid) -> dict | None:
    try:
        fields = (PROC / f"{int(pid)}/stat").read_text().rsplit(") ", 1)[1].split()
        if fields[0] == "Z":
            return None
        return {"boot_id": BOOT_ID, "start_ticks": fields[19], "pgid": int(fields[2])}
    except (OSError, ValueError, IndexError, TypeError):
        return None


def identity_matches(pid, identity) -> bool:
    return bool(identity and process_identity(pid) == identity)


def uncertain(job: dict) -> bool:
    return job.get("status") in UNCERTAIN and not (
        job.get("status") == "lost" and job.get("identity_state") == "previous_boot")


class McpState:
    def __init__(self, state_dir: Path | None = None):
        self.state_dir = Path(state_dir or paths.mcp_state_dir())
        self.jobs_dir = self.state_dir / "jobs"
        self._cache: dict[str, tuple[tuple, dict]] = {}
        self.problems: list[str] = []
        self._last: list[dict] = []

    # -- job records ----------------------------------------------------
    def _read(self, path: Path, limit: int):
        return json.loads(read_bytes_nofollow(path, limit))

    def _sidecar(self, job_id: str, kind: str) -> Path | None:
        for candidate in (self.jobs_dir / ".sidecars" / f"{job_id}.{kind}.json",
                          self.jobs_dir / f"{job_id}.{kind}.json"):
            if candidate.exists() and not candidate.is_symlink():
                return candidate
        return None

    def _derive(self, meta: dict) -> dict:
        """Mirror the server's refresh logic without persisting anything."""
        meta = dict(meta)
        meta["status_derived"] = False
        if meta.get("status") == "cleanup_incomplete":
            return self._refresh_cleanup(meta)
        if meta.get("status") != "running":
            return meta
        job_id = meta["id"]
        outcome = self._sidecar(job_id, "outcome")
        exit_file = self.jobs_dir / f"{job_id}.exit"
        try:
            if outcome:
                result = self._read(outcome, MAX_JOB_BYTES)
                meta.update(exit_code=result.get("exit_code"), ended=result.get("ended"),
                            cleanup=result.get("cleanup"), timed_out=result.get("timed_out", False))
                if (result.get("cleanup") or {}).get("complete") is False:
                    meta["status"] = "cleanup_incomplete"
                elif result.get("cancelled"):
                    meta["status"] = "killed"
                else:
                    meta["status"] = "done" if meta["exit_code"] == 0 else "failed"
                meta["status_derived"] = True
                return self._refresh_cleanup(meta) if meta["status"] == "cleanup_incomplete" else meta
            if exit_file.is_file() and not exit_file.is_symlink():
                try:
                    meta["exit_code"] = int(exit_file.read_text().strip() or "-1")
                except ValueError:
                    meta["exit_code"] = -1
                meta.update(status="done" if meta["exit_code"] == 0 else "failed",
                            ended=exit_file.stat().st_mtime, status_derived=True)
                return meta
        except (OSError, ValueError, UnsafePath):
            meta["identity_state"] = "unreadable_outcome"
            return meta
        if meta.get("identity"):
            if not identity_matches(meta.get("pid"), meta["identity"]):
                state = ("previous_boot" if meta["identity"].get("boot_id") != BOOT_ID
                         else "not_running_or_changed")
                meta.update(status="lost", identity_state=state, status_derived=True)
            else:
                meta["identity_state"] = "verified"
        else:
            meta["identity_state"] = "unverified_legacy"
        return meta

    def _refresh_cleanup(self, meta: dict) -> dict:
        """Refresh ended lineage in memory; unreadable or missing proof stays uncertain.

        Session activity is checked independently by the catalog and action plans.
        A reused PID is not the saved process and must never be signalled.
        """
        if not meta.get("ended") or not meta.get("identity"):
            return meta
        try:
            process = self._sidecar(meta["id"], "process")
            if not process:
                return meta
            state = self._read(process, MAX_JOB_BYTES)
            identities = [(meta.get("pid"), meta["identity"]),
                          (state.get("pid"), state.get("identity"))]
            identities.extend((state.get("descendants") or {}).items())
            for pid, saved in identities:
                if not isinstance(saved, dict) or not saved.get("start_ticks") or not saved.get("boot_id"):
                    return meta
                if saved["boot_id"] != BOOT_ID:
                    continue
                base = PROC / str(int(pid))
                try:
                    fields = (base / "stat").read_text().rsplit(") ", 1)[1].split()
                except FileNotFoundError:
                    if base.exists():
                        return meta
                    continue
                if fields[0] not in ("Z", "X", "x") and fields[19] == str(saved["start_ticks"]):
                    return meta
            meta.update(status="done" if meta.get("exit_code") == 0 else "failed",
                        status_derived=True, identity_state="ended_lineage_gone")
        except (OSError, ValueError, TypeError, IndexError, AttributeError, UnsafePath):
            pass
        return meta

    def _observed_session(self, meta: dict) -> str | None:
        sid = meta.get("session_id")
        if is_uuid(sid):
            return sid
        path = self._sidecar(meta["id"], "output")
        if not path:
            return None
        try:
            parsed = (self._read(path, MAX_OUTPUT_BYTES).get("parsed") or {})
        except (OSError, ValueError, UnsafePath, AttributeError):
            return None
        sid = parsed.get("session_id") if isinstance(parsed, dict) else None
        return sid if is_uuid(sid) else None

    def jobs(self, kind: str | None = "agent") -> list[dict]:
        """Readable job records. Anything that could not be read is recorded in
        self.problems (never silently skipped): callers that destroy data must
        treat a non-empty list as "state unknown"."""
        self.problems = []
        try:
            info = os.lstat(self.jobs_dir)
        except FileNotFoundError:
            return []
        except OSError as error:
            self.problems.append(f"jobs directory unreadable: {error.strerror}")
            return []
        if not stat.S_ISDIR(info.st_mode):
            self.problems.append("jobs path is not a real directory")
            return []
        out = []
        try:
            names = os.listdir(self.jobs_dir)
        except OSError as error:
            self.problems.append(f"jobs directory unreadable: {error.strerror}")
            return []
        for name in names:
            stem = name[:-5] if name.endswith(".json") else None
            if not stem or not JOB_ID.fullmatch(stem):
                continue  # skips *.output.json etc. (they contain dots) and temp files
            path = self.jobs_dir / name
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue  # removed by retention between listdir and lstat
            except OSError as error:
                self.problems.append(f"{stem}: {error.strerror}")
                continue
            if not stat.S_ISREG(info.st_mode):
                self.problems.append(f"{stem}: not a regular file")
                continue
            key = (info.st_ino, info.st_size, info.st_mtime_ns)
            hit = self._cache.get(stem)
            if hit and hit[0] == key and not hit[1].get("status_derived") and hit[1].get("status") not in UNCERTAIN:
                meta = hit[1]
            else:
                try:
                    raw = self._read(path, MAX_JOB_BYTES)
                except FileNotFoundError:
                    continue
                except (OSError, ValueError, UnsafePath) as error:
                    self.problems.append(f"{stem}: {error}"[:200])
                    continue
                if not isinstance(raw, dict) or raw.get("id") != stem:
                    self.problems.append(f"{stem}: unrecognized job record")
                    continue
                meta = self._derive(raw)
                if meta.get("kind") == "agent":
                    meta["observed_session_id"] = self._observed_session(meta)
                self._cache[stem] = (key, meta)
            if kind and meta.get("kind") != kind:
                continue
            out.append(meta)
        out.sort(key=lambda m: m.get("started") or 0, reverse=True)
        self._last = out
        return out

    def unattributed(self) -> list[dict]:
        """Unfinished agent jobs whose session cannot be determined (from the last jobs() call)."""
        return [m for m in self._last if m.get("kind") == "agent" and uncertain(m)
                and not m.get("observed_session_id")]

    def lineage_view(self, meta: dict) -> dict:
        """Saved runner, target and descendant PIDs of a job, with live identity checks."""
        view = {"runner_pid": meta.get("pid"),
                "runner_identity_verified": identity_matches(meta.get("pid"), meta.get("identity"))}
        process = self._sidecar(meta["id"], "process")
        if process:
            try:
                state = self._read(process, MAX_JOB_BYTES)
                view["target_pid"] = state.get("pid")
                view["target_identity_verified"] = identity_matches(state.get("pid"), state.get("identity"))
                view["observed_descendants"] = [
                    {"pid": int(pid), "identity_verified": identity_matches(int(pid), ident)}
                    for pid, ident in (state.get("descendants") or {}).items()]
            except (OSError, ValueError, UnsafePath, TypeError):
                view["process_record"] = "unreadable"
        return view

    def session_jobs(self) -> dict[tuple, list[dict]]:
        index: dict[tuple, list[dict]] = {}
        for meta in self.jobs("agent"):
            sid = meta.get("observed_session_id")
            if meta.get("agent") in paths.AGENTS and sid:
                index.setdefault((meta["agent"], sid.lower()), []).append(meta)
        return index
