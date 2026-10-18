"""Only disposable processes and temporary native stores; never user chats."""
import json
import os
import shutil
import sqlite3
import subprocess
import time
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import CODEX_ID
from rich.console import Console

from ai_bridge_cli import activity, codex_idle, trash
from ai_bridge_cli.catalog import Catalog
from ai_bridge_cli.fsutil import UnsafePath


@pytest.fixture
def managed(populated, monkeypatch):
    cc = shutil.which('cc')
    if not cc:
        pytest.skip('C compiler required for controlled daemon fixture')
    release = populated.codex / 'packages/app-server-daemon/releases/fixture'
    binary = release / 'bin/codex'
    binary.parent.mkdir(parents=True)
    package = release / 'codex-package.json'
    package.write_text(json.dumps({'layoutVersion': 1, 'version': '0.163.0', 'variant': 'codex', 'entrypoint': 'bin/codex'}))
    source = populated.root / 'daemon.c'
    source.write_text('''#include <fcntl.h>
#include <stdlib.h>
#include <unistd.h>
int main(void) { if (open(getenv("ROLLOUT"), O_WRONLY|O_APPEND)<0) return 2;
write(1,"ready\\n",6); for (;;) pause(); }
''')
    subprocess.run([cc, str(source), '-o', str(binary)], check=True, capture_output=True)
    session = Catalog().refresh(probe_activity=False).get('codex', CODEX_ID)
    child = subprocess.Popen([str(binary), 'app-server', '--listen', 'unix://', '--managed-daemon'],
                             env={**os.environ, 'ROLLOUT': session.storage_path}, stdout=subprocess.PIPE)
    assert child.stdout.readline() == b'ready\n'
    registry = populated.codex / 'app-server-daemon/daemon.pid'
    registry.parent.mkdir()
    record = {'pid': child.pid, 'processIdentity': {'startTicks': int(activity.proc_start_ticks(child.pid)),
                  'bootId': Path('/proc/sys/kernel/random/boot_id').read_text().strip()}}
    registry.write_text(json.dumps(record))
    target = Path(session.storage_path).stat()
    key = target.st_dev, target.st_ino
    act = activity.Activity(open_files={key: child.pid}, file_holders={key: {child.pid}})
    thread = {'id': CODEX_ID, 'sessionId': CODEX_ID, 'status': {'type': 'idle'}, 'path': session.storage_path,
                  'updatedAt': time.time() - 3600}

    class Reader:
        def __init__(self, endpoint, pid):
            assert pid == child.pid

        def call(self, method, params):
            assert method == 'thread/read' and params == {'threadId': CODEX_ID, 'includeTurns': False}
            return {'thread': thread}

        def close(self):
            pass

    monkeypatch.setattr(codex_idle, 'NativeReader', Reader)
    monkeypatch.setattr(codex_idle, 'find_binary', lambda agent: str(binary))
    monkeypatch.setattr(activity, 'snapshot', lambda: act)
    yield session, child, act, thread, registry, package
    child.terminate()  # Only the disposable fixture process.
    child.wait(timeout=5)


def test_managed_handle_allows_archive_without_stopping(managed, populated):
    session, child, _, *_ = managed
    catalog = Catalog().refresh()
    assert catalog.get('codex', CODEX_ID).status == 'idle'
    plan = trash.build_plan([session])
    assert plan.allowed and not plan.allow_uninspectable
    assert plan.items[0].activity_proof
    assert 'daemon will stay running' in plan.items[0].warnings[0]
    result = trash.execute(plan)
    assert result['moved'] == [session.selector] and not result['failed']
    assert ['archive', CODEX_ID] in populated.calls() and child.poll() is None


@pytest.mark.parametrize('state', ['active', 'systemError', 'unknown', None])
def test_native_non_idle_refused(managed, state):
    session, child, _, thread, *_ = managed
    thread['status'] = {'type': state}
    plan = trash.build_plan([session])
    assert not plan.allowed and child.poll() is None
    assert 'could not be verified' in ' '.join(plan.items[0].blockers)


