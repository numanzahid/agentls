# Opening sessions

`Enter`/`o` (open) and `f` (fork) launch the session in its native CLI after a preflight:

* the stored file is yours, a regular file, not group/world-writable, reached without symlinks;
* the project directory exists and the session is not in use;
* the installed CLI supports the needed flag (`claude --resume`,
  `codex resume [--include-non-interactive]`, `cursor-agent --resume`).

The browser confirms the command and directory first. The file is rechecked just before launch.
The agent runs in the session's project directory with your environment minus `AGENTLS_*` and `AI_BRIDGE_*`.
Cursor resume is refused while `AGENTLS_CURSOR_HOME` points away from `~/.cursor`, because
Cursor CLI would open a different store.

Preview a launch without a terminal: `agentls sessions open agent:UUID --dry-run`.

## Launch modes

Settings (`Ctrl+S` / `F2`) has a switch, **Return to the browser after the native CLI exits**:

* **Off (default): close/exec.** The browser exits and `exec`s the agent, so no Python parent waits
  and tmux sees the agent process. Exiting the agent does not return to the browser.
* **On: suspend and return.** The browser suspends while the agent runs, then refreshes.

The same menu sets the **auto refresh** interval: off, 20 s, 60 s (default), 2 or 5 minutes.
Enter toggles or picks the highlighted item and saves it at once; the text under the list explains it.

Refusals and cancelled confirmations keep the browser open. If the final recheck fails after
the browser closes, a plain error explains how to reopen it.

The setting is stored in `~/.config/agentls/settings.json` (`$XDG_CONFIG_HOME`), written
atomically as an owner-only file. Unsafe or malformed settings are refused; the default is used.

## Return the shell to the project (Bash)

A launched program cannot change its parent shell's directory, so after close/exec you are back
where you ran `agentls`. To land in the project instead, add this to `~/.bashrc`:

```bash
eval "$(command agentls shell-init bash)"
```

It wraps `agentls` in a Bash function that runs the command, then `cd`s to the
saved project. The agent is still exec'd directly. Cancellations, dry runs, failures and suspend-and-return
leave the directory unchanged. It works only when you call the function from the shell you want
to move (not `command agentls`, scripts or subshells). Disable with
`unset -f agentls _agentls_shell_run`.
