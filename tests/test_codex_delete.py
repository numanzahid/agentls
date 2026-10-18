"""All destructive native operations here use the fake binary and temp stores."""
import json
import os
import sqlite3
from dataclasses import replace
from pathlib import Path

import fixtures
import pytest
from conftest import CODEX_CHILD, CODEX_ID

from ai_bridge_cli import activity, codex_delete, trash
from ai_bridge_cli.catalog import Catalog
from ai_bridge_cli.fsutil import UnsafePath


def archived(env, *, indexed=False):
    path = fixtures.codex_session(env.codex, CODEX_ID, env.project, archived=True, name='Delete fixture')
    if indexed:
        with sqlite3.connect(env.codex / 'state_5.sqlite') as con:
            con.execute('CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, archived INTEGER)')
            con.execute('INSERT INTO threads VALUES (?, ?, 1)', (CODEX_ID, str(path)))
    env.age()
    return Catalog().refresh(probe_activity=False).get('codex', CODEX_ID)


def deleted(env):
    return [args for args in env.calls() if args[:2] == ['delete', '--force']]


def test_indexed_native_delete_and_exact_environment(env, monkeypatch):
    session = archived(env, indexed=True)
    log = env.root / 'native-env.json'
    monkeypatch.setenv('FAKE_ENV_LOG', str(log))
    plan = codex_delete.prepare(session)
    assert not deleted(env) and Path(session.storage_path).exists()
    codex_delete.execute(plan)
    assert deleted(env) == [['delete', '--force', CODEX_ID]]
    assert json.loads(log.read_text()) == {'home': str(env.codex), 'stdin': ''}
    assert not Path(session.storage_path).exists()
    with sqlite3.connect(env.codex / 'state_5.sqlite') as con:
        assert not con.execute('SELECT * FROM threads').fetchall()
    assert CODEX_ID not in (env.codex / 'session_index.jsonl').read_text()
    assert not env.cli_data.exists()  # irreversible: no misleading restore manifest


def test_archive_before_delete_preserves_normal_recovery(populated):
    session = Catalog().refresh(probe_activity=False).get('codex', CODEX_ID)
    with pytest.raises(UnsafePath, match='already natively archived'):
        codex_delete.prepare(session)
    result = trash.execute(trash.build_plan([session]))
    assert ['archive', CODEX_ID] in populated.calls() and not deleted(populated)
    trash.restore(result['batch_id'])
    assert Path(session.storage_path).exists()
    trash.execute(trash.build_plan([session]))
    archived_session = Catalog().refresh(probe_activity=False).get('codex', CODEX_ID)
    codex_delete.execute(codex_delete.prepare(archived_session))
    assert deleted(populated) == [['delete', '--force', CODEX_ID]]


@pytest.mark.parametrize('flag', ['FAKE_NO_DELETE', 'FAKE_NO_FORCE'])
def test_capability_required(env, monkeypatch, flag):
    session = archived(env)
    monkeypatch.setenv(flag, '1')
    with pytest.raises(UnsafePath, match='does not advertise'):
        codex_delete.prepare(session)
    assert not deleted(env)


@pytest.mark.parametrize('status', ['active', 'uncertain', 'recent'])
def test_cached_status_cannot_be_bypassed(env, status):
    session = replace(archived(env), status=status)
    with pytest.raises(UnsafePath, match='status'):
        codex_delete.prepare(session)
    assert not deleted(env)


@pytest.mark.parametrize('mode', ['open', 'command', 'locked', 'incomplete', 'recent', 'job'])
def test_fresh_active_checks(env, monkeypatch, mode):
    session = archived(env)
    info = os.stat(session.storage_path)
    key = (info.st_dev, info.st_ino)
    act = activity.Activity()
    if mode == 'open':
        act.open_files[key] = 1234
    elif mode == 'command':
        act.cmdline_ids[CODEX_ID] = 1234
    elif mode == 'locked':
        act.locked.add(key)
    elif mode == 'incomplete':
        act.uninspectable.append((1234, 'codex'))
    elif mode == 'recent':
        os.utime(session.storage_path, None)
    elif mode == 'job':
        monkeypatch.setattr(codex_delete.BridgeState, 'session_jobs', lambda self: {
            ('codex', CODEX_ID): [{'id': 'busy', 'status': 'running'}]})
    monkeypatch.setattr(activity, 'snapshot', lambda: act)
    with pytest.raises(UnsafePath):
        codex_delete.prepare(session)
    assert not deleted(env)


