#!/usr/bin/env bash
# Install or update agentls for the current user, in its own virtualenv.
#
#   bash install.sh [--dry-run] [--python PY]
#   bash install.sh --rollback     swap back the previous agentls virtualenv (if any)
#   bash install.sh --uninstall    remove the agentls launcher and virtualenvs (trash, settings,
#                                  aliases kept)
#   bash install.sh --status       show what owns ~/.local/bin/agentls
#
# Never uses sudo, never pip-installs globally, and only writes to its own paths.
set -euo pipefail
umask 022

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALLER="$SRC_DIR/src/agentls/installer.py"
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
    --python) [ $# -ge 2 ] || die "--python needs a path"; PY="$2"; shift ;;
    --rollback) MODE=rollback ;;
    --uninstall) MODE=uninstall ;;
    --status) MODE=status ;;
    -h|--help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "Unknown option: $1 (see --help)" ;;
  esac
  shift
done

[ "$(id -u)" -ne 0 ] || die "Run as your normal user, not root (no sudo needed)."
PW_HOME="$(getent passwd "$(id -un)" | cut -d: -f6)"
[ -n "$PW_HOME" ] && [ "$(realpath "$HOME")" = "$(realpath "$PW_HOME")" ] || die "HOME must be your passwd home ($PW_HOME)."
# Match Layout: never stage through a HOME alias with a different lexical path.
CANONICAL_HOME="$(realpath -e "$PW_HOME")" || die "Cannot resolve passwd home."
[ -d "$CANONICAL_HOME" ] || die "Passwd home must be a directory."
DATA_DIR="$CANONICAL_HOME/.local/share/agentls"
VENV="$DATA_DIR/venv"

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
  # Both take the installer lock and verify ownership before touching anything.
  rollback) launchers rollback ${LAUNCHER_ARGS[@]+"${LAUNCHER_ARGS[@]}"}; exit 0 ;;
  uninstall) launchers uninstall ${LAUNCHER_ARGS[@]+"${LAUNCHER_ARGS[@]}"}; exit 0 ;;
esac

[ -f "$SRC_DIR/pyproject.toml" ] && [ -f "$INSTALLER" ] || die "Run install.sh from the agentls source tree."

# Read-only preflight first: refuse before building anything if the launchers
# could not be installed safely.
say "Preflight (read-only)"
launchers preflight --venv-python "$VENV/bin/python" ${LAUNCHER_ARGS[@]+"${LAUNCHER_ARGS[@]}"}
if [ "$DRY_RUN" -eq 1 ]; then
  say "Dry run: would build $VENV with $("$PY" -V 2>&1) and apply the plan above. Nothing was changed."
  exit 0
fi

mkdir -p "$DATA_DIR"
chmod 700 "$DATA_DIR"
STAGE="$(mktemp -d "$DATA_DIR/venv.staging-XXXXXX")"
BUILD="$(mktemp -d)"
cleanup() {
  rm -rf "$BUILD"
  # Recovery may still need this stage, including after the installer was killed.
  if [ -n "${STAGE:-}" ] && [ -d "$STAGE" ] &&
     [ ! -e "$DATA_DIR/venv-transaction.json" ] && [ ! -L "$DATA_DIR/venv-transaction.json" ]; then
    rm -rf "$STAGE"
  fi
}
trap cleanup EXIT

# Reusable build tools (pip, setuptools, wheel), kept in the cache so updates skip
# creating a pip and an isolated build environment each time. Safe to delete.
TOOLS="$CANONICAL_HOME/.cache/agentls/build-venv"
PIP=("$TOOLS/bin/python" -I -m pip --disable-pip-version-check --no-input -q)
tools_ok() {
  [ -x "$TOOLS/bin/python" ] &&
    [ "$("$TOOLS/bin/python" -I -c 'import sys; print(sys.version)')" = "$("$PY" -I -c 'import sys; print(sys.version)')" ] &&
    "$TOOLS/bin/python" -I -c 'import pip, wheel, setuptools; raise SystemExit(int(setuptools.__version__.split(".")[0]) < 77)' 2>/dev/null
}
if ! tools_ok; then
  say "Creating the reusable build environment (first install or new Python only)"
  rm -rf "$TOOLS"
  mkdir -p "$(dirname "$TOOLS")"
  chmod 700 "$(dirname "$TOOLS")"
  "$PY" -m venv "$TOOLS"
  "${PIP[@]}" install "setuptools>=77" wheel
fi

say "Creating virtualenv with $("$PY" -V 2>&1)"
"$PY" -m venv --without-pip "$STAGE"
# Build from a clean copy so the source tree gets no build artifacts.
mkdir "$BUILD/src" "$BUILD/dist"
tar -C "$SRC_DIR" --exclude=.git --exclude=.venv --exclude='*.egg-info' --exclude=__pycache__ \
    --exclude=build --exclude=dist -cf - . | tar -C "$BUILD/src" -xf -
say "Installing agentls and pinned dependencies into the staging virtualenv"
"${PIP[@]}" wheel --no-deps --no-build-isolation -w "$BUILD/dist" "$BUILD/src"
"${PIP[@]}" --python "$STAGE/bin/python" install --no-compile -c "$CONSTRAINTS" "$BUILD"/dist/*.whl

# Named interpreter link, so the running process shows as agentls (tmux, ps).
mkdir "$STAGE/libexec"
ln -s ../bin/python "$STAGE/libexec/agentls"

say "Smoke test (staging)"
"$STAGE/bin/python" -I -m agentls --version
"$STAGE/libexec/agentls" -I -m agentls --version >/dev/null || die "Named interpreter link failed; nothing was changed."
"$STAGE/bin/python" -I -c 'import agentls.tui.app' || die "TUI import failed; nothing was changed."
"$STAGE/bin/python" -I -m agentls sessions list --limit 1 --json >/dev/null \
  || die "Headless listing failed; nothing was changed."

# One locked transaction: preflight again, swap venv (previous kept), verify,
# publish launchers (journaled). Any failure restores the previous state.
say "Activating (venv swap + launchers, all-or-nothing)"
launchers activate --stage "$STAGE" ${LAUNCHER_ARGS[@]+"${LAUNCHER_ARGS[@]}"}

case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) echo "NOTE: add ~/.local/bin to PATH, e.g.: echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.bashrc" ;;
esac
say "Done. Run: agentls"
