"""Conservative filesystem primitives.

Rules used throughout:
* Native and MCP server data is opened with O_NOFOLLOW and checked with fstat, so a
  swapped-in symlink cannot redirect a read or a move.
* Paths below an agent root are walked one component at a time with
  O_NOFOLLOW; a symlink anywhere below the root means "outside the root".
* This tool only writes inside its own private directories.
"""
from __future__ import annotations

import errno
import fcntl
import json
import os
import secrets
import stat
from contextlib import contextmanager
from pathlib import Path


class UnsafePath(ValueError):
    pass


def uid() -> int:
    return os.geteuid()


def check_private(info: os.stat_result, directory: bool = False) -> None:
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if not kind(info.st_mode):
        raise UnsafePath("unexpected file type")
    if info.st_uid != uid():
        raise UnsafePath("not owned by this user")
    if info.st_mode & 0o077:
        raise UnsafePath("group/other permissions are not private")
    if not directory and info.st_nlink != 1:
        raise UnsafePath("hard-linked file")


def read_bytes_nofollow(path: Path, max_bytes: int, *, private: bool = False,
                        dir_fd: int | None = None) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dir_fd)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if private:
            check_private(info)
        elif not stat.S_ISREG(info.st_mode) or info.st_uid != uid():
            raise UnsafePath("not a regular file owned by this user")
        if info.st_size > max_bytes:
            raise UnsafePath(f"file exceeds {max_bytes} bytes")
        data = stream.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise UnsafePath(f"file exceeds {max_bytes} bytes")
    return data


def read_json_nofollow(path: Path, max_bytes: int = 65536, *, private: bool = False,
                       dir_fd: int | None = None):
    return json.loads(read_bytes_nofollow(path, max_bytes, private=private, dir_fd=dir_fd))


def is_within(path: Path, root: Path) -> bool:
    """Lexical containment after normalising both sides (no symlink resolution)."""
    try:
        Path(os.path.normpath(path)).relative_to(os.path.normpath(root))
        return True
    except ValueError:
        return False


def relative_parts(path: Path, root: Path) -> tuple[str, ...]:
    path, root = Path(os.path.normpath(path)), Path(os.path.normpath(root))
    rel = path.relative_to(root)
    if any(part in ("", ".", "..") for part in rel.parts) or not rel.parts:
        raise UnsafePath("path is not strictly inside its root")
    return rel.parts


def open_root(root: Path) -> int:
    """Open an agent root. The root itself may be a user-configured symlink."""
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    info = os.fstat(fd)
    if info.st_uid != uid():
        os.close(fd)
        raise UnsafePath(f"root is not owned by this user: {root}")
    return fd


@contextmanager
def pinned_parent(root: Path, path: Path):
    """Yield (parent_fd, name) for path, walking below root without symlinks."""
    parts = relative_parts(path, root)
    fd = open_root(root)
    try:
        for part in parts[:-1]:
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                dir_fd=fd)
            except OSError as error:
                if error.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise UnsafePath(f"symlink or non-directory below root: {part}") from error
                raise
            os.close(fd)
            fd = child
            if os.fstat(fd).st_uid != uid():
                raise UnsafePath(f"directory not owned by this user: {part}")
        yield fd, parts[-1]
    finally:
        os.close(fd)


def lstat_at(dir_fd: int, name: str) -> os.stat_result:
    return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)


def ensure_private_dir(path: Path) -> Path:
    """Create (0700) or validate one of this tool's own directories."""
    path = Path(path).absolute()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise UnsafePath(f"expected a real directory: {path}")
    if info.st_uid != uid():
        raise UnsafePath(f"directory not owned by this user: {path}")
    if info.st_mode & 0o077:
        os.chmod(path, 0o700)
    return path


def atomic_write_bytes(path: Path, data: bytes, mode: int = 0o600) -> None:
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, mode)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        fsync_dir(path.parent)  # the rename itself must survive a crash
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def atomic_write_json(path: Path, value, mode: int = 0o600) -> None:
    atomic_write_bytes(path, (json.dumps(value, indent=1, ensure_ascii=False) + "\n").encode(), mode)


