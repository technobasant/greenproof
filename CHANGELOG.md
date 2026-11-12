# Changelog

All notable changes are listed here. The project follows [Semantic Versioning](https://semver.org/)
and the format of [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

Milestone M6 (SPEC section 15): six more agents on the hook protocol. All six are
experimental: the files follow each vendor's documentation as read on 2026-11-12 and none has
been run against its real CLI yet.

### Added

- `check --hook` and `session-start --hook` accept `copilot`, `qwen`, `droid`, `antigravity`,
  `openhands` and `auggie`. Each reads its CLI's own payload (session id, start directory, and
  where there is one the field that marks a stop that is not the model finishing, which is
  skipped) and answers in that CLI's output format; the same state, replay and release rules apply.
- `init --agents` writes the hook files for them: `.github/hooks/greenproof.json`,
  `.qwen/settings.json`, `.factory/hooks.json`, `.agents/hooks.json`, `.openhands/hooks.json`
  and `.augment/settings.json`. Merging, ownership and exit 4 on an unusable file work as for
  the other agents. Timeouts use each CLI's unit: seconds for Qwen (capped at 999, because Qwen
  reads 1000 or more as milliseconds), Droid, Antigravity and OpenHands, `timeoutSec` for
  Copilot, milliseconds for Auggie. Droid's file is keyed directly by event name, and
  Antigravity's greenproof entry is the key `greenproof`.
- Experimental adapters print a warning when named, and Qwen Code and Droid print a note about
  approving or reviewing the hooks. `--agents auto` never installs an experimental adapter. It
  now also detects Copilot (`.github/hooks/` or `.github/copilot-instructions.md`), Qwen
  (`.qwen/`) and Droid (`.factory/`), but only for an adapter that is not experimental, so
  none of them is picked yet.
- `scripts/e2e/capture_payload.py`, a hook command that appends its stdin, arguments and the
  `*_PROJECT_DIR` and `*_SESSION_ID` environment variables to the file named by
  `GREENPROOF_CAPTURE`, for recording real payloads before an adapter is marked supported. It is
  not part of the wheel.
- README: the agents that have no hook that can block a stop (Cline, Roo Code, Windsurf, Zed,
  Warp, Aider, Continue, Kiro, Devin, Jules), which rely on AGENTS.md and CI, and Goose, Junie,
  Amp and Kilo Code as planned.

### Changed

- Under `--hook claude`, a payload with a `timestamp` key is Copilot's (Copilot CLI and VS Code
  also run the hooks in `.claude/settings.json`, and treat exit 2 from a stop hook as a
  warning). When a `.github/hooks/*.json` file holds a greenproof `--hook copilot` entry for the
  event, the Claude hook defers: exit 0, no output, no state written. Otherwise it answers in
  Copilot's format.
- `hooks.py` keeps one table of per-agent protocols instead of per-agent branches; the output
  for Claude Code, Cursor, Codex CLI, Gemini CLI and OpenCode is unchanged. `init` takes the
  notes and warnings it prints from attributes of each adapter module.
- README: the Gemini CLI row points to Antigravity CLI, its successor.

## [0.4.1] - 2026-11-09

### Fixed

- `init` no longer writes hooks that point at an ephemeral environment. Run as
  `uvx greenproof init` (or `pipx run`, or from a project venv that is only active in the
  current shell), it found its own copy of `greenproof` on PATH and wrote the bare command into
  the hook files; the agent's PATH does not have that copy, so the hook failed open. The
  scripts directory of a virtual environment the running interpreter is in is now skipped when
  looking for an installed `greenproof`; a `pipx install` or `uv tool install` symlink in
  `~/.local/bin` still counts. When none is found the hooks use `uvx greenproof@X` as before,
  and the summary says so and how to install it.
- The AGENTS.md section named the command to run as bare `greenproof check` whenever
  `--hook-command` was not given, even when the hooks used `uvx greenproof@X`. It now names the
  command the hooks use.