def test_native_not_loaded_is_inactive(managed):
    session, _, _, thread, *_ = managed
    thread['status'] = {'type': 'notLoaded'}
    assert trash.build_plan([session]).allowed


@pytest.mark.parametrize('case', ['rollout-lock', 'writer-lock', 'writer-holder', 'session-id', 'unknown-holder',
                                  'uninspectable', 'recent', 'native-recent', 'wrong-path', 'wrong-id',
                                  'wrong-session', 'wrong-pid', 'wrong-start', 'wrong-boot', 'version', 'argv',
                                  'executable', 'unfinished-job', 'stale-active-status'])
def test_other_activity_and_unsupported_daemons_block(managed, case):
    session, child, act, thread, registry, package = managed
    key = next(iter(act.open_files))
    if case == 'rollout-lock':
        act.locked.add(key)
    elif case in ('writer-lock', 'writer-holder'):
        lock = registry.parents[1] / 'thread-writer-locks' / f'{CODEX_ID}.lock'
        lock.parent.mkdir()
        lock.touch()
        info = lock.stat()
        if case == 'writer-lock':
            act.locked.add((info.st_dev, info.st_ino))
        else:
            act.open_files[(info.st_dev, info.st_ino)] = child.pid
    elif case == 'session-id':
        act.cmdline_ids[CODEX_ID] = child.pid
    elif case == 'unknown-holder':
        act.file_holders[key].add(os.getpid())
    elif case == 'uninspectable':
        act.uninspectable.append((999, 'unknown'))
    elif case == 'recent':
        os.utime(session.storage_path, None)
    elif case in ('native-recent', 'wrong-path', 'wrong-id', 'wrong-session'):
        field, value = {'native-recent': ('updatedAt', time.time()), 'wrong-path': ('path', '/other'),
                        'wrong-id': ('id', 'other'), 'wrong-session': ('sessionId', 'other')}[case]
        thread[field] = value
    elif case.startswith('wrong-'):
        row = json.loads(registry.read_text())
        if case == 'wrong-pid':
            row['pid'] = 1
        else:
            row['processIdentity']['startTicks' if case == 'wrong-start' else 'bootId'] = 0
        registry.write_text(json.dumps(row))
    elif case == 'version':
        row = json.loads(package.read_text())
        row['version'] = '0.164.0'
        package.write_text(json.dumps(row))
    elif case == 'argv':
        # A verified package alone is insufficient without the exact managed argv.
        registry.unlink()
    elif case == 'executable':
        package.parent.chmod(0o777)
    elif case == 'unfinished-job':
        session = replace(session, active_job_ids=['running'])
    else:
        session = replace(session, status='active', status_note='interactive session')
    plan = trash.build_plan([session])
    assert not plan.allowed and child.poll() is None


@pytest.mark.parametrize('change', ['active', 'holder', 'start', 'threads', 'rollout', 'writer-lock'])
def test_recheck_refuses_changed_activity(managed, monkeypatch, change):
    session, child, act, thread, *_ = managed
    plan = trash.build_plan([session])
    assert plan.allowed
    if change == 'active':
        thread['status'] = {'type': 'active', 'activeFlags': []}
    elif change == 'holder':
        act.file_holders[next(iter(act.open_files))].add(os.getpid())
    elif change in ('start', 'threads'):
        original = codex_idle._process
        def changed(pid):
            value = list(original(pid))
            value[0 if change == 'start' else 3] = 'changed'
            return tuple(value)
        monkeypatch.setattr(codex_idle, '_process', changed)
    elif change == 'writer-lock':
        act.locked.add(next(iter(act.open_files)))
    else:
        with open(session.storage_path, 'a') as stream:
            stream.write('{}\n')
    with pytest.raises(ValueError, match='changed'):
        trash.execute(plan)
    assert child.poll() is None