@pytest.mark.parametrize('change', ['replace', 'unarchive', 'duplicate', 'home', 'index', 'child', 'binary'])
def test_changes_after_confirmation_block_native_delete(env, monkeypatch, change):
    session = archived(env, indexed=True)
    plan = codex_delete.prepare(session)
    path = Path(session.storage_path)
    if change == 'replace':
        path.rename(path.with_suffix('.old'))
        path.write_text(path.with_suffix('.old').read_text())
        env.age()
    elif change == 'unarchive':
        path.rename(fixtures.codex_session(env.codex, CODEX_ID, env.project))
        env.age()
    elif change == 'duplicate':
        fixtures.codex_session(env.codex, CODEX_ID, env.project)
        env.age()
    elif change == 'home':
        monkeypatch.setenv('CODEX_HOME', str(env.root / 'other'))
    elif change == 'index':
        with sqlite3.connect(env.codex / 'state_5.sqlite') as con:
            con.execute('UPDATE threads SET archived=0')
    elif change == 'child':
        fixtures.codex_session(env.codex, CODEX_CHILD, env.project, parent=CODEX_ID)
        env.age()
    else:
        binary = Path(plan.binary)
        binary.write_text(binary.read_text() + '\n# changed\n')
    with pytest.raises((ValueError, OSError)):
        codex_delete.execute(plan)
    assert not deleted(env)


@pytest.mark.parametrize('unsafe', ['file_symlink', 'dir_symlink', 'root_symlink', 'traversal',
                                  'foreign_owner', 'hardlink', 'metadata', 'index_symlink'])
def test_unsafe_storage_is_refused(env, monkeypatch, unsafe):
    session = archived(env, indexed=True)
    path = Path(session.storage_path)
    if unsafe == 'file_symlink':
        target = env.root / 'rollout.jsonl'
        path.rename(target)
        path.symlink_to(target)
    elif unsafe == 'dir_symlink':
        target = env.root / 'archive'
        path.parent.rename(target)
        path.parent.symlink_to(target)
    elif unsafe == 'root_symlink':
        alias = env.root / 'alias'
        alias.symlink_to(env.codex, target_is_directory=True)
        monkeypatch.setenv('CODEX_HOME', str(alias))
        session = replace(session, storage_root=str(alias), storage_path=str(alias / 'archived_sessions' / path.name))
    elif unsafe == 'traversal':
        session = replace(session, storage_path=str(path.parent / '..' / 'archived_sessions' / path.name))
    elif unsafe == 'foreign_owner':
        monkeypatch.setattr(codex_delete.os, 'geteuid', lambda: os.getuid() + 1)
    elif unsafe == 'hardlink':
        os.link(path, env.root / 'linked')
    elif unsafe == 'metadata':
        path.write_text(path.read_text().replace(CODEX_ID, CODEX_CHILD))
        env.age()
    else:
        db = env.codex / 'state_5.sqlite'
        db.rename(env.root / 'real.sqlite')
        db.symlink_to(env.root / 'real.sqlite')
    with pytest.raises((ValueError, OSError)):
        codex_delete.prepare(session)
    assert not deleted(env)


def test_native_handles_indexed_descendants(env):
    session = archived(env, indexed=True)
    child = fixtures.codex_session(env.codex, CODEX_CHILD, env.project, parent=CODEX_ID)
    with sqlite3.connect(env.codex / 'state_5.sqlite') as con:
        con.execute('INSERT INTO threads VALUES (?, ?, 0)', (CODEX_CHILD, str(child)))
    env.age()
    codex_delete.execute(codex_delete.prepare(session))
    assert not child.exists()
    with sqlite3.connect(env.codex / 'state_5.sqlite') as con:
        assert not con.execute('SELECT * FROM threads').fetchall()
    assert deleted(env) == [['delete', '--force', CODEX_ID]]  # no hand deletion of descendants


def test_active_descendant_blocks(env, monkeypatch):
    session = archived(env)
    child = fixtures.codex_session(env.codex, CODEX_CHILD, env.project, parent=CODEX_ID)
    env.age()
    monkeypatch.setattr(activity, 'snapshot', lambda: activity.Activity(cmdline_ids={CODEX_CHILD: 1234}))
    with pytest.raises(UnsafePath, match=CODEX_CHILD):
        codex_delete.prepare(session)
    assert not deleted(env) and child.exists()