- The workflow written by `init --ci github` installed nothing for a plain-pip Python project
  (a pyproject, `setup.py` or `setup.cfg` without `requirements.txt`) or for npm without a
  lockfile, so `python -m pytest` ran without pytest or the package, and `npm test` without
  `node_modules`. When the policy has no `setup` command it now adds a `python -m pip install`
  step (the package with its `test`, `tests`, `testing` or `dev` extra, the requirements files
  for development and test, and the pytest, ruff, mypy or pyright the policy runs) and an
  `npm install` step. The step is in the workflow only: `setup` also runs in `--isolate`
  worktrees, where `pip install -e .` would point your own environment at a temporary directory.
  For a project with a `src/` directory the run step also sets `PYTHONPATH: src`, so the revert
  check's base worktree imports its own `src/` and not the editable install of the checkout.

### Changed

- Documentation only, no behaviour beyond the fixes above. README and SPEC describe the new hook
  command rule, the AGENTS.md command and the CI install step, `PYTHONPATH: src` included, and
  that `init` adds an `@AGENTS.md` import to an existing `CLAUDE.md`. They also correct the
  smoke key rule (some keys but not all is a policy error, exit 4), the OpenCode timeouts
  (3600 s for the check, 120 s for session start), the claim that a Python `parametrize` fold
  is blocked when its body ignores the row values (it warns), where `setup` runs (ci mode,
  `--isolate` and revert worktrees), and the `opencode` choices of `--agents` and `--hook`.
  The README quick-start output is re-run on 0.4.1.
- README notes that a uv `exclude-newer` setting keeps `uvx greenproof@X` from resolving for
  new releases, so `uvx` hooks fail open until the release ages past the window.
- SPEC marks each item of "Known limits (0.3.0)" as closed, narrowed or still open in 0.4.0.
  MEASUREMENTS labels the 0.2.0 real-history section and marks the false blocks 0.4.0 no
  longer blocks.

## [0.4.0] - 2026-11-09

Measured against 0.3.1 on the same inputs (MEASUREMENTS.md): of 744 tamper probes, 693 block
and 51 warn, and none passes (0.3.1: 687, 50 and 7); of 502 honest probes, 18 block that should
not (0.3.1: 35). On real history, 66 of 698 test-touching commits block (0.3.1: 75), 12 of them
false blocks (0.3.1: 21).

### Added

- `tests.weakened`: a `for` loop over a literal list or tuple, and a `parametrize` over a
  module-level name bound once to one, are compared by their distinct rows. Fewer distinct rows
  than at the base blocks ("table rows reduced from N to M"), unless every base row is still
  among the candidate's rows or the rows of new tests. A loop whose check no longer reads its
  row blocks, and so does a table name that is bound again or changed in place
  (`CASES.append(4)`). A table that cannot be read (passed to a call, built by `range`) warns,
  "table rows could not be read", and only when the base's table could be read.
- `tests.weakened`: rows count in the check total. A literal loop unrolled into fewer explicit
  asserts blocks, `assert all(<check> for x in <table>)` counts one assert per row, repeated
  rows count once (`[1, 1, 1]` is one case), and an assertion in a nested `def` that nothing
  calls does not count.
