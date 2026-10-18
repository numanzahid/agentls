"""Every on-disk location, resolved from the environment at call time.

Native stores honour the variables their own CLIs use (CLAUDE_CONFIG_DIR,
CODEX_HOME). The ai-bridge MCP server's state is read (never written) from its own
AI_BRIDGE_STATE_DIR. This tool's writable data never lives inside a native store or
the MCP server's directories.
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
    return _env_path("AGENTLS_CURSOR_HOME", home() / ".cursor")


def cursor_home_is_default() -> bool:
    # Cursor CLI itself has no home override; resuming a relocated store would
    # make it open a different (or empty) chat.
    return os.path.realpath(cursor_home()) == os.path.realpath(home() / ".cursor")


def agent_root(agent: str) -> Path:
    return {"claude": claude_dir, "codex": codex_dir, "cursor": cursor_home}[agent]()


def mcp_state_dir() -> Path:
    """ai-bridge MCP server state: job and provenance records (read-only here)."""
    return _env_path("AI_BRIDGE_STATE_DIR", home() / ".local/state/ai-bridge")


def mcp_install_root() -> Path:
    """ai-bridge MCP server install root; its absence proves no session came through MCP."""
    return _env_path("AGENTLS_MCP_ROOT", home() / ".local/share/ai-bridge")


def cli_state_dir() -> Path:
    """Aliases/archive flags written by this tool."""
    return _env_path("AGENTLS_STATE_DIR", home() / ".local/state/agentls")


def cli_config_dir() -> Path:
    """Standalone CLI preferences (never the MCP server's configuration)."""
    return _env_path("XDG_CONFIG_HOME", home() / ".config") / "agentls"


def cli_data_dir() -> Path:
    """Install root and quarantine (trash) for natively deleted sessions."""
    return _env_path("AGENTLS_DATA_DIR", home() / ".local/share/agentls")


def trash_dir() -> Path:
    return cli_data_dir() / "trash"