def test_fork_reference_refusal_preserves_everything(env):
    session = archived(env, indexed=True)
    child = fixtures.codex_session(env.codex, CODEX_CHILD, env.project)
    child.write_text(child.read_text().replace('"thread_source": "user"', f'"forked_from_id": "{CODEX_ID}"'))
    env.age()
    plan = codex_delete.prepare(session)
    before = Path(session.storage_path).read_bytes(), child.read_bytes()
    with pytest.raises(UnsafePath, match='unsafe fork references'):
        codex_delete.execute(plan)
    assert before == (Path(session.storage_path).read_bytes(), child.read_bytes())
    with sqlite3.connect(env.codex / 'state_5.sqlite') as con:
        assert con.execute('SELECT count(*) FROM threads').fetchone() == (1,)


@pytest.mark.parametrize('flag, value, match', [('FAKE_DELETE_ERROR', 'thread is busy', 'busy'),
                                              ('FAKE_DELETE_NOOP', '1', 'remains'),
                                              ('FAKE_DELETE_FLOOD', '1', 'limit')])
def test_native_failure_or_ambiguous_result_never_succeeds(env, monkeypatch, flag, value, match):
    session = archived(env)
    plan = codex_delete.prepare(session)
    monkeypatch.setenv(flag, value)
    with pytest.raises(UnsafePath, match=match):
        codex_delete.execute(plan)
    assert Path(session.storage_path).exists()


def test_timeout_is_bounded_and_noninteractive(env, monkeypatch):
    session = archived(env)
    monkeypatch.setenv('FAKE_DELETE_SLEEP', '20')
    import time
    start = time.monotonic()
    with pytest.raises(UnsafePath, match='timed out'):
        codex_delete._run([codex_delete.find_binary('codex'), 'delete', '--force', session.session_id],
                          env.codex, timeout=0.1)
    assert time.monotonic() - start < 3
    assert Path(session.storage_path).exists()


@pytest.mark.parametrize('argv', [['codex'], ['codex', 'resume'],
                                  ['codex', 'app-server', '--listen', 'unix://'],
                                  ['codex', 'exec', f'look at {CODEX_ID}']])
def test_unidentified_startup_process_blocks_without_signalling(env, monkeypatch, argv):
    proc = env.root / 'proc'
    base = proc / '4321'
    base.mkdir(parents=True)
    executable = env.root / 'codex-native'
    executable.write_bytes(b'native fixture')
    (base / 'exe').symlink_to(executable)
    (base / 'environ').write_bytes(f'CODEX_HOME={env.codex}\0'.encode())
    (base / 'cmdline').write_bytes(b'\0'.join(arg.encode() for arg in argv) + b'\0')
    monkeypatch.setattr(activity, 'PROC', proc)
    monkeypatch.setattr(activity, '_ancestors', lambda: set())
    monkeypatch.setattr(activity, '_exited', lambda base: False)
    with pytest.raises(UnsafePath, match='startup state uncertain'):
        codex_delete._startup_guard(str(executable))
    assert (base / 'cmdline').exists()


def test_identified_process_is_checked_by_session_evidence(env, monkeypatch):
    proc = env.root / 'proc'
    base = proc / '4321'
    base.mkdir(parents=True)
    executable = env.root / 'codex-native'
    executable.write_bytes(b'native fixture')
    (base / 'exe').symlink_to(executable)
    (base / 'environ').write_bytes(f'CODEX_HOME={env.codex}\0'.encode())
    (base / 'cmdline').write_bytes(f'codex\0resume\0{CODEX_CHILD}\0'.encode())
    monkeypatch.setattr(activity, 'PROC', proc)
    monkeypatch.setattr(activity, '_ancestors', lambda: set())
    monkeypatch.setattr(activity, '_exited', lambda base: False)
    codex_delete._startup_guard(str(executable))


def test_managed_idle_archive_exemption_is_not_extended(env, monkeypatch):
    from ai_bridge_cli import codex_idle
    session = archived(env)
    info = os.stat(session.storage_path)
    key = info.st_dev, info.st_ino
    act = activity.Activity(open_files={key: 4321}, file_holders={key: {4321}})
    monkeypatch.setattr(activity, 'snapshot', lambda: act)
    monkeypatch.setattr(codex_idle, 'verify', lambda *args: 'verified archive proof')
    with pytest.raises(UnsafePath, match='daemon still holds'):
        codex_delete.prepare(session)
    assert not deleted(env)