def test_final_recheck_after_journal_blocks_writer(managed, monkeypatch, populated):
    session, child, _, thread, *_ = managed
    plan = trash.build_plan([session])
    original = trash._write_manifest
    def write(batch, manifest):
        original(batch, manifest)
        if any(i.get('codex_state') == 'archiving' for i in manifest['items']):
            thread['status'] = {'type': 'active'}
    monkeypatch.setattr(trash, '_write_manifest', write)
    result = trash.execute(plan)
    assert result['failed'] and not result['moved'] and child.poll() is None
    assert ['archive', CODEX_ID] not in populated.calls()


def test_ui_explains_managed_archive_and_blockers(managed):
    from ai_bridge_cli.tui.screens import plan_table
    session, _, _, thread, *_ = managed
    console = Console(width=100, record=True)
    console.print(plan_table(trash.build_plan([session]).to_json()))
    assert 'daemon will stay running' in ' '.join(console.export_text().split())
    thread['status'] = {'type': 'active'}
    console.print(plan_table(trash.build_plan([session]).to_json()))
    assert 'inactivity could not be verified' in ' '.join(console.export_text().split())


@pytest.fixture
def native_store(tmp_path, monkeypatch):
    """Explicit opt-in native CLI; never inherit the user's Codex home/config."""
    binary = os.environ.get('AI_BRIDGE_TEST_CODEX')
    if not binary:
        pytest.skip('Set AI_BRIDGE_TEST_CODEX to test the installed native Codex in a temporary home')
    binary = str(Path(binary).resolve(strict=True))
    root = tmp_path / 'isolated-native'
    root.mkdir()
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(('CODEX_', 'OPENAI_', 'AI_BRIDGE_'))}
    env.update(CODEX_HOME=str(root), HOME=str(root), XDG_CONFIG_HOME=str(root))
    path = root / 'sessions/2026/01/01' / f'rollout-2026-01-01T00-00-00-{CODEX_ID}.jsonl'
    path.parent.mkdir(parents=True)
    rows = [{'timestamp': '2026-01-01T00:00:00Z', 'type': 'session_meta', 'payload': {
        'id': CODEX_ID, 'timestamp': '2026-01-01T00:00:00Z', 'cwd': str(root), 'originator': 'codex_cli_rs',
        'cli_version': '0.163.0', 'source': 'cli', 'model_provider': 'openai'}},
        {'timestamp': '2026-01-01T00:00:01Z', 'type': 'event_msg', 'payload': {
            'type': 'task_complete', 'last_agent_message': 'synthetic fixture'}}]
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    return binary, root, path, env


def test_native_archive_with_writable_holder_preserves_index(native_store):
    import sys
    binary, root, path, env = native_store
    holder = subprocess.Popen([sys.executable, '-c',
                               'import sys,time; f=open(sys.argv[1],"a"); print("ready",flush=True); time.sleep(60)',
                               str(path)], env=env, stdout=subprocess.PIPE)
    assert holder.stdout.readline() == b'ready\n'
    try:
        original = path.stat()
        for command, archived in [('archive', 1), ('unarchive', 0)]:
            result = subprocess.run([binary, command, CODEX_ID], env=env, capture_output=True,
                                    text=True, timeout=20, check=False, cwd=root)
            assert result.returncode == 0, result.stderr
            with sqlite3.connect(root / 'state_5.sqlite') as db:
                location, flag = db.execute('select rollout_path, archived from threads where id=?',
                                            (CODEX_ID,)).fetchone()
            assert flag == archived
            info = Path(location).stat()
            assert (info.st_dev, info.st_ino) == (original.st_dev, original.st_ino)
            assert ('archived_sessions' in Path(location).parts) == bool(archived)
            assert holder.poll() is None
    finally:
        holder.terminate()  # Disposable fixture only.
        holder.wait(timeout=5)


