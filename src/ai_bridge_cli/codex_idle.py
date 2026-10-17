"""Fail-closed exemption for stale handles held by the managed Codex daemon.

Only read-only native RPCs are used. No session is resumed or unsubscribed and
no process is signalled. This is deliberately Linux/managed-Unix-daemon only.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import socket
import stat
import struct
import time
from functools import lru_cache
from pathlib import Path

from . import activity, paths
from .fsutil import (
    UnsafePath,
    identity,
    lstat_at,
    not_writable_by_others,
    pinned_parent,
    read_json_nofollow,
)
from .native import find_binary

LIMIT = 2 << 20


@lru_cache(maxsize=8)
def _binary_digest(path: str, fingerprint: tuple) -> bytes:
    """Cache only immutable file identities, never a path or version alone."""
    with open(path, 'rb') as stream:
        if tuple(identity(os.fstat(stream.fileno())).items()) != fingerprint:
            raise UnsafePath('Native Codex binary changed during verification.')
        digest = hashlib.file_digest(stream, 'sha256').digest()
        if tuple(identity(os.fstat(stream.fileno())).items()) != fingerprint:
            raise UnsafePath('Native Codex binary changed during verification.')
        return digest


class NativeReader:
    """Bounded JSON-RPC over the daemon's Unix WebSocket, pinned by SO_PEERCRED."""

    def __init__(self, endpoint: Path, pid: int):
        self.sock = socket.socket(socket.AF_UNIX)
        self.deadline = time.monotonic() + 3
        self.sequence = 0
        try:
            self.sock.settimeout(3)
            self.sock.connect(str(endpoint))
            peer = struct.unpack('3i', self.sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if peer[:2] != (pid, os.geteuid()):
                raise UnsafePath('Codex control socket does not belong to the verified daemon.')
            key = base64.b64encode(os.urandom(16)).decode()
            self.sock.sendall((f'GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n'
                               f'Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n'
                               'Sec-WebSocket-Version: 13\r\n\r\n').encode())
            header = b''
            while not header.endswith(b'\r\n\r\n') and len(header) < 8192:
                header += self._read(1)
            expected = base64.b64encode(hashlib.sha1(
                (key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest())
            lines = header.lower().split(b'\r\n')
            if (not lines[0].startswith(b'http/1.1 101 ') or
                    b'sec-websocket-accept: ' + expected.lower() not in lines):
                raise UnsafePath('Unsupported Codex control socket handshake.')
            self.call('initialize', {'clientInfo': {'name': 'ai_bridge_cli_idle_check', 'version': '1'}})
            self._send({'method': 'initialized'})
        except BaseException:
            self.sock.close()
            raise

    def close(self):
        self.sock.close()

    def _read(self, count):
        out = b''
        while len(out) < count:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise UnsafePath('Codex native activity inspection timed out.')
            self.sock.settimeout(remaining)
            chunk = self.sock.recv(count - len(out))
            if not chunk:
                raise UnsafePath('Codex control socket closed during inspection.')
            out += chunk
        return out

    def _send(self, value, opcode=1):
        data = json.dumps(value).encode() if opcode == 1 else value
        mask = os.urandom(4)
        length = len(data)
        prefix = bytes([0x80 | opcode])
        if length < 126:
            prefix += bytes([0x80 | length])
        else:
            prefix += b'\xfe' + struct.pack('!H', length)
        self.sock.sendall(prefix + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def call(self, method, params):
        self.sequence += 1
        self._send({'id': self.sequence, 'method': method, 'params': params})
        for _ in range(100):
            first, second = self._read(2)
            if first & 0x80 == 0 or second & 0x80:
                raise UnsafePath('Unsupported fragmented/masked Codex response.')
            length = second & 127
            if length in (126, 127):
                length = int.from_bytes(self._read(2 if length == 126 else 8), 'big')
            if length > LIMIT:
                raise UnsafePath('Codex activity response exceeds inspection limit.')
            body = self._read(length)
            if first == 0x89:
                self._send(body, 10)
                continue
            if first != 0x81:
                raise UnsafePath('Unsupported Codex activity response frame.')
            row = json.loads(body)
            if row.get('id') == self.sequence:
                if 'error' in row or not isinstance(row.get('result'), dict):
                    raise UnsafePath('Native Codex activity query unsupported or failed.')
                return row['result']
        raise UnsafePath('Too many Codex notifications during activity inspection.')


def _process(pid: int):
    """Verify record, boot/start identity, native release, argv, user and tasks."""
    root = paths.codex_dir().resolve()
    base = activity.PROC / str(pid)
    start = activity.proc_start_ticks(pid)
    if not start or base.stat().st_uid != os.geteuid():
        raise UnsafePath('Managed Codex process identity is unavailable.')
    uids = next(line for line in (base / 'status').read_text().splitlines() if line.startswith('Uid:'))
    if len(uids.split()) != 5 or any(int(x) != os.geteuid() for x in uids.split()[1:]):
        raise UnsafePath('Managed Codex user identities differ.')
    record = read_json_nofollow(root / 'app-server-daemon' / 'daemon.pid')
    recorded = record['processIdentity']
    if (record['pid'] != pid or str(recorded['startTicks']) != start or
            recorded['bootId'] != (activity.PROC / 'sys/kernel/random/boot_id').read_text().strip()):
        raise UnsafePath('Managed Codex daemon record does not match its process.')
    executable = Path(os.readlink(base / 'exe'))
    releases = root / 'packages/app-server-daemon/releases'
    relative = executable.relative_to(releases)
    if len(relative.parts) != 3 or relative.parts[1:] != ('bin', 'codex'):
        raise UnsafePath('Unsupported managed Codex executable layout.')
    for path in (root, root / 'packages', releases, releases.parent,
                 executable.parent.parent, executable.parent, executable):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not not_writable_by_others(path, info):
            raise UnsafePath('Managed Codex release is not trusted.')
    package = read_json_nofollow(executable.parent.parent / 'codex-package.json')
    if (package.get('layoutVersion') != 1 or package.get('variant') != 'codex' or
            package.get('entrypoint') != 'bin/codex' or package.get('version') != '0.163.0'):
        raise UnsafePath('Unsupported managed Codex package.')
    with executable.open('rb') as stream:
        if stream.read(4) != b'\x7fELF':
            raise UnsafePath('Managed Codex executable is not native.')
    if identity(executable.stat()) != identity((base / 'exe').stat()):
        raise UnsafePath('Managed Codex executable changed.')
    argv = (base / 'cmdline').read_bytes().rstrip(b'\0').split(b'\0')
    if Path(os.fsdecode(argv[0])) != executable or argv[1:4] != [b'app-server', b'--listen', b'unix://']:
        raise UnsafePath('Unsupported managed Codex launch mode.')
    options = argv[4:]
    if options.count(b'--managed-daemon') != 1:
        raise UnsafePath('Holder is not a managed shared Codex daemon.')
    index = 0
    while index < len(options):
        option = options[index]
        if option in (b'--managed-daemon', b'--analytics-default-enabled'):
            index += 1
        elif option == b'-c' and index + 1 < len(options):
            value = options[index + 1].decode()
            name, setting = value.split('=', 1)
            if (name not in {'features.api_key_model_discovery', 'features.auth_elicitation',
                             'features.code_mode_host', 'features.mcp_oauth_refresh_coordination'} or
                    setting not in ('true', 'false')):
                raise UnsafePath('Unsupported managed Codex configuration.')
            index += 2
        else:
            raise UnsafePath('Unsupported managed Codex arguments.')
    tasks = []
    for task in (base / 'task').iterdir():
        fields = (task / 'stat').read_text().rsplit(') ', 1)[1].split()
        tasks.append((task.name, fields[19]))
    if not tasks or activity.proc_start_ticks(pid) != start:
        raise UnsafePath('Managed Codex tasks changed during inspection.')
    return start, identity(executable.stat()), tuple(argv), tuple(sorted(tasks))


def verify(pid: int, session_id: str, storage_path: str) -> str:
    """Return reproducible proof for the plan; any ambiguity raises/refuses."""
    before = _process(pid)
    lock = paths.codex_dir() / 'thread-writer-locks' / f'{session_id}.lock'
    try:
        with pinned_parent(paths.codex_dir(), lock) as (fd, name):
            locked = lstat_at(fd, name)
            if not stat.S_ISREG(locked.st_mode) or locked.st_uid != os.geteuid() or locked.st_nlink != 1:
                raise UnsafePath('Unsafe native Codex writer-lock path.')
    except FileNotFoundError:
        pass
    binary = find_binary('codex')
    if not binary:
        raise UnsafePath('Native Codex archiver is unavailable.')
    archiver = Path(binary).resolve()
    package = read_json_nofollow(archiver.parent.parent / 'codex-package.json')
    if package.get('version') != '0.163.0' or package.get('entrypoint') != 'bin/codex':
        raise UnsafePath('Native Codex archive writer-lock behavior is unverified for this version.')
    archiver_identity = identity(archiver.stat())
    executable = os.readlink(activity.PROC / str(pid) / 'exe')
    if _binary_digest(str(archiver), tuple(archiver_identity.items())) != _binary_digest(
            executable, tuple(before[1].items())):
        raise UnsafePath('Managed daemon does not match the installed native Codex binary.')
    path = Path(storage_path)
    info = identity(path.stat(follow_symlinks=False))
    # Confirm the snapshot's exact inode is still held by this process.
    held = False
    for fd in (activity.PROC / str(pid) / 'fd').iterdir():
        try:
            opened = fd.stat()
        except FileNotFoundError:
            continue
        if (opened.st_dev, opened.st_ino) == (info['dev'], info['ino']):
            held = True
    if not held:
        raise UnsafePath('Managed Codex rollout handle changed since inspection.')
    if time.time() - info['mtime_ns'] / 1e9 < activity.RECENT_SECONDS:
        raise UnsafePath('Rollout was recently updated.')
    endpoint = paths.codex_dir() / 'app-server-control/app-server-control.sock'
    reader = NativeReader(endpoint, pid)
    try:
        thread = reader.call('thread/read', {'threadId': session_id, 'includeTurns': False})['thread']
        if (thread['id'] != session_id or thread.get('sessionId') != session_id or
                thread['status'] not in ({'type': 'idle'}, {'type': 'notLoaded'}) or
                Path(thread['path']).resolve() != path.resolve() or
                type(thread['updatedAt']) not in (float, int) or not math.isfinite(thread['updatedAt']) or
                time.time() - thread['updatedAt'] < activity.RECENT_SECONDS):
            raise UnsafePath('Native Codex does not confirm this exact thread is idle.')
    finally:
        reader.close()
    if before != _process(pid) or info != identity(path.stat(follow_symlinks=False)):
        raise UnsafePath('Managed Codex process, tasks or rollout changed during inspection.')
    return hashlib.sha256(repr((pid, before, archiver_identity, info, thread['updatedAt'])).encode()).hexdigest()
