# Working in this repository

The design is in [SPEC.md](SPEC.md). Implement what it says; if it does not settle a
question, stop and report the question instead of deciding silently.

## Verify before you say you are done

```bash
uv sync
uv run ruff format --check .
uv run ruff check .
uv run mypy src
uv run pytest -n auto --cov --cov-fail-under=85
```

All five must pass. Do not delete, skip or weaken a test to get green, and do not loosen
ruff, mypy, pytest or coverage settings. If a requirement itself looks wrong, say which
one and why.

## Rules

- Python >= 3.11, zero runtime dependencies (stdlib only).
- Tests use real git in temporary directories; never mock git.
- Commit on the milestone branch in small, imperative commits. No `Co-Authored-By` or other
  AI-attribution trailers. Do not push, create remote repositories, or publish packages.

<!-- greenproof:begin -->
## Verify

greenproof runs these commands (from `.greenproof.toml`):

- `uv run ruff check .`
- `uv run mypy src`
- `uv run pytest -q -n auto`

Before you say a task is done, run `uv run greenproof check`.

Fix the code, not the check: do not delete, skip or weaken tests, and do not edit test, runner or CI configuration or `.greenproof.toml` to make a failure go away.

Never run `greenproof accept`; accepting a finding is the user's call.
<!-- greenproof:end -->
