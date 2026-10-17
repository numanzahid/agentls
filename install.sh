#!/usr/bin/env bash
# Install or update ai-bridge-cli for the current user, in its own virtualenv.
#
#   bash install.sh [--dry-run] [--no-replace-ai-bridge] [--accept-main-record] [--python PY]
#   bash install.sh --rollback     restore the ai-bridge server's original `ai-bridge` launcher
#                                  and the previous ai-bridge-cli virtualenv (if any)
#   bash install.sh --uninstall    remove ai-bridge-cli launchers + virtualenvs (restores the
#                                  server launcher if it was replaced; trash/state are kept)
#   bash install.sh --status       show what owns ~/.local/bin/ai-bridge{,-cli}
#
# Never uses sudo, never pip-installs globally, never touches the ai-bridge MCP
# service, its releases (~/.local/share/ai-bridge) or its state directory.
set -euo pipefail
umask 022

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="${HOME}/.local/share/ai-bridge-cli"
VENV="$DATA_DIR/venv"
INSTALLER="$SRC_DIR/src/ai_bridge_cli/installer.py"
CONSTRAINTS="$SRC_DIR/constraints.txt"

die() { echo "ERROR: $*" >&2; exit 1; }
say() { echo "==> $*"; }

MODE=install
DRY_RUN=0
PY=""
LAUNCHER_ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1; LAUNCHER_ARGS+=(--dry-run) ;;
    --no-replace-ai-bridge|--accept-main-record) LAUNCHER_ARGS+=("$1") ;;
    --python) [ $# -ge 2 ] || die "--python needs a path"; PY="$2"; shift ;;
    --rollback) MODE=rollback ;;
    --uninstall) MODE=uninstall ;;
    --status) MODE=status ;;
    -h|--help) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "Unknown option: $1 (see --help)" ;;
  esac
  shift
done

[ "$(id -u)" -ne 0 ] || die "Run as your normal user, not root (no sudo needed)."
PW_HOME="$(getent passwd "$(id -un)" | cut -d: -f6)"
[ -n "$PW_HOME" ] && [ "$(realpath "$HOME")" = "$(realpath "$PW_HOME")" ] || die "HOME must be your passwd home ($PW_HOME)."

find_python() {
  local candidate
  for candidate in ${PY:+"$PY"} python3.13 python3.12 python3; do
    if command -v "$candidate" >/dev/null 2>&1 &&
       "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 12))' 2>/dev/null; then
      command -v "$candidate"; return 0
    fi
  done
  return 1
}
PY="$(find_python)" || die "Python 3.12+ is required (try --python /path/to/python3.12)."
"$PY" -c 'import venv, ensurepip' 2>/dev/null || die "$PY lacks venv/ensurepip (Debian/Ubuntu: apt install python3-venv)."

launchers() { "$PY" -I "$INSTALLER" "$@"; }

case "$MODE" in
  status) launchers status; exit 0 ;;
  rollback)
    launchers rollback ${LAUNCHER_ARGS[@]+"${LAUNCHER_ARGS[@]}"}
    if [ -d "$DATA_DIR/venv.previous" ] && [ "$DRY_RUN" -eq 0 ]; then
      say "Restoring previous ai-bridge-cli virtualenv"
      rm -rf "$DATA_DIR/venv.rollback-tmp"
      [ -d "$VENV" ] && mv "$VENV" "$DATA_DIR/venv.rollback-tmp"
      mv "$DATA_DIR/venv.previous" "$VENV"
      [ -d "$DATA_DIR/venv.rollback-tmp" ] && mv "$DATA_DIR/venv.rollback-tmp" "$DATA_DIR/venv.previous"
    fi
    say "Rollback complete. ~/.local/bin/ai-bridge-cli (if present) still uses $VENV."
    exit 0 ;;
  uninstall)
    launchers uninstall ${LAUNCHER_ARGS[@]+"${LAUNCHER_ARGS[@]}"}
    if [ "$DRY_RUN" -eq 0 ]; then
      # Build artifacts only; aliases (~/.local/state/ai-bridge-cli) and trash are kept.
      rm -rf "$VENV" "$DATA_DIR/venv.previous" "$DATA_DIR"/venv.staging-*
      say "Removed virtualenvs. Kept: $DATA_DIR/trash, $DATA_DIR/backups, ~/.local/state/ai-bridge-cli"
    fi
    exit 0 ;;
esac

[ -f "$SRC_DIR/pyproject.toml" ] && [ -f "$INSTALLER" ] || die "Run install.sh from the ai-bridge-cli source tree."
if [ "$DRY_RUN" -eq 1 ]; then
  say "Dry run: would build $VENV with $("$PY" -V 2>&1); launcher plan:"
  launchers install --venv-python "$VENV/bin/python" ${LAUNCHER_ARGS[@]+"${LAUNCHER_ARGS[@]}"}
  exit 0
fi

mkdir -p "$DATA_DIR"
chmod 700 "$DATA_DIR"
STAGE="$(mktemp -d "$DATA_DIR/venv.staging-XXXXXX")"
BUILD="$(mktemp -d)"
cleanup() { rm -rf "$BUILD"; if [ -n "${STAGE:-}" ]; then rm -rf "$STAGE"; fi; }
trap cleanup EXIT

say "Creating virtualenv with $("$PY" -V 2>&1)"
"$PY" -m venv "$STAGE"
# Build from a clean copy so the source tree gets no build artifacts.
tar -C "$SRC_DIR" --exclude=.git --exclude=.venv --exclude='*.egg-info' --exclude=__pycache__ \
    --exclude=build --exclude=dist -cf - . | tar -C "$BUILD" -xf -
say "Installing ai-bridge-cli and pinned dependencies into the staging virtualenv"
PIP=("$STAGE/bin/python" -m pip --disable-pip-version-check --no-input -q)
"${PIP[@]}" install ${CONSTRAINTS:+-c "$CONSTRAINTS"} "$BUILD"

say "Smoke test"
"$STAGE/bin/python" -I -m ai_bridge_cli --version
"$STAGE/bin/python" -I -c 'import ai_bridge_cli.tui.app' || die "TUI import failed; nothing was changed."
"$STAGE/bin/python" -I -m ai_bridge_cli sessions list --limit 1 --json >/dev/null \
  || die "Headless listing failed; nothing was changed."

say "Activating new virtualenv"
rm -rf "$DATA_DIR/venv.previous"
if [ -d "$VENV" ]; then mv "$VENV" "$DATA_DIR/venv.previous"; fi
mv "$STAGE" "$VENV"
STAGE=""
# The relocated venv is only ever invoked as `python -I -m ai_bridge_cli`.
if ! "$VENV/bin/python" -I -m ai_bridge_cli --version >/dev/null; then
  rm -rf "$VENV"
  [ -d "$DATA_DIR/venv.previous" ] && mv "$DATA_DIR/venv.previous" "$VENV"
  die "Relocated virtualenv failed; previous one restored."
fi

say "Installing launchers into ~/.local/bin"
launchers install --venv-python "$VENV/bin/python" ${LAUNCHER_ARGS[@]+"${LAUNCHER_ARGS[@]}"}

case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) echo "NOTE: add ~/.local/bin to PATH, e.g.: echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.bashrc" ;;
esac
say "Done. Run: ai-bridge   (or ai-bridge-cli). The ai-bridge MCP service was not touched."