def test_archive_index_must_agree_before_delete(env):
    session = archived(env, indexed=True)
    with sqlite3.connect(env.codex / 'state_5.sqlite') as con:
        con.execute('UPDATE threads SET rollout_path=?', (str(env.codex / 'sessions/other.jsonl'),))
    with pytest.raises(UnsafePath, match='disagrees'):
        codex_delete.prepare(session)
    assert not deleted(env)


def test_success_with_stale_native_index_is_unconfirmed(env, monkeypatch):
    session = archived(env, indexed=True)
    plan = codex_delete.prepare(session)
    monkeypatch.setenv('FAKE_DELETE_KEEP_INDEX', '1')
    with pytest.raises(UnsafePath, match='index still contains'):
        codex_delete.execute(plan)
    assert not Path(session.storage_path).exists()
    with sqlite3.connect(env.codex / 'state_5.sqlite') as con:
        assert con.execute('SELECT count(*) FROM threads').fetchone() == (1,)


def test_shared_writable_archive_directory_is_refused(env):
    session = archived(env)
    Path(session.storage_path).parent.chmod(0o777)
    with pytest.raises(UnsafePath, match='permissions'):
        codex_delete.prepare(session)
    assert not deleted(env)


@pytest.mark.parametrize('verified_parent', [True, False])
def test_managed_supervisor_requires_verified_child(env, monkeypatch, verified_parent):
    from ai_bridge_cli import codex_idle
    from ai_bridge_cli.fsutil import identity
    proc = env.root / 'proc'
    supervisor = proc / '4321'
    supervisor.mkdir(parents=True)
    executable = env.root / 'codex-native'
    executable.write_bytes(b'native fixture')
    (supervisor / 'exe').symlink_to(executable)
    (supervisor / 'environ').write_bytes(f'CODEX_HOME={env.codex}\0'.encode())
    (supervisor / 'cmdline').write_bytes(b'codex\0app-server\0daemon\0pid-update-loop\0')
    child = proc / '4322'
    child.mkdir()
    (child / 'stat').write_text('4322 (codex) S ' + ('4321' if verified_parent else '1'))
    record = env.codex / 'app-server-daemon/daemon.pid'
    record.parent.mkdir()
    record.write_text(json.dumps({'pid': 4322}))
    monkeypatch.setattr(activity, 'PROC', proc)
    monkeypatch.setattr(activity, '_ancestors', lambda: set())
    monkeypatch.setattr(activity, '_exited', lambda base: False)
    verified = []

    def verify(pid):
        verified.append(pid)
        return ('start', identity(executable.stat()))
    monkeypatch.setattr(codex_idle, '_process', verify)
    if verified_parent:
        codex_delete._startup_guard(str(executable))
    else:
        with pytest.raises(UnsafePath, match='supervisor identity'):
            codex_delete._startup_guard(str(executable))
    assert verified == [4322]
    assert supervisor.exists() and child.exists()


@pytest.mark.parametrize('failure', [FileNotFoundError, PermissionError])
def test_managed_daemon_startup_with_unavailable_identity_is_refused(env, monkeypatch, failure):
    from ai_bridge_cli import codex_idle
    proc = env.root / 'proc'
    base = proc / '4321'
    base.mkdir(parents=True)
    executable = env.root / 'codex-native'
    executable.write_bytes(b'native fixture')
    (base / 'exe').symlink_to(executable)
    (base / 'environ').write_bytes(f'CODEX_HOME={env.codex}\0'.encode())
    (base / 'cmdline').write_bytes(b'codex\0app-server\0--listen\0unix://\0--managed-daemon\0')
    monkeypatch.setattr(activity, 'PROC', proc)
    monkeypatch.setattr(activity, '_ancestors', lambda: set())
    monkeypatch.setattr(activity, '_exited', lambda base: False)

    def unready(pid):
        raise failure('native daemon identity not ready')
    monkeypatch.setattr(codex_idle, '_process', unready)
    with pytest.raises(UnsafePath, match='startup identity'):
        codex_delete._startup_guard(str(executable))
    assert base.exists()


def test_relative_binary_override_is_pinned_before_changing_subprocess_cwd(env, monkeypatch):
    session = archived(env)
    monkeypatch.chdir(env.root)
    monkeypatch.setenv('AI_BRIDGE_CODEX_BIN', 'bin/codex')
    plan = codex_delete.prepare(session)
    assert Path(plan.binary).is_absolute() and plan.binary == str(env.root / 'bin/codex')
    codex_delete.execute(plan)
    assert deleted(env) == [['delete', '--force', CODEX_ID]]