@contextmanager
def file_lock(path: Path):
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != uid():
            raise UnsafePath(f"unsafe lock file: {path}")
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def identity(info: os.stat_result) -> dict:
    """Fields that must be unchanged between a preview and a mutation."""
    return {"dev": info.st_dev, "ino": info.st_ino, "mode": stat.S_IFMT(info.st_mode),
            "size": info.st_size if stat.S_ISREG(info.st_mode) else None,
            "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns, "uid": info.st_uid}


def private_primary_group(owner: int, gid: int) -> bool:
    """True only for an enumerated primary group with no other members."""
    import grp
    import pwd
    try:
        account = pwd.getpwuid(owner)
        group = grp.getgrgid(gid)
        accounts, groups = pwd.getpwall(), grp.getgrall()
        if account.pw_uid != owner or account.pw_gid != gid or group.gr_gid != gid:
            return False
        if not any(r.pw_name == account.pw_name and r.pw_uid == owner and r.pw_gid == gid for r in accounts):
            return False
        matching = [r for r in groups if r.gr_gid == gid]
        if not matching or group not in matching:
            return False
        return (all(r.pw_name == account.pw_name and r.pw_uid == owner for r in accounts if r.pw_gid == gid) and
                all(name == account.pw_name for r in [group, *matching] for name in r.gr_mem))
    except (KeyError, OSError, RuntimeError):
        return False


def not_writable_by_others(path: Path, info: os.stat_result | None = None) -> bool:
    """Owner is this user (or root) and nobody else can write it."""
    info = info or os.lstat(path)
    if info.st_uid not in (uid(), 0) or info.st_mode & 0o002:
        return False
    if info.st_mode & 0o020:
        if info.st_uid != uid() or not private_primary_group(uid(), info.st_gid):
            return False
        try:
            if {"system.posix_acl_access", "system.posix_acl_default"} & set(
                    os.listxattr(path, follow_symlinks=False)):
                return False
        except OSError:
            return False
    return True


_RENAME_NOREPLACE = 1


class NoReplaceUnsupported(OSError):
    pass


def _renameat2():
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    return getattr(libc, "renameat2", None), ctypes


def rename_noreplace(src_fd: int, src: str, dst_fd: int, dst: str) -> None:
    """renameat2(RENAME_NOREPLACE): atomic, never replaces an existing target.

    Fails closed: if the kernel, libc or filesystem cannot do an atomic
    no-replace rename, nothing is moved (no check-then-rename fallback).
    """
    fn, ctypes = _renameat2()
    if fn is None:
        raise NoReplaceUnsupported(errno.ENOSYS, "atomic no-replace rename (renameat2) is unavailable", dst)
    if fn(src_fd, os.fsencode(src), dst_fd, os.fsencode(dst), _RENAME_NOREPLACE) == 0:
        return
    code = ctypes.get_errno()
    if code == errno.EEXIST:
        raise FileExistsError(code, os.strerror(code), dst)
    if code in (errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP):
        raise NoReplaceUnsupported(code, "atomic no-replace rename is unsupported here", dst)
    raise OSError(code, os.strerror(code), dst)


def fsync_dir(fd_or_path) -> None:
    if isinstance(fd_or_path, int):
        os.fsync(fd_or_path)
        return
    fd = os.open(fd_or_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def open_regular(path, *, dir_fd: int | None = None) -> tuple[int, os.stat_result]:
    """Open for reading without following a final symlink or blocking on a FIFO;
    refuse anything that is not a regular file."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dir_fd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise UnsafePath(f"not a regular file: {path}")
        os.set_blocking(fd, True)
        return fd, info
    except BaseException:
        os.close(fd)
        raise


def read_regular(path, max_bytes: int, *, dir_fd: int | None = None) -> bytes:
    fd, info = open_regular(path, dir_fd=dir_fd)
    with os.fdopen(fd, "rb") as stream:
        if info.st_size > max_bytes:
            raise UnsafePath(f"file exceeds {max_bytes} bytes: {path}")
        data = stream.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise UnsafePath(f"file exceeds {max_bytes} bytes: {path}")
    return data


class TreeTooLarge(UnsafePath):
    pass


def tree_fingerprint(dir_fd: int, max_entries: int = 20000, max_depth: int = 32) -> dict:
    """Every object below an open directory, without following symlinks.

    Returns {"entries": [(relpath, mode, dev, ino, size, mtime_ns, ctime_ns), ...],
    "newest_mtime": float, "foreign_owner": bool, "special": [relpaths]}. In-place
    file changes (size/mtime/ctime) anywhere in the tree change the result.
    """
    entries: list[tuple] = []
    newest = os.fstat(dir_fd).st_mtime
    foreign, special = False, []

    def walk(fd: int, prefix: str, depth: int) -> None:
        nonlocal newest, foreign
        if depth > max_depth:
            raise TreeTooLarge(f"directory deeper than {max_depth} levels")
        with os.scandir(fd) as it:
            names = sorted(e.name for e in it)
        for name in names:
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            rel = f"{prefix}{name}"
            entries.append((rel, info.st_mode, info.st_dev, info.st_ino,
                            info.st_size if stat.S_ISREG(info.st_mode) else 0, info.st_mtime_ns, info.st_ctime_ns))
            if len(entries) > max_entries:
                raise TreeTooLarge(f"more than {max_entries} files to verify")
            newest = max(newest, info.st_mtime)
            foreign |= info.st_uid != uid()
            if stat.S_ISDIR(info.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
                try:
                    walk(child, rel + "/", depth + 1)
                finally:
                    os.close(child)
            elif not (stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)):
                special.append(rel)

    walk(dir_fd, "", 1)
    return {"entries": entries, "newest_mtime": newest, "foreign_owner": foreign, "special": special}
