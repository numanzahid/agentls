# Opening sessions

`Enter`/`o` (open) and `f` (fork) launch the session in its native CLI after a preflight:

* the stored file is yours, a regular file, not group/world-writable, reached without symlinks;
* the project directory exists and the session is not in use;
* the installed CLI supports the needed flag (`claude --resume`,
  `codex resume [--include-non-interactive]`, `cursor-agent --resume`).

The browser confirms the command and directory first. The file is rechecked just before launch.
The agent runs in the session's project directory with your environment minus `AI_BRIDGE_*`.
Cursor resume is refused while `AI_BRIDGE_CURSOR_HOME` points away from `~/.cursor`, because
Cursor CLI would open a different store.

Preview a launch without a terminal: `ai-bridge sessions open agent:UUID --dry-run`.

## Launch modes

Change with `Ctrl+S` / `F2`:

* **A: close/exec (default).** The browser exits and `exec`s the agent, so no Python parent waits
  and tmux sees the agent process. Exiting the agent does not return to the browser.
* **B: suspend and return.** The browser suspends while the agent runs, then refreshes.

Refusals and cancelled confirmations keep the browser open. If the final recheck fails after
the browser closes, a plain error explains how to reopen it.

The setting is stored in `~/.config/ai-bridge-cli/settings.json` (`$XDG_CONFIG_HOME`), written
atomically as an owner-only file. Unsafe or malformed settings are refused; the default is used.

## Return the shell to the project (Bash)

A launched program cannot change its parent shell's directory, so after mode A you are back
where you ran `ai-bridge`. To land in the project instead, add this to `~/.bashrc`:

```bash
eval "$(command ai-bridge shell-init bash)"
```

It wraps `ai-bridge` and `ai-bridge-cli` in Bash functions that run the command, then `cd` to the
saved project. The agent is still exec'd directly. Cancellations, dry runs, failures and mode B
leave the directory unchanged. It works only when you call the function from the shell you want
to move (not `command ai-bridge`, scripts or subshells). Disable with
`unset -f ai-bridge ai-bridge-cli _ai_bridge_shell_run`.
