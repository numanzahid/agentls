"""Read Cursor CLI's active conversation graph without modifying its databases.

Field numbers were verified against Cursor CLI 2026.10.14. Unknown or missing
structures fail explicitly; scanning every blob would include abandoned turns.
Adapted from the MIT-licensed ai-bridge server (ai_bridge/cursor_store.py).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
from pathlib import Path
from urllib.parse import unquote, urlparse


def fields(data: bytes) -> dict[int, list]:
    data = bytes(data)
    pos = 0
    result: dict[int, list] = {}

    def varint():
        nonlocal pos
        value = shift = 0
        while pos < len(data) and shift < 70:
            byte = data[pos]
            pos += 1
            value |= (byte & 127) << shift
            if byte < 128:
                return value
            shift += 7
        raise ValueError('Invalid Cursor protobuf varint')

    while pos < len(data):
        key = varint()
        number, wire = key >> 3, key & 7
        if not number:
            raise ValueError('Invalid Cursor protobuf field')
        if wire == 0:
            value = varint()
        elif wire in (1, 2, 5):
            size = varint() if wire == 2 else (8 if wire == 1 else 4)
            value = data[pos:pos + size]
            pos += size
            if pos > len(data):
                raise ValueError('Truncated Cursor protobuf data')
        else:
            raise ValueError(f'Unsupported Cursor protobuf wire type: {wire}')
        result.setdefault(number, []).append(value)
    return result


def text_field(obj: dict, number: int) -> str:
    values = obj.get(number, [])
    return values[0].decode('utf-8', errors='replace') if values else ''


def clean_user(text: str) -> str:
    if '<user_query>' in text:
        return text.split('<user_query>', 1)[1].split('</user_query>', 1)[0].strip()
    if text.lstrip().startswith(('<user_info>', '<environment_context>', '<system_reminder>', '<system-reminder>', '<agent_transcripts>', '<rules>', '<skills')):
        return ''
    return text


def connect_readonly(db: Path) -> sqlite3.Connection:
    """Open without write access. With an empty/missing WAL the database is
    opened immutable, so SQLite does not even touch the -shm index; otherwise
    mode=ro is required to see committed WAL frames (shm may be mapped)."""
    db = Path(db)
    # SQLite would block on a FIFO and follow symlinks; only regular files are opened.
    if not stat.S_ISREG(db.lstat().st_mode):
        raise ValueError('Cursor store is not a regular file; refusing to open it.')
    wal = db.with_name(db.name + '-wal')
    try:
        wal_info = wal.lstat()
        if not stat.S_ISREG(wal_info.st_mode):
            raise ValueError('Cursor WAL is not a regular file; refusing to open the store.')
        wal_size = wal_info.st_size
    except FileNotFoundError:
        wal_size = 0
    query = 'mode=ro&immutable=1' if wal_size == 0 else 'mode=ro'
    con = sqlite3.connect(db.absolute().as_uri() + '?' + query, uri=True, timeout=2)
    con.execute('PRAGMA query_only=1')
    return con


class Store:
    def __init__(self, db: Path):
        self.db = db
        self.con = connect_readonly(db)
        # A read transaction gives metadata and blobs a consistent snapshot.
        self.con.execute('BEGIN')
        try:
            row = self.con.execute("select value from meta where key='0'").fetchone()
            if not row:
                raise ValueError('Cursor chat metadata is missing')
            raw = row[0]
            try:
                self.meta = json.loads(raw)
            except (ValueError, TypeError):
                self.meta = json.loads(bytes.fromhex(raw).decode())
            if not isinstance(self.meta, dict):
                raise ValueError('Unsupported Cursor metadata format')
            root = self.meta.get('latestRootBlobId')
            self.root = fields(self.blob(root)) if root else {}
        except Exception:
            self.con.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.con.close()

    def blob(self, ref: bytes | str) -> bytes:
        key = ref.hex() if isinstance(ref, bytes) else ref
        row = self.con.execute('select data from blobs where id=?', (key,)).fetchone()
        if not row:
            raise ValueError(f'Cursor conversation references a missing blob: {key}')
        data = row[0]
        return data.encode() if isinstance(data, str) else data

    def workspace(self) -> str | None:
        candidates = []
        for uri in self.root.get(9, []):
            parsed = urlparse(uri.decode(errors='replace'))
            if parsed.scheme == 'file' and parsed.netloc in ('', 'localhost'):
                candidates.append(unquote(parsed.path))
        if not candidates:
            for ref in self.root.get(1, []):
                try:
                    obj = json.loads(self.blob(ref))
                except (ValueError, UnicodeDecodeError):
                    continue
                if isinstance(obj, dict):
                    content = obj.get('content', '')
                    text = content if isinstance(content, str) else json.dumps(content)
                    candidates.extend(re.findall(r'Workspace Path: ([^\n]+)', text))
        # The directory hash is how Cursor chooses the store on resume.
        for path in candidates:
            path = os.path.abspath(os.path.expanduser(path))
            if hashlib.md5(path.encode()).hexdigest() == self.db.parent.parent.name:
                return path
        # Keep the known project visible even if it moved; callers must not
        # resume through a hash that would select a different/empty store.
        return os.path.abspath(candidates[0]) if candidates else None

    def can_resume(self, workspace: str | None) -> bool:
        return bool(workspace and self.root and
                    hashlib.md5(workspace.encode()).hexdigest() == self.db.parent.parent.name)

    def messages(self) -> list[dict]:
        out = []
        # root_prompt_messages_json is the model cache, not an independent
        # transcript: it duplicates the structured turns and may contain scaffolding.
        if self.root.get(6):
            summary = text_field(fields(self.blob(self.root[6][0])), 1)
            if summary:
                out.append({"role": "assistant", "text": "[Earlier context summary]\n" + summary})
        for ref in self.root.get(8, []):
            turn = fields(self.blob(ref))
            if 1 in turn:
                agent = fields(turn[1][0])
                user = fields(self.blob(agent[1][0]))
                text = text_field(user, 1)
                if not text and 18 in user:
                    text = self.blob(user[18][0]).decode(errors='replace')
                text = clean_user(text)
                if text:
                    out.append({'role': 'user', 'text': text})
                for step_ref in agent.get(2, []):
                    step = fields(self.blob(step_ref))
                    if 1 in step:
                        text = text_field(fields(step[1][0]), 1)
                        if text:
                            out.append({'role': 'assistant', 'text': text})
                    elif 2 in step:
                        tool = fields(step[2][0])
                        names = {1: 'shell', 3: 'delete', 4: 'glob', 5: 'grep', 8: 'read', 9: 'update_todos', 10: 'read_todos', 12: 'edit', 13: 'ls', 14: 'read_lints', 15: 'mcp', 16: 'search', 17: 'create_plan', 18: 'web_search', 19: 'task', 23: 'ask_question', 24: 'fetch', 25: 'switch_mode'}
                        for number, values in tool.items():
                            if number in (54, 57, 59, 60) or not isinstance(values[0], bytes):
                                continue
                            call = fields(values[0])
                            detail = ''
                            if number in (1, 3, 8, 12, 13) and 1 in call:
                                detail = text_field(fields(call[1][0]), 1)
                            out.append({'role': 'tool', 'text': f'[{names.get(number, "cursor_tool_" + str(number))}] {detail}'.strip()})
                    elif 3 not in step:
                        raise ValueError('Unsupported Cursor conversation step')
            elif 2 in turn:
                shell = fields(turn[2][0])
                command = text_field(fields(self.blob(shell[1][0])), 1)
                out.append({'role': 'tool', 'text': '[shell] ' + command})
            else:
                raise ValueError('Unsupported Cursor conversation turn')
        return out
