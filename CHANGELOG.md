# Changelog

All notable changes are listed here. The project follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- The engine: repo discovery, candidate snapshot, base resolution, policy loading from the
  base, command runner, cache, text and JSON reports, and the `check` and `explain` commands.
- `tests.self_mocked` (warn): an added or changed test that patches the callable its assertions
  call (`monkeypatch.setattr`, `mock.patch`, `patch.object`, `jest.mock`, `vi.mock`, a mocked
  `spyOn`) is reported as testing the mock.
- `code.hardcoded` (warn, Python only): non-test code that gains a literal table of eight or
  more entries or a branch on a literal the diff's tests just added.
- The revert check (`revert.passes_without_change`): runs only the added or changed tests
  against the base source with the candidate's test files overlaid, for pytest, jest, vitest
  and `go test`; other runners are skipped with a note.
- The smoke runner (`smoke.failed`): starts the artifact in its own process group, polls
  `ready_url`, runs the probes and always terminates the group.
- `check --isolate`: setup, commands and smoke run in a worktree of the candidate tree.
- A check that is skipped now says why in the report's notes. `init` writes `revert` into
  `[modes.ci]`.