- `tests.weakened`: an expected value replaced by a matcher is a loose check on the other side
  of `==` or `assertEqual`. `assert f(1) == 2` becoming `assert f(1) == ANY` blocks ("exact
  check on `f(1)` loosened to `f(1) == ANY`"). Matchers read: `ANY` and `mock.ANY`, dirty-equals,
  callee, testfixtures `Comparison`, a class whose only base is `object` and whose own `__eq__`
  never reads its second parameter, and a function whose every return is such a value (to
  depth 3). Only the top-level operand is judged.
- `tests.weakened`: `pytest.approx` with a tolerance that is a name or cannot be resolved is
  read as loosened. A name bound to a module-level number is resolved first.
- `tests.weakened`: a same-file helper earns credit for its asserts only when it is unique at
  module level, plain (not async, a generator or decorated) and straight (no branch, loop,
  early return, `raise`, `yield`, `await`, skip or exit; `with raises/warns/subTest` is
  allowed). Assertions behind an `if` or after a `return` are not credited, so a check moved
  into such a helper blocks.
- `tests.weakened`: a loop over a literal table whose body can leave the row outside its
  `with self.subTest(...)` block (`break`, `continue`, `return`, `raise`, a skip) counts only
  its first row, since later rows may never run.
- `tests.skipped`: a test file moved or renamed onto a live `collect_ignore` or
  `collect_ignore_glob` entry of a `conftest.py` blocks. Every write to the list is read, not
  only the first assignment.
- `runner.tampered`: a deleted root Makefile is followed, and a target that was in it and is
  gone blocks. `-i` in `MAKEFLAGS` or `MFLAGS`, and `.IGNORE` bare or naming the target, block.
  A command substitution in a check's arguments warns. An `include` of a `$(wildcard ...)` glob
  that matches tracked files is read; an include that the Makefile itself rebuilds warns.
- `runner.tampered`: nox commands held in a name bound once, or returned by one same-file
  helper, are read, and a helper's literal arguments are bound. A name set through `globals()`,
  `exec`, `setattr` or a star import is unknown and warns.
- `runner.tampered`: a policy script that no longer runs a kind of check its base ran blocks,
  even when other lines remain (`pytest` and `ruff check .` becoming `ruff check .`): "it no
  longer runs `pytest`". The scripts it calls are read into it, to depth 4, 64 files per side
  and 1 MiB per file. The lint kinds are one kind, and a call that cannot be read leaves the
  script unjudged by kind. A script that a recipe of the base Makefile runs counts as run by the
  policy command that runs the target.
- `runner.tampered` and `ci.tampered`: npm `-w`/`--workspace` is package selection. `npm test`
  or `npm test --workspaces` becoming `npm test -w web` blocks ("npm ran every package before
  and now only `web`"), and widening passes. In a workflow step the selection is the step's own
  (the words before `--`), and the root `test` script is no longer read in place of the package
  the step selects.
- `ci.tampered`: where errexit is off, a subshell or group around a check that ends in another
  command (`( pytest; echo done )`) swallows the check, and so does one with `exit 0`,
  `return 0` or `trap ... EXIT` (or `ERR`). `( pytest )` alone is honest, and so is
  `( pytest; exit 0 )` under `set -e`. A GitHub `run:` step runs with errexit on, so these
  apply to scripts and other CI files that run without it.
- `ci.tampered`: a `cp`, `install`, `ln` or `rsync` into the test tree blocks when it lands on a
  test file, on a file that decides the runner, or on a tracked file it would overwrite. It
  warns for a new non-test file. A copy with several sources or a computed destination is not
  read.
- `ci.tampered`: `nx run-many` becoming `nx affected`, or `nx affected` added to a monorepo
  test step, warns (0.3.0 did this in `runner.tampered` only).
- The release workflow publishes the OpenCode plugin to npm as `opencode-greenproof` after PyPI
  accepts the release, with npm trusted publishing from the `npm` environment: no token is
  stored, and a tag that does not match the package's version fails before anything is
  published.

### Changed

- `tests.removed`: a test renamed while its body was edited is paired with its old self when
  the match is unique and the bodies share most of their tokens (0.6 in the same file, 0.75
  across files; a body of at least 10 tokens). Repeated leaf names are told apart by the
  trailing parts of the qualified name, and names equal once case and punctuation are ignored
  are paired. A paired test goes through the weakening comparison, so a rename that also drops
  a check blocks as `tests.weakened`. Below the thresholds, or on a tie, it stays a removal and
  blocks.
- `tests.skipped`: a `collect_ignore` entry that hides only non-test files is no longer a skip.
- `tests.weakened`: `pytest.approx` with the default tolerance, or a literal `rel` or `abs` of
  1e-6 or less, counts as a pinned value, and so does a value built by a class imported from
  outside the test tree (`QueryParams('a=1')`). A class defined in the test tree earns no
  credit.
- `tests.weakened` (Go): a drop in the line count of checks warns instead of blocking when the
  credited count is at least the base's and every number or string literal of the removed
  checks is still present ("by line; helper checks or table rows give N against M"). The
  credit is a helper's checks that run on every path, and a loop over a literal table of
  distinct rows times the loop's live checks that read the row. A row that is only assigned to
  `_`, printed or logged is not read. Rust and JS loops that replace separate checks still
  block.
- `tests.skipped` (Go): a conditional `t.Skip` that only new table rows reach is excused only
  inside a `t.Run` closure, on a field the checks never read, and only when every base row is
  still present and unchanged.
- The line-level flow reader for Go, Rust and JS also reads Rust `let ... else`, labelled
  `break` and `continue`, `goto`, and `switch` fallthrough when deciding whether a check runs.
- `runner.tampered`: more than 32 different pytest runs (was 8) are needed before the runs are
  no longer compared one by one.
- `runner.tampered`: a package-filter rename or glob (`web` to `app-*`) passes when it still
  selects every package the old filter selected, read from the `package.json` names in the
  tree, and warns when it does not.
- `runner.tampered`: a masked check line in a recipe (`-pytest`, `|| true`) is credited when
  another line of the target runs the same check unmasked. Under `.ONESHELL` this holds only
  when the mask is a pure no-op (`true`, `:`, `echo` or `printf` of literal words) and every
  statement of the recipe is plain; a `PATH=` line only warns.
- Scripts and actions are read up to 1 MiB (was 64 KiB), for `ci.tampered` and for the scripts a
  policy command runs. A step's reading budget is 32 files and 2 MiB (was 512 KiB). Past a cap,
  a change to what the file runs warns.

### Fixed

- A quoted `;`, `}`, `)` or `|` in a CI command is an argument, not a terminator, so a group
  around a check no longer turns `tests;` or `}` into a selection.
- A Makefile `include` guarded by a wildcard test of its own file no longer warns.

## [0.3.1] - 2026-11-08

### Fixed

- An interrupted check no longer waits forever for the process it killed. When SIGINT or
  SIGTERM arrives while a policy command or the smoke start command runs, greenproof's handler
  raises inside `Popen.wait` or `Popen.poll`, and that leaves the Popen's internal lock held
  about once in a hundred signals: 95 of 8,576 timed waits on CPython 3.11, 62 of 6,847 on 3.12
  and 67 of 6,781 on 3.14. After that no wait on the process returns, so the unbounded wait
  after SIGKILL hung. It is now bounded at two seconds (also when the `git cat-file` reader is
  killed), and the process is reaped when greenproof exits. CI showed it as a 30-second timeout in
  `test_sigint_removes_the_worktree_and_kills_the_command`.

### Changed

- CI, the release workflow, the GitHub Action and the workflow that `greenproof init --ci github`
  writes use astral-sh/setup-uv 10.3.3.

## [0.3.0] - 2026-11-03

Measured against 0.2.0 on the same inputs (MEASUREMENTS.md): of 684 tamper probes, 638 block
and 46 warn (0.2.0: 187 and 42); of 450 honest probes, 2 block (0.2.0: 87). On real history,
75 of 698 test-touching commits block (0.2.0: 70), 21 of them false blocks (0.2.0: 20).

### Added

- `scripts/release_notes.py X.Y.Z`, which prints the GitHub Release notes for a version: the
  install line, that version's CHANGELOG section and the compare link from the previous
  release. `tests/test_release_notes.py` runs it against the real CHANGELOG.
- The release workflow checks that CHANGELOG.md has a section for the tag before it builds
  anything for PyPI, and after PyPI accepts the upload it creates the GitHub Release with that
  section as the notes and the built files attached.
- Link references at the end of the CHANGELOG for each version and for `[Unreleased]`.
- A report note on the default branch with no upstream: only uncommitted changes are checked,
  and `--base REF` includes commits. `bench/replay.py` takes `--select runner` (commits that
  touch CI, runner configs, `package.json`, Makefiles or shell scripts) and `--command CMD`.
- `runner.tampered` reads more of what chooses which tests run. tox and nox: the `commands` of
  each tox environment and each nox session are read, and a `-` prefix on a check
  (`commands = - pytest`), `ignore_errors` or `ignore_outcome` on an environment, a nox
  `success_codes` widened (`[0, 1, 5]`, `range(10)`), a `session.run` inside a `try` whose
  `except` swallows it, a check dropped (`ruff check .` became `echo skipped`) and a nox
  `session.run` path narrowed (`tests` to `tests/test_shop.py`) block. `{envpython}`,
  `{toxinidir}` and `{[section]key}` are normalised first, so `python -m pytest` for `pytest`
  passes, and a workflow step that runs `tox` or `nox` is judged the same way. A bare `tox` or
  `nox` is judged by what its default environments or sessions run, so a shorter `envlist` or a
  narrower `{posargs:...}` default blocks. Also read as
  selection: Go `-tags` dropped or narrowed; jest and vitest path flags (`--testPathPatterns`,
  `--dir`) and changed-file flags (`--changedSince`, `--onlyChanged`, `--lastCommit`,
  `--changed`, `--related`); cargo target kinds (`--lib`, `--bins`, `--bin`, `--test`, `--doc`,
  `--example`), `--workspace` replaced by `-p`, and a dropped `--features` or `--all-features`.
  Widening passes.
- Monorepo package filters are selection, in `runner.tampered` and `ci.tampered`: turbo
  `--filter`/`-F` (including `!docs`), pnpm `--filter`/`-F`, nx `run-many` `--projects`/`-p` and
  `--exclude`, lerna `--scope`/`--ignore`, and yarn `workspaces foreach` `--include`/`--exclude`.
  A filter added where the base had none, or one that leaves out a package the base ran, blocks
  (`turbo run test` to `turbo run test --filter=web`); a dropped or widened filter passes, and a
  rename or a glob (`web` to `app-*`) warns. In `runner.tampered`, `nx run-many` becoming
  `nx affected` warns.
- A test command that names exact test files covering every file the base path selected
  (`pytest .` to `pytest test_shop.py` when that is the only test file) is a warn in
  `runner.tampered`, in any directory, and says that test files added later will not run. Fewer
  files, or a test file added in the same change, still blocks.
- `runner.tampered` reads these as selection: pytest `python_files`, `python_classes`,
  `python_functions`, `testpaths`, `norecursedirs`, `--doctest-modules` and `--doctest-glob`, `-o`
  and `--override-ini` of any of them, and the keys of jest (`roots`, `rootDir`,
  `modulePathIgnorePatterns`, `testRegex`), vitest (`dir`) and mocha (`spec`, `ignore`,
  `recursive`) that choose or ignore test files. Each pytest run is judged from the config's
  `addopts`, then `PYTEST_ADDOPTS`, then the run's own flags (a command-line `-o` beats
  `addopts`), following the run through `make` (`-C` included), `npm run` and shell scripts. A
  flag moved between `addopts` and the command line passes, and one dropped from a command line
  blocks. With more than 8 different pytest runs the runs are not compared one by one: the
  configuration alone is judged, and a change to the runs warns.
- Makefile reading follows `include` files (literal paths, simple variables, `$(CURDIR)` and
  `$(dir $(lastword $(MAKEFILE_LIST)))`), `define` macros used through `$(call ...)`,
  conditionals (`ifeq`, `ifdef` and the like, read as alternative worlds, so an honest edit in
  one branch passes and a swallow in any branch blocks), double-colon, pattern and
  multi-target rules, and a `GNUmakefile` that make reads before `Makefile`.
- Makefile recipes are judged in the directory they run in (`cd dir && ...`, `make -C dir`),
  target-specific variables are read in one pass, and a Makefile read only in part still judges
  the goal's recipe. `set -o pipefail` keeps a pipeline's status when `SHELL` is bash, a line
  that masks a check is credited when another line of the target runs the same check unmasked,
  and a touched line with `$(call ...)` or `$(eval ...)` warns, once per target.
- `ci.tampered` judges a `branches` or `branches-ignore` filter added under `push`: it passes
  while `pull_request` still runs on every branch, and warns when it takes the default branch
  (`main` or `master`) off `push`, or when `pull_request` is itself filtered or gone from the
  workflow (a dropped `pull_request` also blocks, as before). It also reads kept exit statuses:
  `rc=$?; ...; exit 0` swallows the check, `exit $rc` keeps it.

### Changed

- Base resolution: on the default branch the base is the merge base with the upstream, so
  commits not yet pushed are checked with the uncommitted changes. With no upstream, or HEAD
  equal to it (CI on a push), only uncommitted changes are compared. An upstream that shares
  no history with HEAD (a shallow clone) is exit 4 with the `fetch-depth: 0` hint, not a
  silent fall back to `HEAD`.
- Python test analysis: `tests.weakened` (block) now fires for a test removed by a later `def`,
  `del` or rebinding of its name, a swallowing decorator added to a test, `assert [x]` or a
  non-empty string or tuple, `x == x`, and an exact numeric check loosened to a `pytest.approx`,
  `math.isclose` or `assertAlmostEqual` band of 1% of the expected value or more. A bare
  `pytest.approx(99)`, a tight tolerance and a retry or cleanup decorator that re-raises pass.
- A comparison with a call (`f(x) == f(x)`, `get_settings() is get_settings()`) is a real
  determinism or singleton check and passes, unless it replaces a comparison of that call in
  the same test (`assert refund(100) == 99` to `== refund(100)`), which blocks. `del` of a base
  class that tests subclass removes no tests.
- Helper credit follows imports of test-path modules and of test-support modules (those that
  import `pytest`, `unittest` or `hypothesis`, or sit under `testing`, `test_utils` or
  `pytest_plugin`), up to 8 calls deep; application modules earn nothing. A helper or
  `conftest.py` that loses checks re-analyses the unchanged tests that use it (at most 300
  files read and 200 compared, with no note at the cap); one that loses none does not.
- Table folds: folding several tests into one table-driven test (a parametrized Python test,
  `it.each`, a Go table, Rust `#[case]`) keeps its `tests.replaced` warn, which 0.2.0 blocked,
  only when the table runs, no row leaves early and the row values reach an assertion. It blocks
  when an assertion sits inside a `try`/`except` or `try`/`catch` that swallows the failure, when
  it is `assert a or b` with `a` depending on a row, or when a row carries a `skip`, `skipif` or
  `xfail` mark of a kind the folded tests did not already carry. A Python `case _` or a Rust
  catch-all arm that closes a `match`, `expect(() => f(row)).toThrow()` and `.rejects.*` count as
  assertions, and a kept `xfail` row passes. A fold body past the reading budget keeps the warn
  and says it is too large to check.
- Python warnings: `tests.expected_changed` also fires when a `@pytest.fixture` returns a
  different literal, and `tests.self_mocked` when an added or changed autouse fixture patches
  a callable the tests in its scope assert on.
- Go, JavaScript and Rust test analysis: `tests.weakened` (block) now fires for a top-level
  bare `return`, `if (true) return;` or `x || true` guard before the checks, checks under
  `if false` or `&& false`, `expect(X).toBe(X)` self-equality of a call-free expression, and
  fewer rows in an `it.each`, `test.each` or `describe.each` table. Call self-comparisons follow
  the Python rule. A `return` that is the body of a brace-less `if`, `else`, `for` or `while`,
  `return expect(...)`, Go's `got != got` and a table with a spread or a non-literal table pass.
  Helpers in the same file (JS) or directory (Go) earn credit.
- A Go `//go:build` or `// +build` constraint added to an existing `_test.go` file that had none
  is `tests.skipped` (block); one that differs from the file's own warns. gofmt's migration to
  `//go:build`, go fix's `// +build` removal and a constraint on a new test file give nothing.
- Data files: a modified, or renamed with changed content, expected-output file (`.json`,
  `.snap`, `.golden`, `.csv` and similar) under a test path or `testdata`, `fixtures`, `golden`
  or `__snapshots__` is `tests.expected_changed` (warn), once per file. Dependency, compose,
  `Dockerfile*`, `.pre-commit*`, `codecov.yml` and `.env*` files and pure renames are skipped.
- `ci.tampered` (block) judges CI across all workflow files and looks through local composite
  actions, local scripts, `bash -c` and launchers such as `docker compose run` and `uv run`.
  It blocks a swallowed check (`|| true`, `|| echo`, `set +e`, `; exit 0` under a shell with no
  errexit), `continue-on-error: true` or `if: false` on a check, `--collect-only`,
  `--passWithNoTests`, `exit 0` before the first check, a selection flag (`-k`, `-m`, `-run`),
  fewer events, a `paths` filter that hides a test or source file, and a changed greenproof
  step or `with:` input. Only GitHub `run:` steps get the flag checks; other CI files get the
  swallow check.
- A workflow keeps its greenproof step only if it keeps an automatic code trigger (`push`,
  `pull_request`, `pull_request_target` or `merge_group`) that runs on every ref; moving the step
  to a manual, scheduled or branch-filtered workflow blocks. Check kinds are per tool (`ruff` and
  `flake8` differ), so swapping one for the other blocks; an unclassifiable replacement (`poe
  test`) warns only in the same file and job, and fewer steps of a surviving kind warn.
- `ci.tampered` passes a split into a reusable workflow, a `paths` filter that only excludes docs
  or meta files, `rm -rf build || true`, `continue-on-error` on checkout, cache or upload steps,
  and `pytest || pytest`, `pytest || pytest --lf` (a retry, not a swallow). Narrowed
  `branches`, `tags` or `types` filters (not a `push` `branches` or `branches-ignore` filter that
  leaves the default branch alone while `pull_request` stays open), a matrix-chosen flag, `continue-on-error: ${{ ... }}`, a mocha
  selection change and a ref-only greenproof bump warn. An `if: ''` is read as false, and
  `if: "false"` blocks.
- `runner.tampered` (block) reads swallows in Makefiles, `package.json` scripts and the files a
  policy command runs (deleted, emptied, no-op, swallow, selection flag, early `exit 0`). A Go
  `TestMain` that skips `m.Run()` or always exits 0 blocks, a conditional exit warns. Fewer
  steps count checks only, and a script that stops running a kind of check the base ran (`jest`
  became `eslint`) blocks, with lint tools as one kind. Makefile variables are expanded, and a
  target named like an existing path and not in `.PHONY` runs nothing.
- Flags in `runner.tampered` belong to their tool (`-k` is pytest's, mocha's `-t` is a timeout).
  `-p no:<plugin>` is judged by the plugin: `cacheprovider`, `randomly` and `xdist` pass,
  `python`, `doctest`, `cov` and `asyncio` block, `warnings` blocks where the base turns warnings
  into errors, unknown plugins warn, and `--disable-plugin-autoload` warns. A nested
  `package.json` no policy command reaches warns; with a workspace, filter, `cd` or `lerna`
  in the policy, it blocks.
- The shared shell reader (`rules/shell.py`) counts only a known test runner, linter, type
  checker or policy command in command position as a check, so `pip install pytest-xdist`
  is not one. Heredocs are data, and only swallows the base lacked are reported.
- Performance: git blobs are read through one `git cat-file --batch` process, workflows are
  parsed once, and swallow detection, `paths` globs and `pnpm run '/regex/'` run in linear time.
  The shell reader, the tox and nox readers, the monorepo filter reader and the comparison of
  selected paths also take time linear in their input.

### Fixed

- A `pnpm run '/regex/'` pattern from the candidate could hang the gate (`/^(a+)+$/`); it is now
  matched by an automaton with a work budget. Names are no longer cut at 200 characters, which
  let `/^[a-z]*$/` match a long name ending in `!`.
- An `if:` wrapped in redundant parentheses is read as the expression inside, however many
  pairs wrap it, and the wrapping does not count toward depth. A new or changed `if:` with more
  than 32 levels of real `&&`/`||` nesting on a job or step that runs a check is a warn, "has an
  `if:` nested too deeply to read".
- Dropping a workspace member from `pnpm-workspace.yaml` or `workspaces` made its neutered
  `package.json` a warn; it now blocks.
- A huge numeric literal or a very deep expression in a test no longer crashes the check, and
  Rust character literals such as `'}'` no longer break brace matching.
- False blocks cleared: a `|| true` after a command that is not a check (`rm -rf build`,
  `pip install x`), `-p no:randomly` and other plugins that only reorder or report, and a
  nested `package.json` that no policy command reaches (now a warn).

## [0.2.0] - 2026-11-02

### Added

- A plugin for Claude Code, Codex and Cursor (`plugins/greenproof`) that installs the Stop
  and SessionStart hooks without running `greenproof init`, with a marketplace file for each
  agent (`.claude-plugin/`, `.agents/plugins/`, `.cursor-plugin/`). Each manifest names its
  own hooks file, so no agent loads another's. The hooks run `uvx greenproof@<version>`,
  pinned to the release; the plugin also carries a skill that tells the agent how to answer
  a block.
- `scripts/bump_version.py X.Y.Z`, which sets the version in `_version.py`, the manifests and
  every pinned command in the plugin, and `tests/test_plugins.py`, which fails when any of
  them differs from `__version__` and checks each manifest against its vendor's format.
- `scripts/make_icon.py`, the standard-library script that draws the plugin icon.
- An OpenCode 2.x plugin (`integrations/opencode`, package `opencode-greenproof`, also
  packaged inside greenproof): on a top-level session going idle it runs
  `greenproof check --hook opencode` and continues the same run with `ctx.session.synthetic`
  on a block; a release is a console warning and every failure fails open. It starts a
  session on the first prompt of a top-level session (the `prompt` hook), skips subagent
  sessions, runs one check per session at a time and gives way to a second copy loaded in the
  same process. Plain ES module with only `node:` imports; 2.x only.
- `greenproof init --agents opencode` writes `.opencode/plugins/greenproof.js` with the hook
  command set. The file is replaced only when its first line says greenproof wrote it; any
  other file there is a usage error (exit 4) and nothing is written. `--agents auto` also
  installs it when `.opencode/`, `opencode.json` or `opencode.jsonc` exists.
- A CI job that runs `node --test` on the plugin (actions/setup-node pinned to a commit) and
  checks that `npm pack` lists only `index.js`, `README.md`, `LICENSE` and `package.json`.
- `scripts/e2e/opencode.py`, an end-to-end run of the plugin in the real OpenCode CLI against
  a fake OpenAI-compatible model server (`--self-test` checks only the server). It is not
  part of pytest.
- `scripts/bump_version.py` also sets the npm package's version and the command pinned in
  its copy of the plugin.
- `check --hook opencode` and `session-start --hook opencode`, for the OpenCode plugin: the
  payload carries `session_id` and `cwd`, and the output is Codex's protocol (block, pass,
  release) and Claude Code's session-start output.
