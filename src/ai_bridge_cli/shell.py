"""Opt-in parent-shell cwd handoff; native launch still uses direct exec."""
from __future__ import annotations

import os
import stat
from contextlib import contextmanager
from pathlib import Path


def bash_init() -> str:
    return r'''# Opt in with: eval "$(command ai-bridge shell-init bash)"
_ai_bridge_shell_run() {
    local _ab_command=$1 _ab_dir _ab_cwd _ab_status
    shift
    _ab_dir=$(command mktemp -d /tmp/ai-bridge-shell.XXXXXXXXXX) || return
    if AI_BRIDGE_CLI_CWD_FILE="$_ab_dir/cwd" command "$_ab_command" "$@"; then
        _ab_status=0
    else
        _ab_status=$?
    fi
    if IFS= read -r -d '' _ab_cwd 2>/dev/null < "$_ab_dir/cwd"; then
        if ! builtin cd -- "$_ab_cwd"; then
            printf '%s\n' 'ai-bridge: could not change shell to the saved project directory' >&2
        fi
    fi
    command rm -f -- "$_ab_dir/cwd"
    command rmdir -- "$_ab_dir"
    return "$_ab_status"
}
ai-bridge() { _ai_bridge_shell_run ai-bridge "$@"; }
ai-bridge-cli() { _ai_bridge_shell_run ai-bridge-cli "$@"; }
'''


@contextmanager
def cwd_handoff(cwd: str):
    """Publish NUL-terminated data in a private wrapper directory; undo failed exec."""
    target = os.environ.get("AI_BRIDGE_CLI_CWD_FILE")
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
