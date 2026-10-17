"""Display aliases and archive flags. Native transcripts are never modified.

Writes go to this tool's own state file ($AI_BRIDGE_CLI_STATE_DIR/overlays.json).
Overlays written by older ai-bridge CLIs inside the server's state directory
(management/sessions/<sha256(agent:id)>.json) are honoured READ-ONLY as a
fallback; any value set here takes precedence.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from . import paths
from .fsutil import UnsafePath, atomic_write_json, ensure_private_dir, file_lock, read_bytes_nofollow
from .textsafe import is_uuid

MAX_ALIAS = 200
MAX_FILE = 4 << 20


def validate_alias(alias: str) -> str:
    alias = " ".join(alias.split()) if isinstance(alias, str) else ""
    if not 1 <= len(alias) <= MAX_ALIAS or any(ord(c) < 32 or 0x7f <= ord(c) < 0xa0 for c in alias):
        raise ValueError(f"Alias must be 1..{MAX_ALIAS} printable characters.")
    return alias


class Overlays:
    def __init__(self, state_dir: Path | None = None, bridge_state_dir: Path | None = None):
        self.dir = Path(state_dir or paths.cli_state_dir())
        self.path = self.dir / "overlays.json"
        self.legacy_dir = Path(bridge_state_dir or paths.bridge_state_dir()) / "management" / "sessions"
        self._data: dict | None = None
        self._key: tuple | None = None
        self._legacy_index: set[str] = set()
        self.error: str | None = None

    @staticmethod
    def key(agent: str, sid: str) -> str:
        if agent not in paths.AGENTS or not is_uuid(sid):
            raise ValueError("Use an exact agent and session UUID.")
        return f"{agent}:{sid}"

    def _load_own(self) -> dict:
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            return {}
        key = (info.st_ino, info.st_size, info.st_mtime_ns)
        if self._data is not None and self._key == key:
            return self._data
        value = json.loads(read_bytes_nofollow(self.path, MAX_FILE, private=True))
        if not isinstance(value, dict) or value.get("schema") != 1 or not isinstance(value.get("sessions"), dict):
            raise ValueError(f"Unrecognized overlay file: {self.path}")
        self._data, self._key = value["sessions"], key
        return self._data

    def load(self) -> dict[str, dict]:
        """Own records {"agent:id": {...}}; also indexes legacy server-side files."""
        self.error = None
        self._legacy_index = self._legacy_names()
        try:
            return dict(self._load_own())
        except (OSError, ValueError, UnsafePath) as error:
            self.error = f"Overlay file unreadable ({error}); aliases/archive flags hidden."
            return {}

    def _legacy_names(self) -> set[str]:
        try:
            if self.legacy_dir.is_symlink() or not self.legacy_dir.is_dir():
                return set()
            return {n for n in os.listdir(self.legacy_dir) if n.endswith(".json") and len(n) == 69}
        except OSError:
            return set()

    def _legacy(self, agent: str, sid: str) -> dict:
        name = hashlib.sha256(f"{agent}:{sid}".encode()).hexdigest() + ".json"
        if name not in self._legacy_index:
            return {}
        try:
            value = json.loads(read_bytes_nofollow(self.legacy_dir / name, 65536, private=True))
        except (OSError, ValueError, UnsafePath):
            return {}
        if not isinstance(value, dict):
            return {}
        return {k: value[k] for k, kind in (("alias", str), ("archived", bool))
                if isinstance(value.get(k), kind)}

    def resolve(self, agent: str, sid: str, own: dict) -> dict:
        """Effective {"alias", "archived"}: own values (even None/False) beat legacy ones."""
        record = own.get(f"{agent}:{sid}") or {}
        merged = self._legacy(agent, sid)
        merged.update({k: v for k, v in record.items() if k in ("alias", "archived")})
        return merged

    def update(self, items: list[tuple[str, str]], **changes) -> int:
        """Apply alias/archived changes to many sessions in one locked write."""
        if "alias" in changes and changes["alias"] is not None:
            changes["alias"] = validate_alias(changes["alias"])
        keys = [self.key(a, s) for a, s in items]
        ensure_private_dir(self.dir)
        with file_lock(self.dir / ".overlays.lock"):
            self._data = None
            current = dict(self._load_own())
            for key in keys:
                record = dict(current.get(key) or {})
                for field, value in changes.items():
                    record[field] = value  # None/False are explicit: they override legacy values
                record["updated"] = time.time()
                current[key] = record
            atomic_write_json(self.path, {"schema": 1, "sessions": current})
            self._data = None
        return len(keys)
