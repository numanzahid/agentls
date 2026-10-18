"""Display aliases and archive flags. Native transcripts are never modified.

Stored in this tool's own state file ($AGENTLS_STATE_DIR/overlays.json).
"""
from __future__ import annotations

import json
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
    def __init__(self, state_dir: Path | None = None):
        self.dir = Path(state_dir or paths.cli_state_dir())
        self.path = self.dir / "overlays.json"
        self._data: dict | None = None
        self._key: tuple | None = None
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
        """Records {"agent:id": {...}}."""
        self.error = None
        try:
            return dict(self._load_own())
        except (OSError, ValueError, UnsafePath) as error:
            self.error = f"Overlay file unreadable ({error}); aliases/archive flags hidden."
            return {}

    def resolve(self, agent: str, sid: str, own: dict) -> dict:
        """Effective {"alias", "archived"} for one session."""
        record = own.get(f"{agent}:{sid}") or {}
        return {k: v for k, v in record.items() if k in ("alias", "archived")}

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
                    record[field] = value
                record["updated"] = time.time()
                current[key] = record
            atomic_write_json(self.path, {"schema": 1, "sessions": current})
            self._data = None
        return len(keys)
