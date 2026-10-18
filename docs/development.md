# Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/pytest                    # synthetic fixtures only; never reads your real stores
.venv/bin/ruff check --isolated --select E4,E7,E9,F src tests
# Optional: real native Codex in a temporary CODEX_HOME/HOME, no model calls.
AGENTLS_TEST_CODEX="$(command -v codex)" .venv/bin/pytest tests/test_codex_idle.py
```

Process tests start real child processes, so run the suite outside restrictive sandboxes.

## Releasing

Every build installed for testing gets a new plain version number (`0.3.0`, then `0.3.1`, …), so
`agentls --version` always tells builds apart. A release is the tested number pushed to `main`;
numbers that were never pushed are simply skipped. The version lives only in
`src/agentls/__init__.py` (`__version__`); `pyproject.toml` reads it from there.

1. Pick the next version ([SemVer](https://semver.org); below 1.0, bump the minor version for
   features or behavior changes and the patch version for fixes only).
2. Set `__version__` in `src/agentls/__init__.py`.
3. Run the tests and ruff check above.
4. Commit the bump on its own (`Release X.Y.Z`) and push `main`. Releases are not tagged; the
   version in `main` is the release.
5. Install from that checkout: `bash install.sh --dry-run`, then `bash install.sh`.
6. Verify: `agentls --version` prints `X.Y.Z`, and `bash install.sh --status` shows the launcher
   owned by agentls.
