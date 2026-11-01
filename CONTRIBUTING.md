# Contributing

Thanks for helping. Bugs and ideas go in [issues](https://github.com/technobasant/greenproof/issues);
security problems go to [private reporting](SECURITY.md), never a public issue. Everyone
taking part agrees to the [code of conduct](CODE_OF_CONDUCT.md).

## Setup

```bash
uv sync
uvx pre-commit install        # ruff format and check, YAML/TOML checks, on every commit
```

Before a pull request, run what CI runs:

```bash
uv run ruff format . && uv run ruff check . && uv run mypy src
uv run pytest --cov
```

## What a change needs

* Tests use real git in temporary directories; never mock git.
* Every rule needs a fixture for a true positive and one for a legitimate change that must
  not fire.
* A change to a rule id, a default severity or the JSON report is a change to the public
  interface: note it in `CHANGELOG.md`.
