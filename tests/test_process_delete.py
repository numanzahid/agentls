"""Only synthetic native stores and disposable fixture processes are used."""
import os
import shutil
import subprocess
from dataclasses import replace

import pytest
from conftest import CODEX_ID

from ai_bridge_cli import activity, trash
from ai_bridge_cli.catalog import Catalog
from ai_bridge_cli.fsutil import UnsafePath
from ai_bridge_cli.tui import process_delete as stop


@pytest.fixture
def process(populated, monkeypatch):
    cc = shutil.which('cc')
    if not cc:
        pytest.skip('C compiler required for disposable native process fixture')
    source = populated.root / 'fixture.c'
    source.write_text('''
#include <fcntl.h>
#include <stdlib.h>
#include <signal.h>
#include <unistd.h>
int main(void) {
 if (getenv("IGNORE_TERM")) signal(SIGTERM, SIG_IGN);
 int fd = open(getenv("ROLLOUT"), O_RDONLY);
 if (fd < 0) return 2;
 if (getenv("EXTRA")) open(getenv("EXTRA"), O_RDONLY);
 write(1, "ready\\n", 6);
 for (;;) pause();
}
''')
    binary = populated.root / 'native' / 'codex'
    binary.parent.mkdir()
    subprocess.run([cc, str(source), '-o', str(binary)], check=True, capture_output=True)
    session = Catalog().refresh(probe_activity=False).get('codex', CODEX_ID)
    children = []

    def launch(extra=None, argv=None, ignore=False):
        env = {**os.environ, 'ROLLOUT': session.storage_path}
        if extra:
            env['EXTRA'] = str(extra)
        if ignore:
            env['IGNORE_TERM'] = '1'
        child = subprocess.Popen([str(binary), *(argv or ['resume', CODEX_ID])], env=env,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        children.append(child)
        assert child.stdout.readline() == b'ready\n'
        return child

    monkeypatch.setattr(stop, 'find_binary', lambda agent: str(binary))

    def snapshot():
        act = activity.Activity()
        for child in children:
            if child.poll() is not None:
                continue
            for fd in (activity.PROC / str(child.pid) / 'fd').iterdir():
                info = fd.stat()
                key = info.st_dev, info.st_ino
                act.open_files.setdefault(key, child.pid)
                act.file_holders.setdefault(key, set()).add(child.pid)
        return act

    monkeypatch.setattr(activity, 'snapshot', snapshot)
    yield session, launch
    for child in children:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                child.kill()  # Only our deliberately unresponsive fixture child.
        child.wait()


def test_sigterm_then_native_archive(process, populated):
    session, launch = process
    child = launch()
    plan = stop.prepare(trash.build_plan([replace(session, status='active')]))
    assert plan.process.pid == child.pid
    result = stop.execute(plan)
    assert result['moved'] == [session.selector]
    assert ['archive', CODEX_ID] in populated.calls()
    assert child.wait(timeout=1) == -15


@pytest.mark.parametrize('case', ['extra-file', 'cross-chat', 'mcp', 'other-agent', 'wrong-session',
                                  'recent', 'multi-select', 'shared', 'incomplete', 'bridge-job', 'lock'])
def test_safe_refusals(process, populated, monkeypatch, case):
    session, launch = process
    extra = None
    argv = None
    if case == 'extra-file':
        extra = populated.root / 'other.txt'
        extra.write_text('other')
    if case == 'cross-chat':
        extra = next(path for path in populated.codex.rglob('*.jsonl') if str(path) != session.storage_path)
    if case in ('mcp', 'other-agent', 'wrong-session'):
        argv = {'mcp': ['mcp-server'], 'other-agent': ['exec', 'prompt'],
                'wrong-session': ['resume', '01a00000-0000-7000-8000-000000000002']}[case]
    child = launch(extra=extra, argv=argv)
    sessions = [session]
    if case == 'recent':
        os.utime(session.storage_path, None)
    if case == 'multi-select':
        sessions.append(Catalog().refresh(probe_activity=False).get('claude', '5c1f0e2a-7b3d-4c9e-8a61-2f4d9b0c7e13'))
    if case == 'shared':
        launch()
    if case == 'bridge-job':
        sessions = [replace(session, active_job_ids=['running'])]
    if case in ('incomplete', 'lock'):
        act = activity.snapshot()
        if case == 'incomplete':
            act.problems.append('unreadable')
        else:
            act.locked.add(stop.inspect(child.pid, session).inode)
            # Lock on the rollout remains after process evidence is removed.
        monkeypatch.setattr(activity, 'snapshot', lambda: act)
    with pytest.raises((UnsafePath, OSError)):
        stop.prepare(trash.build_plan(sessions))
    assert child.poll() is None and not populated.cli_data.exists()


@pytest.mark.parametrize('change', ['pid-reuse', 'executable', 'inode', 'argv', 'file', 'new-holder'])
def test_changed_identity_never_signals(process, populated, monkeypatch, change):
    session, launch = process
    child = launch()
    plan = stop.prepare(trash.build_plan([session]))
    original = stop.inspect
    if change == 'file':
        with open(session.storage_path, 'a') as stream:
            stream.write('{}\n')
    elif change == 'new-holder':
        launch()
    else:
        def changed(pid, selected):
            result = original(pid, selected)
            field, value = {'pid-reuse': ('start', 'different'), 'executable': ('executable', (0, 0, 0)),
                            'inode': ('inode', (0, 0)), 'argv': ('argv', (b'codex', b'mcp-server'))}[change]
            return replace(result, **{field: value})
        monkeypatch.setattr(stop, 'inspect', changed)
    signals = []
    monkeypatch.setattr(stop.signal, 'pidfd_send_signal', lambda *args: signals.append(args))
    with pytest.raises(UnsafePath):
        stop.execute(plan)
    assert not signals and child.poll() is None and not populated.cli_data.exists()


def test_unresponsive_process_is_not_killed_or_deleted(process, populated):
    session, launch = process
    child = launch(ignore=True)
    plan = stop.prepare(trash.build_plan([session]))
    with pytest.raises(UnsafePath, match='remains after SIGTERM'):
        stop.execute(plan, timeout=0.02)
    assert child.poll() is None and not populated.cli_data.exists()


def test_changed_files_after_exit_block_archive(process, populated, monkeypatch):
    session, launch = process
    launch()
    plan = stop.prepare(trash.build_plan([session]))
    real_signal = stop.signal.pidfd_send_signal

    def changed(*args):
        real_signal(*args)
        with open(session.storage_path, 'a') as stream:
            stream.write('{}\n')
    monkeypatch.setattr(stop.signal, 'pidfd_send_signal', changed)
    with pytest.raises(UnsafePath, match='Process exited'):
        stop.execute(plan)
    assert not populated.cli_data.exists()


def test_wrong_executable_is_refused(process, monkeypatch):
    session, launch = process
    child = launch()
    monkeypatch.setattr(stop, 'find_binary', lambda agent: '/usr/bin/sleep')
    with pytest.raises(UnsafePath, match='verified native'):
        stop.prepare(trash.build_plan([session]))
    assert child.poll() is None


def test_wrong_user_is_refused(process, monkeypatch):
    session, launch = process
    child = launch()
    monkeypatch.setattr(stop.os, 'geteuid', lambda: 987654321)
    with pytest.raises(UnsafePath, match='owned by you'):
        stop.inspect(child.pid, session)
    assert child.poll() is None


def test_identity_rechecked_after_pidfd_open(process, monkeypatch):
    session, launch = process
    child = launch()
    plan = stop.prepare(trash.build_plan([session]))
    original = stop.os.pidfd_open
    original_inspect = stop.inspect
    signals = []

    def opened(pid):
        fd = original(pid)
        monkeypatch.setattr(stop, 'inspect', lambda pid, selected:
                            replace(original_inspect(pid, selected), start='reused'))
        return fd
    monkeypatch.setattr(stop.os, 'pidfd_open', opened)
    monkeypatch.setattr(stop.signal, 'pidfd_send_signal', lambda *args: signals.append(args))
    with pytest.raises(UnsafePath, match='identity'):
        stop.execute(plan)
    assert not signals and child.poll() is None


@pytest.mark.parametrize('answer', ['y', 'n'])
def test_active_process_tui_flow(process, populated, answer):
    from test_tui import drive, keys, settle

    from ai_bridge_cli.tui import screens

    session, launch = process
    child = launch()

    async def scenario(app, pilot):
        app._q('#table').move_cursor(row=keys(app).index(session.selector))
        await pilot.press('d')
        await settle(pilot, app)
        assert isinstance(app.screen, screens.RemoveScreen)
        await pilot.press('d')
        await settle(pilot, app)
        assert isinstance(app.screen, screens.DeleteConfirmScreen) and app.screen.allowed
        assert str(child.pid) in app.screen.body.plain and session.selector in app.screen.body.plain
        assert child.poll() is None and app.focused.id == 'cancel'
        for button in app.screen.query('Button'):
            assert button.region.width >= len(button.label.plain) + 2
            assert button.region.right <= 40
        await pilot.press(answer)
        await settle(pilot, app)
        assert len(app.screen_stack) == 1
        if answer == 'y':
            assert child.wait(timeout=1) == -15
            assert ['archive', CODEX_ID] in populated.calls()
        else:
            assert child.poll() is None and not populated.cli_data.exists()
    drive(scenario, size=(40, 20))
