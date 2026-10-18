# Install details

```bash
bash install.sh [--dry-run] [--no-replace-ai-bridge] [--accept-main-record] [--python PY]
bash install.sh --status      # who owns ~/.local/bin/ai-bridge{,-cli}
bash install.sh --rollback    # previous venv back; restores the server's ai-bridge launcher
bash install.sh --uninstall   # remove our launchers and venvs; keeps aliases, trash, backups
```

Run as your normal user. The installer never uses `sudo`, never installs packages globally, and
never touches the ai-bridge MCP service, its releases or its state.

## What an install does

1. **Read-only preflight.** Refuses before building anything if `~/.local/bin` is unsafe or a
   launcher path holds a file this tool does not own.
2. **Build.** Creates `~/.local/share/ai-bridge-cli/venv.staging-*` from a clean copy of the
   source, with the pins in `constraints.txt`, then smoke-tests it (version, TUI import, a
   headless listing). A failure here changes nothing. pip, setuptools and wheel live in a reusable
   `~/.cache/ai-bridge-cli/build-venv` (rebuilt when Python changes; safe to delete), so the app's
   venv has no pip and updates take a few seconds.
3. **Activate, all or nothing.** Under `.install.lock` (a concurrent install or rollback is
   refused) it repeats the preflight, moves the old venv to `venv.previous`, swaps the new one in,
   verifies it, and writes two launchers, `~/.local/bin/ai-bridge-cli` and `~/.local/bin/ai-bridge`,
   that run `-I -m ai_bridge_cli` through `venv/libexec/<name>`, a link to the venv's Python, so
   tmux and `ps` show the process as `ai-bridge`.

Every change is journaled first (`launchers.json`, `venv-transaction.json`). Any failure restores
the previous venv and launchers. If the installer is killed, the next install or rollback
finishes recovery first, and recovery itself can be retried.

Rollback consumes `venv.previous`, so repeating it is a no-op. The displaced environment is
kept as `venv.rolled-back-*` until uninstall.

## The `ai-bridge` launcher

An existing `~/.local/bin/ai-bridge` is replaced only if it is:

* a launcher this tool installed (hash matches its record), or
* the ai-bridge server's launcher, where the server's record
  (`~/.local/share/ai-bridge/.cli-launcher.json`) matches its path, uid and sha256, and the hash
  is a known server version. `--accept-main-record` trusts the record for an unknown version.

The original is backed up under `~/.local/share/ai-bridge-cli/backups/`; the server's record is
never modified. Anything else at that path (foreign, edited, a symlink) is left alone and
reported. `--no-replace-ai-bridge` installs only `ai-bridge-cli`.
