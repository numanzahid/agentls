import json
import os
import stat

import pytest

from agentls import paths, settings
from agentls.fsutil import UnsafePath


def test_default_and_private_atomic_persistence(env):
    assert settings.load().launch_mode == "exec"
    directory = paths.cli_config_dir()
    assert not directory.exists()  # reading defaults does not write
    settings.save(settings.Settings(launch_mode="suspend"))
    target = directory / "settings.json"
    inode = target.stat().st_ino
    assert settings.load().launch_mode == "suspend"
    settings.save(settings.Settings())
    assert target.stat().st_ino != inode
    assert settings.load().launch_mode == "exec"
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert target.stat().st_uid == directory.stat().st_uid == os.geteuid()
    assert list(directory.iterdir()) == [target]
    assert not env.state.exists() and not env.cli_state.exists()


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "public", "directory"])
def test_refuse_unsafe_existing_file(env, kind):
    settings.save(settings.Settings())
    target = paths.cli_config_dir() / "settings.json"
    original = target.read_bytes()
    if kind == "public":
        target.chmod(0o644)
    else:
        target.unlink()
        outside = env.root / "outside"
        outside.write_bytes(original)
        outside.chmod(0o600)
        if kind == "symlink":
            target.symlink_to(outside)
        elif kind == "hardlink":
            os.link(outside, target)
        else:
            target.mkdir()
    for operation in (settings.load, lambda: settings.save(settings.Settings(launch_mode="suspend"))):
        with pytest.raises((OSError, UnsafePath)):
            operation()
    if kind != "directory":
        assert target.read_bytes() == original


@pytest.mark.parametrize("mode", ["invalid", None, [], False])
def test_invalid_preference_refused(env, mode):
    settings.save(settings.Settings())
    target = paths.cli_config_dir() / "settings.json"
    target.write_text(json.dumps({"schema": 1, "launch_mode": mode}))
    with pytest.raises(ValueError):
        settings.load()


def test_config_directory_symlink_and_permissions_refused(env):
    directory = paths.cli_config_dir()
    directory.parent.mkdir()
    directory.symlink_to(env.project, target_is_directory=True)
    with pytest.raises(OSError):
        settings.save(settings.Settings())
    assert list(env.project.iterdir()) == []
    directory.unlink()
    directory.mkdir(mode=0o755)
    with pytest.raises(UnsafePath):
        settings.save(settings.Settings())


def test_atomic_failure_keeps_old_preference(env, monkeypatch):
    settings.save(settings.Settings())

    def fail(*args, **kwargs):
        raise OSError("simulated rename failure")

    monkeypatch.setattr(settings.os, "replace", fail)
    with pytest.raises(OSError, match="rename failure"):
        settings.save(settings.Settings(launch_mode="suspend"))
    assert settings.load().launch_mode == "exec"
    assert [p.name for p in paths.cli_config_dir().iterdir()] == ["settings.json"]


def test_refresh_interval_persists_and_defaults_for_old_files(env):
    settings.save(settings.Settings(refresh_seconds=300))
    assert settings.load() == settings.Settings(refresh_seconds=300)
    target = paths.cli_config_dir() / "settings.json"
    target.write_text(json.dumps({"schema": 1, "launch_mode": "suspend"}))  # written before the setting existed
    assert settings.load() == settings.Settings(launch_mode="suspend", refresh_seconds=60)
    for bad in (7, "60", True, -1):
        target.write_text(json.dumps({"schema": 1, "refresh_seconds": bad}))
        with pytest.raises(ValueError):
            settings.load()
