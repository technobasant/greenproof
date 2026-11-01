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
