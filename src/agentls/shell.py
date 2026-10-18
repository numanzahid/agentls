"""Opt-in parent-shell cwd handoff; native launch still uses direct exec."""
from __future__ import annotations

import os
import stat
from contextlib import contextmanager
from pathlib import Path


def bash_init() -> str:
    return r'''# Opt in with: eval "$(command agentls shell-init bash)"
_agentls_shell_run() {
    local _al_command=$1 _al_dir _al_cwd _al_status
    shift
    _al_dir=$(command mktemp -d /tmp/agentls-shell.XXXXXXXXXX) || return
    if AGENTLS_CWD_FILE="$_al_dir/cwd" command "$_al_command" "$@"; then
        _al_status=0
    else
        _al_status=$?
    fi
    if IFS= read -r -d '' _al_cwd 2>/dev/null < "$_al_dir/cwd"; then
        if ! builtin cd -- "$_al_cwd"; then
            printf '%s\n' 'agentls: could not change shell to the saved project directory' >&2
        fi
    fi
    command rm -f -- "$_al_dir/cwd"
    command rmdir -- "$_al_dir"
    return "$_al_status"
}
agentls() { _agentls_shell_run agentls "$@"; }
'''


@contextmanager
def cwd_handoff(cwd: str):
    """Publish NUL-terminated data in a private wrapper directory; undo failed exec."""
    target = os.environ.get("AGENTLS_CWD_FILE")
    if not target:
        yield
        return
    path = Path(target)
    if not path.is_absolute() or path.name != "cwd":
        raise ValueError("Invalid shell cwd handoff path")
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    created = False
    try:
        info = os.fstat(directory)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError("Shell cwd handoff directory must be owned by you with mode 0700")
        fd = os.open("cwd", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600, dir_fd=directory)
        created = True
        with os.fdopen(fd, "wb") as stream:
            stream.write(os.fsencode(cwd) + b"\0")
        yield
    finally:
        # Successful exec never unwinds Python. A failed launch must not change the shell cwd.
        if created:
            os.unlink("cwd", dir_fd=directory)
        os.close(directory)