- Duplicate hooks for one stop. The gate can be installed twice (a plugin and the hooks
  `init` writes), and the agent then runs both in parallel. Everything after repository
  discovery runs under an exclusive lock on `<git-dir>/greenproof/hook.lock`, the hook state
  records the payload's fingerprint (a sha256 of the sorted, compact payload), the decision
  and the block message, and a second hook for the same stop (same payload, same tree)
  outputs that decision again (at most three replays) instead of finding the first one's
  block and releasing the agent.
  When the lock file cannot be opened, the hook runs without the lock.
- `fork` in the SessionStart matchers of the Claude Code and Codex CLI hooks `init` writes.
  Both agents document it as a source; the session marker is kept, as for `resume`.

### Fixed

- `greenproof check` in local mode no longer exits 4 when the git directory is read-only, as
  in an agent sandbox that lets the agent write the working tree but not `.git` (Codex's
  workspace-write): `git add -A` could not store the snapshot's objects. When the snapshot
  fails and a probe file cannot be created in the object directory, greenproof retries with
  a temporary object directory (`GIT_OBJECT_DIRECTORY`, with the repository's objects as
  `GIT_ALTERNATE_OBJECT_DIRECTORIES`) for every git command of the run, deletes it on exit
  and writes nothing under the git directory, so no pass is cached. The revert check is
  skipped with a note, `--isolate` exits 4, and the report carries a note. Hook and CI mode
  still fail closed on a git failure.

### Changed

- A pass is now recorded in the hook state instead of clearing it, so a duplicate hook for
  the same stop can replay it. Nothing to verify still clears the state. A state file
  written by 0.1.0 is read as a block with no fingerprint.
- `suppress.added` applies each generic pattern only where it has an effect. `|| true`,
  `; true`, `set +e` and the git `--no-verify` pattern fire in shell-capable files (scripts,
  makefiles, justfiles, CI and container files, `package.json`, `pyproject.toml`,
  `tox.ini`, `setup.cfg`, `.husky/` and `.githooks/`), and `--passWithNoTests` also in
  JavaScript and TypeScript. They no longer fire in `.toml` case files, `.json` data,
  prose or source files of other languages.
- The README says that `uvx` hook commands need `uv` on PATH.

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

[Unreleased]: https://github.com/technobasant/greenproof/compare/v0.4.1...HEAD
[0.4.1]: https://github.com/technobasant/greenproof/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/technobasant/greenproof/compare/v0.3.1...v0.4.0
[0.3.1]: https://github.com/technobasant/greenproof/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/technobasant/greenproof/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/technobasant/greenproof/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/technobasant/greenproof/releases/tag/v0.1.0
