import os
import subprocess
import sys

import pytest

from ai_bridge_cli import cli, native
from ai_bridge_cli.shell import bash_init, cwd_handoff


def test_shell_init_is_sourceable(capsys):
    assert cli.main(['shell-init', 'bash']) == 0
    source = capsys.readouterr().out
    assert source == bash_init()
    assert subprocess.run(['bash', '-n'], input=source, text=True).returncode == 0


# Both names share one shell helper; run every mode once and the alias once.
@pytest.mark.parametrize('name,mode', [('ai-bridge', 'exec'), ('ai-bridge', 'fail'), ('ai-bridge', 'cancel'),
                                       ('ai-bridge', 'suspend'), ('ai-bridge-cli', 'exec')])
def test_real_shell_handoff(tmp_path, name, mode):
    project = tmp_path / "project ' $()\nwith newline\n"
    project.mkdir()
    record = tmp_path / 'agent-record'
    agent = tmp_path / 'agent'
    agent.write_text(f'''#!{sys.executable}
import os
from pathlib import Path
Path({str(record)!r}).write_text(str(os.getpid()) + '\\n' + os.getcwd() + '\\n' + str('AI_BRIDGE_CLI_CWD_FILE' in os.environ))
raise SystemExit(7)
''')
    agent.chmod(0o700)
    launcher = tmp_path / name
    launcher.write_text(f'''#!{sys.executable}
import os
from types import SimpleNamespace
from ai_bridge_cli.native import exec_, run, PreflightError
plan = SimpleNamespace(cwd={str(project)!r}, argv=[{str(agent)!r}])
print(os.getpid(), flush=True)
if {mode!r} == 'cancel':
    raise SystemExit(0)
if {mode!r} == 'suspend':
    raise SystemExit(run(plan))
if {mode!r} == 'fail':
    plan.argv = ['/nonexistent/ai-bridge-agent']
try:
    exec_(plan)
except PreflightError:
    raise SystemExit(1)
''')
    launcher.chmod(0o700)
    script = bash_init() + f'''
set -e
if {name}; then result=0; else result=$?; fi
printf 'status=%s\\n' "$result"
printf '%s' "$PWD"
'''
    env = {**os.environ, 'PATH': str(tmp_path) + ':' + os.environ['PATH']}
    proc = subprocess.run(['bash', '--noprofile', '--norc'], input=script, text=True,
                          capture_output=True, cwd=tmp_path, env=env, timeout=10)
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr == ''
    pid, status, cwd = proc.stdout.split('\n', 2)
    assert status == f"status={7 if mode in ('exec', 'suspend') else 1 if mode == 'fail' else 0}"
    assert cwd == str(project if mode == 'exec' else tmp_path)
    if mode in ('exec', 'suspend'):
        agent_pid, agent_cwd = record.read_text().split('\n', 1)
        assert (agent_pid == pid) == (mode == 'exec')
        assert agent_cwd == str(project) + '\nFalse'


def test_handoff_refuses_existing_file_and_shared_directory(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    target = tmp_path / 'cwd'
    monkeypatch.setenv('AI_BRIDGE_CLI_CWD_FILE', str(target))
    target.symlink_to(tmp_path / 'victim')
    with pytest.raises(FileExistsError), cwd_handoff('/project'):
        pass
    assert target.is_symlink() and not (tmp_path / 'victim').exists()
    target.unlink()
    tmp_path.chmod(0o755)
    with pytest.raises(ValueError, match='0700'), cwd_handoff('/project'):
        pass
    assert not target.exists()


def test_handoff_removed_on_failed_launch(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    target = tmp_path / 'cwd'
    monkeypatch.setenv('AI_BRIDGE_CLI_CWD_FILE', str(target))
    with pytest.raises(OSError), cwd_handoff('/project'):
        assert target.read_bytes() == b'/project\0'
        assert target.stat().st_mode & 0o777 == 0o600
        raise OSError('failed exec')
    assert not target.exists()
    assert 'AI_BRIDGE_CLI_CWD_FILE' not in native.native_env()


def test_invalid_handoff_is_clean_native_error(tmp_path, monkeypatch):
    from types import SimpleNamespace
    before = os.getcwd()
    monkeypatch.setenv('AI_BRIDGE_CLI_CWD_FILE', 'relative/cwd')
    plan = SimpleNamespace(cwd=str(tmp_path), argv=['/nonexistent/agent'])
    with pytest.raises(native.PreflightError, match='Invalid shell cwd handoff'):
        native.exec_(plan)
    assert os.getcwd() == before
