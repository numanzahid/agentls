import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agentls import installer

@pytest.fixture
def home(tmp_path):
    (tmp_path / ".local/bin").mkdir(parents=True)
    os.chmod(tmp_path / ".local/bin", 0o755)
    return tmp_path


@pytest.fixture
def python(tmp_path):
    return Path(sys.executable)


def layout(home):
    return installer.Layout(home)


def test_fresh_install_and_idempotent_update(home, python):
    uid = os.geteuid()
    report = installer.install(layout(home), python, uid)
    assert sorted(report["installed"]) == sorted(str(home / ".local/bin" / n) for n in installer.NAMES)
    for name in installer.NAMES:
        text = (home / ".local/bin" / name).read_text()
        assert installer.MARKER in text and str(python) in text
        assert os.access(home / ".local/bin" / name, os.X_OK)
    record = json.loads((home / ".local/share/agentls/launchers.json").read_text())
    assert list(record["launchers"]) == [str(home / ".local/bin/agentls")]
    assert oct((home / ".local/share/agentls/launchers.json").stat().st_mode & 0o777) == "0o600"
    again = installer.install(layout(home), python, uid)
    assert len(again["unchanged"]) == 1 and not again["installed"]
    other = home / "py"
    other.symlink_to(python)
    updated = installer.install(layout(home), other, uid)
    assert len(updated["updated"]) == 1


def test_launcher_runs_the_package(home, tmp_path):
    venv_python = Path(sys.executable)
    installer.install(layout(home), venv_python, os.geteuid())
    env = {**os.environ, "PYTHONPATH": ""}
    proc = subprocess.run([str(home / ".local/bin/agentls"), "--version"], capture_output=True, text=True,
                          env=env)
    if proc.returncode != 0 and "No module named agentls" in proc.stderr:
        pytest.skip("package not installed into this interpreter (editable dev install needed)")
    assert proc.returncode == 0 and proc.stdout.startswith("agentls ")


def test_launcher_prefers_named_interpreter_link(home, tmp_path):
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin/python").symlink_to(sys.executable)
    named = venv / "libexec/agentls"
    named.parent.mkdir()
    named.write_text('#!/bin/sh\necho "named $*"\n')
    named.chmod(0o755)
    installer.install(layout(home), venv / "bin/python", os.geteuid())
    proc = subprocess.run([str(home / ".local/bin/agentls"), "--version"], capture_output=True, text=True)
    assert proc.stdout == "named -I -m agentls --version\n"


def test_foreign_or_tampered_launcher_is_refused(home, python):
    target = home / ".local/bin/agentls"
    target.write_bytes(b"#!/bin/sh\necho someone else\n")
    os.chmod(target, 0o755)
    with pytest.raises(installer.Refusal, match="not ours"):
        installer.install(layout(home), python, os.geteuid())
    assert target.read_bytes() == b"#!/bin/sh\necho someone else\n"
    target.unlink()
    installer.install(layout(home), python, os.geteuid())
    target.write_bytes(target.read_bytes() + b"# edited\n")  # no longer matches our record
    with pytest.raises(installer.Refusal):
        installer.install(layout(home), python, os.geteuid())
    real = home / "elsewhere"
    real.write_text("x")
    target.unlink()
    target.symlink_to(real)
    with pytest.raises(installer.Refusal, match="symlink"):
        installer.install(layout(home), python, os.geteuid())


def test_never_touches_other_commands(home, python):
    other = home / ".local/bin/ai-bridge"
    other.write_bytes(b"#!/bin/sh\necho mcp server\n")
    os.chmod(other, 0o755)
    installer.install(layout(home), python, os.geteuid())
    installer.uninstall(layout(home), os.geteuid())
    assert other.read_bytes() == b"#!/bin/sh\necho mcp server\n"


def test_dry_run_changes_nothing(home, python):
    report = installer.install(layout(home), python, os.geteuid(), dry_run=True)
    assert report["installed"] == [str(home / ".local/bin/agentls")]
    assert not list((home / ".local/bin").iterdir())
    assert not (home / ".local/share/agentls").exists()


def test_uninstall_removes_own_launcher_and_venvs(home, python):
    installer.install(layout(home), python, os.geteuid())
    venv = home / ".local/share/agentls/venv"
    venv.mkdir()
    cli = home / ".local/bin/agentls"
    report = installer.uninstall(layout(home), os.geteuid())
    assert report["removed"] == [str(cli)] and not cli.exists()
    assert report["removed_venvs"] == [str(venv)] and not venv.exists()


def test_unsafe_bin_directory_is_refused(home, python):
    os.chmod(home / ".local/bin", 0o777)
    with pytest.raises(installer.Refusal):
        installer.install(layout(home), python, os.geteuid())


def test_install_script_is_valid_bash():
    script = Path(__file__).parents[1] / "install.sh"
    assert subprocess.run(["bash", "-n", str(script)]).returncode == 0
    text = script.read_text()
    assert "sudo " not in text.replace("no sudo", "") and "systemctl" not in text
    assert "pip install --user" not in text and "--break-system-packages" not in text


@pytest.mark.parametrize("passwd_alias", [False, True])
def test_home_alias_uses_same_canonical_layout_before_staging(tmp_path, passwd_alias):
    real = tmp_path / "real-home"
    real.mkdir()
    alias = tmp_path / "home-alias"
    alias.symlink_to(real, target_is_directory=True)
    commands = tmp_path / "commands"
    commands.mkdir()
    calls = tmp_path / "python-calls"
    passwd_home = alias if passwd_alias else real
    scripts = {
        "id": '#!/bin/sh\ncase "$1" in -u) echo 1234;; -un) echo testuser;; esac\n',
        "getent": f"#!/bin/sh\necho 'testuser:x:1234:1234:test:{passwd_home}:/bin/sh'\n",
        "test-python": '#!/bin/sh\nprintf "%s\\n" "$@" >> "$CALLS"\n',
    }
    for name, text in scripts.items():
        path = commands / name
        path.write_text(text)
        path.chmod(0o755)
    script = Path(__file__).parents[1] / "install.sh"
    proc = subprocess.run(["bash", str(script), "--dry-run", "--python", str(commands / "test-python")],
                          env={**os.environ, "HOME": str(alias), "PATH": f"{commands}:{os.environ['PATH']}",
                               "CALLS": str(calls)}, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    expected = installer.Layout(passwd_home).venv / "bin/python"
    assert str(expected) in calls.read_text().splitlines()
    assert str(alias / ".local/share/agentls/venv/bin/python") not in calls.read_text().splitlines()
    assert not (real / ".local").exists()


@pytest.mark.parametrize("journal", ["absent", "present", "dangling"])
def test_shell_cleanup_preserves_journaled_stage(tmp_path, journal):
    script = (Path(__file__).parents[1] / "install.sh").read_text()
    cleanup = script[script.index("cleanup() {"):script.index("\ntrap cleanup EXIT")]
    data = tmp_path / "data"
    data.mkdir()
    stage = data / "venv.staging-test"
    stage.mkdir()
    (stage / "KEEP").write_text("staged")
    build = tmp_path / "build"
    build.mkdir()
    path = data / "venv-transaction.json"
    if journal == "present":
        path.write_text("{}")
    elif journal == "dangling":
        path.symlink_to(data / "missing")
    proc = subprocess.run(["bash", "-euc", cleanup + "\ncleanup"],
                          env={**os.environ, "DATA_DIR": str(data), "STAGE": str(stage), "BUILD": str(build)},
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert stage.exists() == (journal != "absent")
    assert not build.exists()
