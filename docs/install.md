# Install details

```bash
bash install.sh [--dry-run] [--python PY]
bash install.sh --status      # who owns ~/.local/bin/agentls
bash install.sh --rollback    # swap the previous virtualenv back in
bash install.sh --uninstall   # remove the launcher and virtualenvs; keeps aliases, trash, settings
```

Run as your normal user. The installer never uses `sudo`, never installs packages globally, and
writes only to its own paths.

## What an install does

1. **Read-only preflight.** Refuses before building anything if `~/.local/bin` is unsafe or
   `~/.local/bin/agentls` holds a file this tool did not install.
2. **Build.** Creates `~/.local/share/agentls/venv.staging-*` from a clean copy of the source,
   with the pins in `constraints.txt`, then smoke-tests it (version, TUI import, a headless
   listing). A failure here changes nothing. pip, setuptools and wheel live in a reusable
   `~/.cache/agentls/build-venv` (rebuilt when Python changes; safe to delete), so the app's venv
   has no pip and updates take a few seconds.
3. **Activate, all or nothing.** Under `.install.lock` (a concurrent install or rollback is
   refused) it repeats the preflight, moves the old venv to `venv.previous`, swaps the new one in,
   verifies it, and writes the launcher `~/.local/bin/agentls`. The launcher runs
   `-I -m agentls` through `venv/libexec/agentls`, a link to the venv's Python, so tmux and `ps`
   show the process as `agentls`.

Every change is journaled first (`launchers.json`, `venv-transaction.json`). Any failure restores
the previous venv and launcher. If the installer is killed, the next install or rollback
finishes recovery first, and recovery itself can be retried.

Rollback consumes `venv.previous`, so repeating it is a no-op. The displaced environment is
kept as `venv.rolled-back-*` until uninstall.
