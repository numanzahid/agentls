"""Per-user CLI preferences, separate from native stores and MCP configuration."""
from __future__ import annotations

import json
import os
import secrets
from contextlib import contextmanager
from dataclasses import dataclass

from . import paths
from .fsutil import UnsafePath, check_private, read_json_nofollow

MODES = ("exec", "suspend")


@dataclass(frozen=True)
class Settings:
    launch_mode: str = "exec"

    def __post_init__(self):
        if self.launch_mode not in MODES:
            raise ValueError("Unknown native launch mode; expected exec or suspend.")


@contextmanager
def _directory(create: bool = False):
    # Pin both the config base and our private leaf; never follow a leaf symlink.
    base = paths.cli_config_dir().parent
    if create:
        base.mkdir(mode=0o700, parents=True, exist_ok=True)
    base_fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        info = os.fstat(base_fd)
        if info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise UnsafePath("CLI config base must be owned by you and not writable by others.")
        name = paths.cli_config_dir().name
        if create:
            try:
                os.mkdir(name, mode=0o700, dir_fd=base_fd)
                os.fsync(base_fd)
            except FileExistsError:
                pass
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                     dir_fd=base_fd)
        try:
            check_private(os.fstat(fd), directory=True)
            yield fd
        finally:
            os.close(fd)
    finally:
        os.close(base_fd)


def _read(fd: int) -> Settings:
    row = read_json_nofollow("settings.json", private=True, dir_fd=fd)
    if not isinstance(row, dict) or row.get("schema") != 1:
        raise ValueError("Unrecognized CLI settings schema.")
    return Settings(launch_mode=row.get("launch_mode", "exec"))


def load() -> Settings:
    try:
        with _directory() as fd:
            return _read(fd)
    except FileNotFoundError:
        return Settings()


def save(settings: Settings) -> None:
    with _directory(create=True) as fd:
        try:
            _read(fd)  # refuse unsafe or unrecognized existing settings
        except FileNotFoundError:
            pass
        temp = f".settings.{secrets.token_hex(12)}.tmp"
        out = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                      0o600, dir_fd=fd)
        try:
            with os.fdopen(out, "w") as stream:
                json.dump({"schema": 1, "launch_mode": settings.launch_mode}, stream)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, "settings.json", src_dir_fd=fd, dst_dir_fd=fd)
            os.fsync(fd)
        finally:
            try:
                os.unlink(temp, dir_fd=fd)
            except FileNotFoundError:
                pass
