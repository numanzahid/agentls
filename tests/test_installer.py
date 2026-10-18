import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ai_bridge_cli import installer

MAIN_LAUNCHER = b"#!/bin/sh\n# pretend ai-bridge server launcher\nexec python3 -I \"$0\" \"$@\"\n"


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


def main_launcher(home, data=MAIN_LAUNCHER, record=True, known=True, monkeypatch=None):
    target = home / ".local/bin/ai-bridge"
    target.write_bytes(data)
    os.chmod(target, 0o755)
    digest = hashlib.sha256(data).hexdigest()
    if record:
        rec = home / ".local/share/ai-bridge/.cli-launcher.json"
        rec.parent.mkdir(parents=True, exist_ok=True)
        rec.write_text(json.dumps({"schema": 1, "uid": os.geteuid(), "path": str(target), "sha256": digest}))
        os.chmod(rec, 0o600)
    if known and monkeypatch:
        monkeypatch.setattr(installer, "KNOWN_MAIN_LAUNCHERS", {digest})
    return target


def test_fresh_install_and_idempotent_update(home, python):
    uid = os.geteuid()
    report = installer.install(layout(home), python, uid)
    assert sorted(report["installed"]) == sorted(str(home / ".local/bin" / n) for n in installer.NAMES)
    for name in installer.NAMES:
        text = (home / ".local/bin" / name).read_text()
        assert installer.MARKER in text and f"AI_BRIDGE_CLI_PROG={name}" in text and str(python) in text
        assert os.access(home / ".local/bin" / name, os.X_OK)
    record = json.loads((home / ".local/share/ai-bridge-cli/launchers.json").read_text())
    assert len(record["launchers"]) == 2
    assert oct((home / ".local/share/ai-bridge-cli/launchers.json").stat().st_mode & 0o777) == "0o600"
    again = installer.install(layout(home), python, uid)
    assert len(again["unchanged"]) == 2 and not again["installed"]
    other = home / "py"
    other.symlink_to(python)
    updated = installer.install(layout(home), other, uid)
    assert len(updated["updated"]) == 2


def test_launcher_runs_the_package(home, tmp_path):
    venv_python = Path(sys.executable)
    installer.install(layout(home), venv_python, os.geteuid())
    env = {**os.environ, "PYTHONPATH": ""}
    proc = subprocess.run([str(home / ".local/bin/ai-bridge-cli"), "--version"], capture_output=True, text=True,
                          env=env)
    if proc.returncode != 0 and "No module named ai_bridge_cli" in proc.stderr:
        pytest.skip("package not installed into this interpreter (editable dev install needed)")
    assert proc.returncode == 0 and proc.stdout.startswith("ai-bridge-cli ")


def test_launcher_prefers_named_interpreter_link(home, tmp_path):
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin/python").symlink_to(sys.executable)
    named = venv / "libexec/ai-bridge"
    named.parent.mkdir()
    named.write_text('#!/bin/sh\necho "named $*"\n')
    named.chmod(0o755)
    installer.install(layout(home), venv / "bin/python", os.geteuid())
    proc = subprocess.run([str(home / ".local/bin/ai-bridge"), "--version"], capture_output=True, text=True)
    assert proc.stdout == "named -I -m ai_bridge_cli --version\n"


def test_verified_main_launcher_is_backed_up_replaced_and_rolled_back(home, python, monkeypatch):
    target = main_launcher(home, monkeypatch=monkeypatch)
    report = installer.install(layout(home), python, os.geteuid())
    assert report["replaced_server_launcher"] == [str(target)] and report["backups"]
    assert installer.MARKER.encode() in target.read_bytes()
    backup = Path(report["backups"][0])
    assert (backup / "ai-bridge").read_bytes() == MAIN_LAUNCHER
    main_record = home / ".local/share/ai-bridge/.cli-launcher.json"
    before = main_record.read_bytes()
    result = installer.rollback(layout(home), os.geteuid())
    assert result["restored"] == str(target) and target.read_bytes() == MAIN_LAUNCHER
    assert main_record.read_bytes() == before  # server's record never touched
    assert installer.status(layout(home), os.geteuid())["ai-bridge"]["state"] == "main"


def test_unknown_hash_or_mismatched_record_is_not_replaced(home, python, monkeypatch):
    target = main_launcher(home, known=False)
    report = installer.install(layout(home), python, os.geteuid())
    assert report["skipped"][0]["path"] == str(target) and "unknown hash" in report["skipped"][0]["reason"]
    assert target.read_bytes() == MAIN_LAUNCHER
    report = installer.install(layout(home), python, os.geteuid(), accept_main_record=True)
    assert report["replaced_server_launcher"] == [str(target)]


def test_foreign_or_tampered_files_are_left_alone(home, python, monkeypatch):
    target = main_launcher(home, monkeypatch=monkeypatch)
    target.write_bytes(MAIN_LAUNCHER + b"# edited\n")  # record no longer matches
    report = installer.install(layout(home), python, os.geteuid())
    assert any(s["path"] == str(target) for s in report["skipped"])
    assert target.read_bytes().endswith(b"# edited\n")
    target.unlink()
    target.write_bytes(b"#!/bin/sh\necho someone else\n")
    (home / ".local/share/ai-bridge/.cli-launcher.json").unlink()
    report = installer.install(layout(home), python, os.geteuid())
    assert any(s["path"] == str(target) for s in report["skipped"])
    real = home / "elsewhere"
    real.write_text("x")
    target.unlink()
    target.symlink_to(real)
    report = installer.install(layout(home), python, os.geteuid())
    assert any("symlink" in s["reason"] for s in report["skipped"])


def test_no_replace_flag_keeps_server_launcher(home, python, monkeypatch):
    target = main_launcher(home, monkeypatch=monkeypatch)
    report = installer.install(layout(home), python, os.geteuid(), replace_ai_bridge=False)
    assert target.read_bytes() == MAIN_LAUNCHER and report["installed"] == [str(home / ".local/bin/ai-bridge-cli")]


def test_dry_run_changes_nothing(home, python, monkeypatch):
    main_launcher(home, monkeypatch=monkeypatch)
    before = sorted(p.name for p in (home / ".local/bin").iterdir())
    report = installer.install(layout(home), python, os.geteuid(), dry_run=True)
    assert report["replaced_server_launcher"] and report["installed"]
    assert sorted(p.name for p in (home / ".local/bin").iterdir()) == before
    assert not (home / ".local/share/ai-bridge-cli").exists()


def test_uninstall_removes_only_own_launchers(home, python, monkeypatch):
    target = main_launcher(home, monkeypatch=monkeypatch)
    installer.install(layout(home), python, os.geteuid())
    cli = home / ".local/bin/ai-bridge-cli"
    report = installer.uninstall(layout(home), os.geteuid())
    assert str(cli) in report["removed"] and not cli.exists()
    assert target.read_bytes() == MAIN_LAUNCHER  # restored, not removed


def test_rollback_refuses_if_launcher_changed(home, python, monkeypatch):
    target = main_launcher(home, monkeypatch=monkeypatch)
    installer.install(layout(home), python, os.geteuid())
    target.write_bytes(b"#!/bin/sh\necho replaced by someone\n")
    with pytest.raises(installer.Refusal):
        installer.rollback(layout(home), os.geteuid())


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
    assert str(alias / ".local/share/ai-bridge-cli/venv/bin/python") not in calls.read_text().splitlines()
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