def test_native_archive_refuses_writer_lock_even_after_idle_check(native_store):
    import fcntl
    binary, root, path, env = native_store
    lock = root / 'thread-writer-locks' / f'{CODEX_ID}.lock'
    lock.parent.mkdir()
    before = path.read_bytes()
    with lock.open('w') as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = subprocess.run([binary, 'archive', CODEX_ID], env=env, capture_output=True,
                                text=True, timeout=20, check=False, cwd=root)
    assert result.returncode != 0 and path.read_bytes() == before
    assert not list((root / 'archived_sessions').glob('rollout-*'))
    with sqlite3.connect(root / 'state_5.sqlite') as db:
        assert db.execute('select archived from threads where id=?', (CODEX_ID,)).fetchone() == (0,)


def test_native_read_only_rpc_and_peer_identity(native_store):
    binary, root, path, env = native_store
    endpoint = root / 'control.sock'
    daemon = subprocess.Popen([binary, 'app-server', '--listen', f'unix://{endpoint}'], env=env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=root)
    try:
        deadline = time.monotonic() + 5
        while not endpoint.exists() and daemon.poll() is None and time.monotonic() < deadline:
            time.sleep(.02)
        with pytest.raises(UnsafePath, match='verified daemon'):
            codex_idle.NativeReader(endpoint, os.getpid())
        reader = codex_idle.NativeReader(endpoint, daemon.pid)
        try:
            thread = reader.call('thread/read', {'threadId': CODEX_ID, 'includeTurns': False})['thread']
            assert thread['status'] == {'type': 'notLoaded'} and thread['path'] == str(path)
        finally:
            reader.close()
        assert daemon.poll() is None and path.exists()
    finally:
        daemon.terminate()  # Only this isolated test's server, never the user's daemon.
        daemon.wait(timeout=5)


@pytest.mark.parametrize('case', ['unmanaged', 'stdio', 'mcp', 'other-config', 'unknown-flag',
                                  'other-user', 'no-tasks', 'malformed-stat'])
def test_process_launch_and_task_checks(managed, populated, monkeypatch, case):
    session, child, *_ = managed
    real = activity.PROC / str(child.pid)
    fake = populated.root / 'proc'
    base = fake / str(child.pid)
    base.mkdir(parents=True)
    for name in ('status', 'stat', 'cmdline'):
        (base / name).write_bytes((real / name).read_bytes())
    (base / 'exe').symlink_to(os.readlink(real / 'exe'))
    tasks = base / 'task'
    tasks.mkdir()
    task = tasks / str(child.pid)
    task.mkdir()
    (task / 'stat').write_bytes((real / 'stat').read_bytes())
    boot = fake / 'sys/kernel/random/boot_id'
    boot.parent.mkdir(parents=True)
    boot.write_bytes(Path('/proc/sys/kernel/random/boot_id').read_bytes())
    argv = (base / 'cmdline').read_bytes()
    if case == 'unmanaged':
        argv = argv.replace(b'--managed-daemon\0', b'')
    elif case == 'stdio':
        argv = argv.replace(b'unix://', b'stdio://')
    elif case == 'mcp':
        argv = argv.replace(b'app-server', b'mcp-server')
    elif case == 'other-config':
        argv += b'-c\0model=unknown\0'
    elif case == 'unknown-flag':
        argv += b'--unknown\0'
    elif case == 'other-user':
        (base / 'status').write_text('Uid:\t0\t0\t0\t0\n')
    elif case == 'no-tasks':
        (task / 'stat').unlink()
        task.rmdir()
    else:
        (task / 'stat').write_text('malformed')
    (base / 'cmdline').write_bytes(argv)
    monkeypatch.setattr(activity, 'PROC', fake)
    assert not trash.build_plan([session]).allowed
    assert child.poll() is None


