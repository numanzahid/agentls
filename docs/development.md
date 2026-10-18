# Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/pytest                    # synthetic fixtures only; never reads your real stores
.venv/bin/ruff check --isolated --select E4,E7,E9,F src tests
# Optional: real native Codex in a temporary CODEX_HOME/HOME, no model calls.
AI_BRIDGE_TEST_CODEX="$(command -v codex)" .venv/bin/pytest tests/test_codex_idle.py
```

Process tests start real child processes, so run the suite outside restrictive sandboxes.

## Releasing

Every release that is pushed or installed gets a new version. The version lives only in
`src/ai_bridge_cli/__init__.py` (`__version__`); `pyproject.toml` reads it from there.

1. Pick the next version ([SemVer](https://semver.org); below 1.0, bump the minor version for
   features or behavior changes and the patch version for fixes only).
2. Set `__version__` in `src/ai_bridge_cli/__init__.py`.
3. Run the tests and ruff check above.
4. Commit the bump on its own: `Release X.Y.Z`.
5. Tag and push: `git tag -a vX.Y.Z -m "ai-bridge-cli X.Y.Z"`, then `git push origin main vX.Y.Z`.
6. Install from the tagged checkout: `bash install.sh --dry-run`, then `bash install.sh`.
7. Verify: `ai-bridge --version` and `ai-bridge-cli --version` print `X.Y.Z`, and
   `bash install.sh --status` shows both launchers owned by ai-bridge-cli.
