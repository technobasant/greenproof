# Changelog

All notable changes are listed here. The project follows [Semantic Versioning](https://semver.org/)
and the format of [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.1.0] - 2026-11-01

First release.

### Added

- The engine (`greenproof check`): repository discovery, a snapshot of the candidate tree
  (staged, unstaged and untracked files) taken with a temporary index so the user's index and
  working tree are untouched, base resolution (`--base`, the pull request's merge base in
  GitHub Actions, the session marker, the default branch), the policy and commands read from
  the base revision, a command runner that kills the process group on timeout, a pass cache,
  and text, JSON (`greenproof/report@1`, with a shipped JSON Schema) and Markdown reports,
  plus GitHub annotations.
- Exit codes: 0 pass, 1 blocking integrity finding, 2 command failed or timed out, 3 smoke
  failed, 4 usage or configuration error, 5 internal error (fails closed).
- `greenproof explain RULE_ID`, and a `[rules]` table in the policy to set a rule to `block`,
  `warn` or `off`.
- Static rules for tests and checks that were weakened: `policy.changed`, `tests.deleted`,
  `tests.removed`, `tests.replaced`, `tests.skipped`, `tests.focused`, `tests.weakened`,
  `tests.expected_changed`, `suppress.added`, `runner.tampered`, `ci.tampered` and
  `static.parse_error`. Python test files are analysed as syntax trees; JavaScript,
  TypeScript, Go and Rust test files get line-level checks.
- `tests.self_mocked` (warn): an added or changed test that patches the callable its
  assertions call (`monkeypatch.setattr`, `mock.patch`, `patch.object`, `jest.mock`,
  `vi.mock`, a mocked `spyOn`) is reported as testing the mock.
- `code.hardcoded` (warn, Python only): non-test code that gains a literal table of eight or
  more entries, or a branch on a literal the diff's tests just added.
- The revert check (`revert.passes_without_change`, warn): runs only the added or changed
  tests against the base source with the candidate's test files overlaid, for pytest, jest,
  vitest and `go test`; other runners are skipped with a note.
- The smoke check (`smoke.failed`, block): starts the artifact in its own process group,
  polls `ready_url`, runs the probes and always terminates the group.
- `check --isolate`: setup, commands and smoke run in a worktree of the candidate tree.
- A check that is skipped says why in the report's notes.
- Agent hooks for Claude Code, Cursor and Codex CLI, and an experimental one for Gemini CLI:
  `check --hook AGENT` speaks each agent's stop-hook protocol, releases the agent instead of
  trapping it (nothing changed, same tree after a block, or eight consecutive blocks), and
  `session-start --hook AGENT` records the session's base and tells the agent what the gate
  checks.
- Finding fingerprints, and `greenproof accept FINGERPRINT... [--reason TEXT]`, `--list` and
  `--revoke`. Local acceptances are advisory. In CI, `--github-accept` honours
  `/greenproof accept` comments from repository owners, members and collaborators only.
- `greenproof init`: detects Python, Node, Go and Rust projects and Makefile or justfile
  targets, proposes `[commands]` from evidence, verifies them, and writes `.greenproof.toml`,
  an `AGENTS.md` section, the agent hook files (`--agents`) and, with `--ci github`, a
  workflow. It never prompts and never commits. It writes `revert` into `[modes.ci]` and
  never writes a `[smoke]` table.
- A composite GitHub Action (`action.yml`) and a pre-commit hook (`.pre-commit-hooks.yaml`).
- A release workflow: a `v*` tag builds the package, checks that the tag matches its version
  and publishes it to PyPI with trusted publishing, so no token is stored.
- Dogfooding: this repository has its own `.greenproof.toml`, AGENTS.md section and Claude
  Code hooks, and a `greenproof` job in CI that runs `greenproof check --mode ci` on pull
  requests.
- A README covering the problem, install, quick start, how a check works, the rules, agent
  support and its caveats, CI, accepting findings, smoke setup and the limits.
- A benchmark (`bench/run.py`, 107 cases on Python, JavaScript and Go fixtures) and a
  real-history replay (`bench/replay.py`). MEASUREMENTS.md reports the benchmark, a replay
  of 698 commits from click, httpx, zustand and cobra with every block triaged, a Claude
  Code end-to-end run, and the known false-block patterns. The raw results are in
  `bench/results/`.
- `tests.expected_changed` also reports a test whose expected value changed when no
  non-test code changed (a test edited to match what the code returns).
- Calibration from the replay triage. Tests pair across renames by leaf name, by a body hash
  without the test's own name, and by a structural hash with identifiers renamed.
  JavaScript counts `expectTypeOf`, `assertType` and testing-library queries that throw
  (`getBy…`, awaited `findBy…`) as checks, once per statement. Go counts calls to same-file
  assertion helpers and no longer treats benchmarks as tests. A test inside a block
  comment is not a test. A drop made only of loose checks, a removed test with no checks in
  a file that gains tests, and suppressions in a newly adopted linter or type-checker table
  are warnings, not blocks.

### Changed

- The test suite runs in parallel with `pytest-xdist` (`pytest -n auto`) in CI and in the
  documented gate.