@pytest.mark.parametrize('case', ['tasks', 'process-start', 'file', 'socket-error', 'unsupported-rpc', 'malformed-rpc'])
def test_changes_or_unknowns_during_native_query_refuse(managed, monkeypatch, case):
    session, child, _, _, *_ = managed
    original = codex_idle.NativeReader
    class ChangedReader(original):
        def call(self, method, params):
            if case == 'socket-error':
                raise OSError('unreadable')
            if case == 'unsupported-rpc':
                raise UnsafePath('unsupported')
            if case == 'malformed-rpc':
                return {'thread': None}
            if case == 'file':
                with open(session.storage_path, 'a') as stream:
                    stream.write('{}\n')
            else:
                saved = codex_idle._process
                def changed(pid):
                    values = list(saved(pid))
                    values[0 if case == 'process-start' else 3] = 'changed'
                    return tuple(values)
                monkeypatch.setattr(codex_idle, '_process', changed)
            return super().call(method, params)
    monkeypatch.setattr(codex_idle, 'NativeReader', ChangedReader)
    assert not trash.build_plan([session]).allowed and child.poll() is None


@pytest.mark.parametrize('value', [float('nan'), float('inf'), True, 'old', None])
def test_unknown_native_update_times_refused(managed, value):
    session, _, _, thread, *_ = managed
    thread['updatedAt'] = value
    assert not trash.build_plan([session]).allowed


def test_incomplete_holder_set_never_exempted(managed):
    session, _, act, *_ = managed
    act.file_holders.clear()
    assert not trash.build_plan([session]).allowed


def test_allow_uninspectable_does_not_bypass_unknown_holder(managed):
    session, _, act, *_ = managed
    act.file_holders[next(iter(act.open_files))].add(os.getpid())
    assert not trash.build_plan([session], allow_uninspectable=True).allowed


def test_tui_managed_idle_uses_ordinary_archive_confirmation(managed, populated):
    from test_tui import drive, keys, settle

    from ai_bridge_cli.tui import screens

    session, child, *_ = managed
    async def scenario(app, pilot):
        app._q('#table').move_cursor(row=keys(app).index(session.selector))
        await pilot.press('d')
        await settle(pilot, app)
        assert isinstance(app.screen, screens.RemoveScreen)
        console = Console(width=100, record=True)
        console.print(app.screen.note)
        content = ' '.join(console.export_text().split())
        assert 'daemon will stay running' in content and 'SIGTERM' not in content
        await pilot.press('d')
        await settle(pilot, app)
        assert len(app.screen_stack) == 1
        assert child.poll() is None
        assert ['archive', CODEX_ID] in populated.calls()
    drive(scenario, size=(40, 20))


@pytest.mark.parametrize('cached', [False, True])
def test_daemon_binary_must_match_native_archiver(managed, populated, monkeypatch, cached):
    session, child, _, _, _, package = managed
    archiver = populated.root / 'alternate/bin/codex'
    archiver.parent.mkdir(parents=True)
    shutil.copyfile(package.parent / 'bin/codex', archiver)
    shutil.copyfile(package, archiver.parent.parent / 'codex-package.json')
    monkeypatch.setattr(codex_idle, 'find_binary', lambda agent: str(archiver))
    if cached:
        assert trash.build_plan([session]).allowed
    with archiver.open('ab') as stream:
        stream.write(b'changed binary')
    assert not trash.build_plan([session]).allowed and child.poll() is None


def test_binary_change_during_hash_is_refused(tmp_path, monkeypatch):
    from ai_bridge_cli.fsutil import identity
    binary = tmp_path / 'binary'
    binary.write_bytes(b'synthetic binary fixture')
    original = codex_idle.hashlib.file_digest
    def changed(stream, algorithm):
        digest = original(stream, algorithm)
        binary.write_bytes(b'changed while hashing')
        return digest
    monkeypatch.setattr(codex_idle.hashlib, 'file_digest', changed)
    with pytest.raises(UnsafePath, match='changed'):
        codex_idle._binary_digest(str(binary), tuple(identity(binary.stat()).items()))
