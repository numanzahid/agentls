"""Every on-disk location, resolved from the environment at call time.

Native stores honour the variables their own CLIs use (CLAUDE_CONFIG_DIR,
CODEX_HOME). Bridge state is read from AI_BRIDGE_STATE_DIR. This tool's own
writable data never lives inside a native store or the bridge state directory.
"""
from __future__ import annotations

import os
from pathlib import Path

AGENTS = ("claude", "codex", "cursor")


def home() -> Path:
    return Path.home()


def _env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name, "").strip()
    return Path(value).expanduser().absolute() if value else default.absolute()


def claude_dir() -> Path:
    return _env_path("CLAUDE_CONFIG_DIR", home() / ".claude")


def codex_dir() -> Path:
    return _env_path("CODEX_HOME", home() / ".codex")


def cursor_home() -> Path:
    return _env_path("AI_BRIDGE_CURSOR_HOME", home() / ".cursor")


def cursor_home_is_default() -> bool:
    # Cursor CLI itself has no home override; resuming a relocated store would
    # make it open a different (or empty) chat.
    return os.path.realpath(cursor_home()) == os.path.realpath(home() / ".cursor")


def agent_root(agent: str) -> Path:
    return {"claude": claude_dir, "codex": codex_dir, "cursor": cursor_home}[agent]()


def bridge_state_dir() -> Path:
    """Main MCP server state (read-only from this tool's point of view)."""
    return _env_path("AI_BRIDGE_STATE_DIR", home() / ".local/state/ai-bridge")


def bridge_tasks_dir() -> Path:
    return _env_path("AI_BRIDGE_TASKS_DIR", home() / ".ai-bridge-tasks")


def bridge_install_root() -> Path:
    """Main server's versioned release root (current -> releases/<id>)."""
    return _env_path("AI_BRIDGE_CLI_BRIDGE_ROOT", home() / ".local/share/ai-bridge")


def cli_state_dir() -> Path:
    """Aliases/archive flags written by this tool."""
    return _env_path("AI_BRIDGE_CLI_STATE_DIR", home() / ".local/state/ai-bridge-cli")


def cli_config_dir() -> Path:
    """Standalone CLI preferences (never the MCP server's configuration)."""
    return _env_path("XDG_CONFIG_HOME", home() / ".config") / "ai-bridge-cli"


def cli_data_dir() -> Path:
    """Install root and quarantine (trash) for natively deleted sessions."""
    return _env_path("AI_BRIDGE_CLI_DATA_DIR", home() / ".local/share/ai-bridge-cli")


def trash_dir() -> Path:
    return cli_data_dir() / "trash"
