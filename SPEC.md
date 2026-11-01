# greenproof — design spec (v0.1)

Status: approved design for implementation. Owner of design decisions: the maintainer
(reviewed by Opus). Implementers follow this document; anything it does not settle is a
question to report back, not a decision to make silently.

## 1. What it is

A definition-of-done gate for code written by LLM coding agents. It runs **outside the
agent**, the same way on a laptop (as an agent stop hook or git hook) and in CI, and answers
one question: *is the green real?*

Agents are very good at satisfying the stated constraint. Point one at a failing gate and
it will make the gate pass: delete the test, skip it, loosen the assertion, edit the
expected value, exclude the test group in CI, or report "all tests pass" from a run that is
stale. greenproof widens what the gate measures:

1. it runs the project's checks itself, now, instead of trusting the agent's report;
2. it reads its policy and the commands it runs **from the base revision**, so the agent
   cannot weaken the gate in the same change;
3. it inspects the diff for the specific ways tests and checks get weakened, and names
   each one with file, line and rule id;
4. optionally it proves new tests depend on the change (revert check) and boots the built
   artifact (smoke).

Non-goals for v0.1: LLM judging, code review, security scanning, coverage measurement,
Windows support (best effort only).

## 2. Principles (each is a requirement)

- **Names the thing.** Every finding has a rule id, severity, file, line (when there is
  one), a one-line message and a hint. No "something looks off".
- **Worth running.** Default severities must keep false positives on legitimate changes
  near zero; noisy heuristics default to `warn`. A rule that fires on clean refactors is a
  bug.
- **Executable, fail closed.** Any internal error or missing prerequisite yields a non-zero
  exit with a clear message, never a silent pass.
- **Agent-agnostic.** The core is a CLI; agent hooks and CI are thin adapters.
- **Zero runtime dependencies.** Python stdlib only (`ast`, `tomllib`, `subprocess`,
  `json`, `argparse`, `hashlib`, `fnmatch`, `shlex`, `tempfile`). Runs with `uvx greenproof`.
- **Honest about coverage.** Python test analysis is AST-based; JS/TS, Go and Rust are
  line-level heuristics in v0.1, and the README says so.

## 3. Commands

```
greenproof init   [--yes] [--agents auto|none|claude,cursor,codex,gemini] [--ci github|none] [--no-verify]
greenproof check  [--base REF] [--mode local|hook|ci] [--hook claude|cursor|codex|gemini]
                  [--checks static,commands,revert,smoke] [--json PATH] [--markdown PATH]
                  [--github-annotations] [--allow-policy-change] [--no-cache] [--isolate]
greenproof session-start [--hook claude|cursor|codex|gemini]
greenproof explain RULE_ID
greenproof --version
```

### 3.1 `check` pipeline

1. **Locate the repo** (`git rev-parse --show-toplevel`). Not a git repo → exit 4.
2. **Snapshot the candidate** without touching the user's index or working tree: seed a
   temporary index with a copy of the user's index (its stat cache means unchanged files are
   not re-hashed and clean filters such as Git LFS do not re-run), falling back to
   `GIT_INDEX_FILE=<tmp> git read-tree HEAD` when there is no index or the copy cannot be
   used; then `GIT_INDEX_FILE=<tmp> git add -A && GIT_INDEX_FILE=<tmp> git write-tree`.
   Both seeds must give the same tree. This includes staged, unstaged and untracked (non-ignored) files, because agents often
   do not commit. When `--isolate` or the revert check needs a checkout, create a commit
   object with `git commit-tree <tree> -p HEAD` (no ref is updated) and
   `git worktree add --detach <tmpdir> <commit>`; always remove the worktree afterwards,
   including on error and on SIGINT/SIGTERM.
3. **Resolve the base**, first match wins:
   1. `--base REF`;
   2. in GitHub Actions on a PR: `GITHUB_BASE_REF` → `git merge-base HEAD origin/<ref>` (the
      action fetches it; on the PR merge-commit checkout this stays correct after the base
      branch has advanced);
   3. the session marker written by `session-start` at
      `<git-dir>/greenproof/session-base`, if that commit is an ancestor of HEAD. The marker
      is per worktree (`git rev-parse --git-dir`), so parallel agent sessions in separate
      worktrees do not overwrite each other's base;
   4. `git merge-base HEAD <default branch>` where the default branch is
      `origin/HEAD`'s target, else `main`, else `master`; if HEAD *is* the default branch,
      use `HEAD` (only uncommitted changes are compared).
   Base not resolvable (e.g. shallow clone) → exit 4 with the fix (`fetch-depth: 0`).
4. **Load policy from the base**: `git show <base>:.greenproof.toml`. If the base has no
   policy file (first adoption), use the candidate's and record `policy.source = "candidate"`.
   No policy at the base or in the working tree → exit 4, telling the user to run
   `greenproof init`. Policies are compared on parsed TOML (comment and formatting edits are
   not a change); a deleted or unparseable candidate policy is a change, not a config error.
   If the candidate's policy differs from the base's, emit `policy.changed` (block) unless
   `--allow-policy-change`. The commands, test globs and severities used for this run all
   come from the base policy.
5. **Compute the change set**: `git diff --name-status -M -z <base> <candidate-tree>`, file
   contents for both sides via `git cat-file`. Binary files are skipped.
6. **Run checks** in this order, cheap first: `static` → `commands` → `revert` → `smoke`.
   Static findings never prevent commands from running (the report should be complete),
   except in `hook` mode where a blocking static finding skips the remaining checks to keep
   the agent loop fast.
7. **Cache**: key = sha256(candidate tree, base sha, policy hash, greenproof version, mode,
   checks, verdict-changing flags such as `--allow-policy-change`). Stored in
   `<git-common-dir>/greenproof/cache.json` (keep the last 50 entries) together with the
   check statuses of the original run, which a hit replays (a skipped check stays skipped).
   A previous *pass* for the same key returns pass immediately with
   "unchanged since the last verified pass". Failures are never cached. `--no-cache`
   disables it. **`ci` mode never reads or writes the cache**: the file is writable by
   anything running as the user, an agent included, so a cached pass is a speed-up for the
   local loop and never evidence. The key uses the snapshot taken before the commands run,
   so commands that leave untracked, non-ignored artifacts cause a miss next time.
8. **Report** (text to stdout, plus optional JSON/Markdown/annotations) and **exit**.

### 3.2 Exit codes

| code | meaning |
|---|---|
| 0 | pass |
| 1 | a blocking integrity finding (static or revert) |
| 2 | a configured command failed or timed out |
| 3 | smoke check failed |
| 4 | usage or configuration error (not a repo, base unresolvable, bad policy) |
| 5 | internal error (fail closed) |

When several apply, the lowest non-zero code among 1–3 wins (integrity is the most
important signal); 4 and 5 override everything. In `--hook` mode the process maps any
non-zero outcome to the agent's blocking protocol (section 6).

## 4. Policy file `.greenproof.toml`

```toml
version = 1

[commands]            # run in order; empty or missing entries are skipped
setup = "uv sync --frozen"      # only used for --isolate / revert worktrees
lint = "uv run ruff check ."
typecheck = "uv run mypy src"
test = "uv run pytest -q"
build = ""
timeout = 900                   # seconds, per command

[tests]
# Globs that identify test files. If omitted, language defaults apply (section 5.1).
paths = []

[smoke]                         # optional; all three keys required to enable
start = "uv run uvicorn app.main:app --port 8765"
ready_url = "http://127.0.0.1:8765/health"   # polled until HTTP 2xx/3xx
probes = ["curl -fsS http://127.0.0.1:8765/health"]
timeout = 60

[rules]                         # per-rule severity: "block" | "warn" | "off"
"tests.expected_changed" = "warn"

[modes.hook]
checks = ["static", "commands"]
[modes.local]
checks = ["static", "commands"]
[modes.ci]
checks = ["static", "commands", "revert", "smoke"]
```

Validation errors (unknown keys, wrong types, unknown rule ids) → exit 4 naming the key.

## 5. Static rules

Each rule has an id, a default severity, and fixtures in `tests/fixtures/rules/<id>/`
covering at least one true positive and one legitimate change that must NOT fire.

### 5.1 Test-file detection (defaults)

- Python: `test_*.py`, `*_test.py`, anything under a `tests/` or `test/` directory, and
  `conftest.py`.
- JS/TS: `*.test.{js,jsx,ts,tsx,mjs,cjs}`, `*.spec.*`, `__tests__/**`.
- Go: `*_test.go`. Rust: files under `tests/`, plus inline `#[cfg(test)]` modules
  (counted by `#[test]` attributes in any `.rs` file).

### 5.2 Rules

| id | default | fires when |
|---|---|---|
| `policy.changed` | block | the candidate's `.greenproof.toml` differs from the base's |
| `tests.deleted` | block | a test file is deleted, or renamed to a non-test path |
| `tests.removed` | block | a test function/case present at base is gone and is not covered (5.7), including a test moved to a file or name the runner does not collect |
| `tests.replaced` | block | in the same file, a test is removed and a different test is added (delete-and-replace) and the removed test is not covered (5.7); report both names. `warn` when the new tests are at least as strong (5.7, a rewrite) |
| `tests.skipped` | block | a skip/xfail/ignore/todo marker is added to a test that exists at base, or at module/class/conftest scope (see 5.3). On a brand-new test an unconditional skip/xfail is `warn` and a conditional one (`skipif(<condition>)`, `importorskip`) does not fire: neither hides existing verification |
| `tests.focused` | block | a focus marker is added (`it.only`, `fit`, `describe.only`, `test.only`), which silently skips the rest |
| `tests.weakened` | block | for a test present on both sides: fewer assertions, a strong assertion replaced by a weaker one, tolerance widened, parametrize cases reduced, an assertion made vacuous, tautological or unreachable, or an assertion wrapped in a swallowing `try/except` (see 5.4) |
| `tests.expected_changed` | warn | a literal in an assertion's expected side changed while non-test code also changed (legitimate when the spec changed, so warn) |
| `suppress.added` | warn | an added line introduces a type/lint/coverage suppression (see 5.5) |
| `runner.tampered` | block | a change narrows or neuters how checks run: test selection, thresholds, strictness, test scripts (see 5.6) |
| `ci.tampered` | block | a CI workflow change removes or neuters a test/lint step, adds `continue-on-error`, `|| true`, `if: false`, path filters excluding tests, or changes the greenproof step itself. A ref-only version bump of the greenproof action (same inputs) is `warn` |
| `static.parse_error` | warn | a changed file in a parsed language (Python, TOML, JSON, INI) does not parse; that file falls back to the line-level checks |
| `tests.self_mocked` | warn | (M3) a test patches (`monkeypatch.setattr`, `mock.patch`, `jest.mock`, `vi.mock`) the same callable its assertions call, so it tests the mock |
| `code.hardcoded` | warn | non-test code gains a literal table (≥ 8 entries) or an `if x == <literal>` branch whose literal also appears in a test file or fixture changed in this diff (Python AST only in v0.1) |

### 5.3 Skip/ignore markers

- Python: `@pytest.mark.skip`, `@pytest.mark.skipif`, `@pytest.mark.xfail`,
  `pytest.skip(`, `pytest.xfail(`, `pytest.importorskip(` added to an existing test,
  `@unittest.skip*`, `raise unittest.SkipTest`, `collect_ignore` / `collect_ignore_glob`
  and `pytest_collection_modifyitems` added to `conftest.py`, `__test__ = False` added at
  module or class scope, `pytestmark` gaining a skip/xfail.
- JS/TS: `.skip(`, `xit(`, `xdescribe(`, `xtest(`, `.todo(`, `test.fails(` added.
- Go: `t.Skip(`, `t.SkipNow(`, `t.Skipf(`. Rust: `#[ignore]`.

### 5.4 Weakening heuristics

Python (AST, matched by qualified test name `path::Class::test_name`):
- the test's **effective check count** drops. It is the number of reachable, non-vacuous
  assertions (`assert` statements, `self.assert*` calls, `pytest.raises`/`pytest.warns`
  contexts, `assert_*` calls on mocks) times the test's parametrize cases (the product
  over stacked `parametrize` decorators whose argvalues are a literal list or tuple; when
  either side has a non-literal argvalues, both sides are compared without the case
  factor). Turning six asserts into one assert over six cases is not a drop. There is no
  finding when every distinct exact assertion form (5.7) of the base test still appears in
  the candidate test or among the assertions the change adds: dropping a duplicated
  assertion, or moving one into another test, is not weakening;
- `assert a == b` / `assertEqual` / `assertIn` / `assertRaises(SpecificError)` replaced by
  a truthiness or existence check (`assert a`, `assert a is not None`, `assertTrue`,
  `assertIsNotNone`) or by a broader exception (`Exception`, `BaseException`);
- `pytest.approx` `rel`/`abs` increased, `assertAlmostEqual` `places` decreased or `delta`
  increased;
- `@pytest.mark.parametrize` cases removed, unless every removed case (the exact form of
  its argvalues element) appears among the cases the change adds (all cases of new tests,
  plus the cases a paired test gained, as a multiset difference). Splitting cases out
  into other tests or files is not weakening;
- an assertion moved inside `try:` whose handler catches `AssertionError`/`Exception`
  without re-raising;
- an assertion made vacuous or tautological: `assert <truthy constant>` (`assert True`,
  `assert 1`), `self.assertTrue(True)`, an `or` with a truthy constant operand
  (`assert x == 3 or True`), or a comparison of an expression with itself;
- an assertion whose inputs are all constants, so it checks nothing about the code:
  every operand is a literal, a pure builtin (`len`, `abs`, `min`, `max`, `sum`, `sorted`,
  `reversed`, `list`, `tuple`, `set`, `frozenset`, `dict`, `str`, `int`, `float`, `bool`,
  `round`, `repr`) applied to constant arguments, an operator over constants, or a local
  name assigned exactly once in the test body, before the assertion, from a constant
  expression and not used before it in any other way (passed to a call, its attribute or
  method accessed, subscript-, attribute- or augmented-assigned). Names bound by
  parameters, fixtures, `parametrize`, loops, comprehensions, `with ... as`, imports or
  module scope are never constant. Example: `got = parse("4")` → `got = 4` with
  `assert got == want` unchanged. Such an assertion is vacuous, like `assert True`;
  vacuous assertions do not count toward assertion totals or 5.7 coverage;
- an assertion made unreachable: an unconditional `return`/`raise` (outside a
  `pytest.raises`/`assertRaises` block) placed before it in the test body, or the
  assertion moved under `if False:`/`if 0:`. Unreachable assertions do not count;
- an **exact check loosened**. An exact check pins a value: an `assert` whose test is a
  single `==` or `is` comparison (including `is None`), `assertEqual`, `assertIs`,
  `assertIsNone`, `assertListEqual`, `assertDictEqual`, `assertTupleEqual`,
  `assertSetEqual`, `assertSequenceEqual`, `assertMultiLineEqual`, `assertCountEqual` or
  `assertAlmostEqual`. Its **subjects** are its operands that are not constant (a literal,
  or an operator, container or pure builtin over literals, as in the constant rule above),
  compared as shapes after single-assignment locals are inlined. A **loose check** on `X`
  is any other assertion in which `X` is: the whole test (`assert X`, `assertTrue(X)`); an
  operand of `is not`, `!=`, `<`, `<=`, `>`, `>=`, `not in` (`assert X > 0`,
  `assert 0 < X < 10`, `assertNotEqual`, `assertGreater`, `assertGreaterEqual`,
  `assertLess`, `assertLessEqual`, `assertIsNotNone`); the first argument of `isinstance`,
  `issubclass` or `assertIsInstance`; or wrapped as `type(X)`, `len(X)` or `bool(X)` in
  any comparison (`assert type(X) is int`, `assert len(X) == 3`). A subject of an exact
  check in the base test that is the subject of no exact check in the candidate test, and
  is the target of a loose check there, is loosened: `tests.weakened` (block), "exact
  check on `<X>` loosened to `<the new assertion's source>`". This includes
  `assert total() == 100` → `assert total()`, → `assert total() > 0`, →
  `assert isinstance(total(), int)` and `assert rows == [a, b]` → `assert len(rows) == 2`.
  When the new assertion is a different expression (`assert f(x) == 1` →
  `assert is_one(x)`) it is a `warn` for a strong check replaced by a different one,
  because that is often a legitimate refactor.

JS/TS, Go, Rust (line level, per test file): count of assertion calls drops (`expect(`,
`assert.`, `t.Error`/`t.Fatal`/`require.`/`assert.`, `assert!`/`assert_eq!`), or strong
matchers are removed while weak ones are added: JS/TS strong `toEqual`, `toBe(`,
`toStrictEqual`, `toMatchObject`, `toThrow(<arg>)`, weak `toBeDefined`, `toBeTruthy`,
`toBeFalsy`, `not.toBeNull`, `not.toBeUndefined`, `not.toThrow`, `toBeGreaterThan`,
`toBeGreaterThanOrEqual`, `toBeLessThan`, `toBeLessThanOrEqual`, `toBeInstanceOf`,
`toHaveLength`, `expect.any(`, `not.toBe(`, `not.toEqual(`; Go strong `assert.Equal(`,
`assert.EqualValues(`, `require.Equal(`, `require.EqualValues(`, weak `.NotNil(`,
`.True(`, `.NotEmpty(`, `.Greater(`, `.GreaterOrEqual(`, `.Less(`, `.LessOrEqual(`,
`.IsType(`, `.NotEqual(`, `.Len(`; Rust strong `assert_eq!(`, weak `assert_ne!(`,
`.is_some())`, `.is_ok())`, `.is_empty())`, and `assert!(` lines containing `>`, `<`
or `!=`. An `expect(<literal>)` (number, string, `true`/`false`/`null`/`undefined`) is
vacuous and not counted, so `expect(add(1, 2)).toBe(3)` → `expect(3).toBe(3)` drops the count.

### 5.5 Suppressions (`suppress.added`, warn by default; each pattern its own sub-id in the message)

`# type: ignore`, `# noqa`, `# pragma: no cover`, `# pyright: ignore`, `@ts-ignore`,
`@ts-expect-error`, `@ts-nocheck`, `eslint-disable`, `biome-ignore`, `//nolint`,
`#[allow(`, `as any`, `: any` (TS), `|| true`, `; true$`, `set +e`, `--passWithNoTests`,
`--no-verify`. Matching is on added lines only, ignoring lines whose content already
existed elsewhere in the same file at base (moved code is not new suppression).

### 5.6 Runner tampering (`runner.tampered`)

Structural diff (parse TOML/JSON/INI with stdlib; YAML by line heuristics) of:
- `pyproject.toml` `[tool.pytest.ini_options]` (`testpaths`, `addopts` adding `-k`,
  `--ignore`, `--deselect`, `-p no:`), `[tool.coverage.report] fail_under` lowered,
  `[tool.mypy]` (`strict` off, `ignore_errors`, `disable_error_code` added), `fail_under`
  removed (counts as lowered), `python_files`/`python_functions`/`python_classes` narrowed,
  `[tool.ruff.lint]` (`ignore`/`extend-ignore`/`per-file-ignores` added);
- `pytest.ini`, `setup.cfg`, `tox.ini`, `noxfile.py` equivalents;
- `package.json` `scripts` whose names appear in the base policy's commands, plus
  `test`, `lint`, `typecheck`; `jest.config.*`/`vitest.config.*` (`testPathIgnorePatterns`,
  `exclude`, `coverageThreshold` lowered, `passWithNoTests`); `tsconfig*.json` (`strict`
  or `noImplicitAny` set false, `skipLibCheck` is ignored);
- `Makefile`/`justfile` targets referenced by the base policy's commands;
- `conftest.py` or a local pytest plugin gaining a hook that can rewrite outcomes or
  collection (`pytest_runtest_makereport`, `pytest_runtest_call`, `pytest_runtest_protocol`,
  `pytest_pyfunc_call`, `pytest_report_teststatus`, `pytest_ignore_collect`,
  `pytest_collect_file`, or `pytest_sessionfinish` assigning `exitstatus`), and
  `os._exit(` or `sys.exit(0)` added to a test or conftest file.
When structural parsing fails, fall back to a file-level `warn` naming the file.

### 5.7 Test identity and covered removals

Base tests are paired with candidate tests in passes, each pairing a test at most once:
1. same path and qualified name; 2. same qualified name in another file; 3. same
normalised body hash; 4. same *structural* hash (identifiers alpha-renamed; constants,
operators and assertion shapes kept) when the match is one-to-one within the change set.
Only candidate tests the runner will **collect** can be paired: the file matches the
runner's collection patterns (pytest `python_files`/`python_classes`/`python_functions`
read from the base revision's pytest config, else `test_*.py`/`*_test.py`, `Test*`,
`test*`; Jest/Vitest `*.test.*`, `*.spec.*`, `__tests__/`; Go `*_test.go`), and the
module or class is not disabled by `__test__ = False`. A test file renamed to a path
the runner does not collect is `tests.deleted` even when its content is unchanged.

A base test left unpaired is **covered**, and produces no finding, when every one of its
assertions appears among the assertions the change **adds**: all assertions of collected
candidate tests left unpaired (new tests), plus, for each paired test, the candidate's
assertions minus its base test's (a multiset difference). Assertions are compared in
**exact form**: the parsed assertion with identifiers and constants kept and only
formatting normalised (Python: the AST dump without positions; line engines: the assertion
line with whitespace removed). Unreachable assertions (5.4) are not counted.

Both limits matter. Generic assertions such as `assert got == want` or
`assert len(rows) == 1` would, in structural form, match almost any other test. An
assertion that already existed sat alongside the removed test before the change, so it
replaces nothing. Renames that also rename identifiers are handled by pass 4, not by
coverage. This keeps renames, splits, merges (including into an existing test) and moves
clean, while a deleted test, or one replaced by a weaker test, still fires. Paired tests
are then compared by the 5.4 heuristics.

A base test is also covered when a collected candidate test, new or pre-existing, has the
same normalised body hash and the same parametrize cases. The removed test was an exact
duplicate, and its checks still run.

**`tests.replaced` severity** (Python; the line engines stay `block`). Per file, the
removed uncovered tests are compared with the new unpaired tests. The finding is `warn`,
a rewrite, when all of these hold:
- the new tests together call every **subject** the removed tests called. A subject is
  the dotted name of a called function or method that is not a builtin, not
  pytest/unittest/mock vocabulary, and not bound inside the test itself;
- their combined effective check count (5.4) is at least the removed tests';
- every subject of an exact check in the removed tests that the new tests check at all
  is the subject of an exact check in the new tests (no loosened check, 5.4);
- every literal expected value of the removed tests (the literal side of an exact
  check) appears as a constant somewhere in the new tests (their assertions or their
  parametrize cases). A rewrite keeps what the old tests pinned.

Otherwise it is `block`. A removed test with no new test in its file stays
`tests.removed` (block). Replacing `test_parse` with a test that calls `total`, with
a test whose inputs are constants, or with `test_total_in_range` checking
`0 < total() < 1000` where `test_total` checked `total() == 100`, still blocks. A rename that rewrites the test into a
parametrized superset warns.

## 6. Agent and CI adapters

All adapters call the same `greenproof check`. Formats below must be verified against the
vendor's current documentation at implementation time; record the doc URL in a code
comment next to each adapter. If a vendor's format cannot be verified, ship that adapter as
`experimental` (behind `--agents` explicitly) and say so in the README.

- **Claude Code**: `Stop` hook (and `SubagentStop`) → `greenproof check --mode hook --hook claude`.
  On failure write the agent message to stderr and exit 2 (blocks and feeds the reason
  back). `SessionStart` hook → `greenproof session-start`. `init` merges into
  `.claude/settings.json` without clobbering existing keys or hooks; idempotent.
- **Cursor**: `.cursor/hooks.json` `stop` hook → on failure return the documented JSON that
  asks the agent to continue with the message (`followup_message` per current docs).
- **Codex CLI** and **Gemini CLI**: their documented hook files, if verified; else
  experimental.
- **Any agent / human**: git `pre-push` via the pre-commit framework
  (`.pre-commit-hooks.yaml` in this repo exposing `id: greenproof`, `stages: [pre-push]`).
  `init` never changes `core.hooksPath` or git config.
- **GitHub Actions**: composite `action.yml` at the repo root (inputs: `base`,
  `checks`, `allow-policy-change`, `version`); it fetches the base ref, runs
  `uvx --from <action path> greenproof check --mode ci --github-annotations --markdown
  "$GITHUB_STEP_SUMMARY" --json greenproof.json`, uploads `greenproof.json` as an artifact,
  and exposes `exit-code`. `init --ci github` writes `.github/workflows/greenproof.yml`
  using it, with actions pinned to SHAs.

**Agent message (hook mode)** — short, because it lands in the agent's context:

```
greenproof: not done.
  BLOCK tests.skipped  tests/test_api.py:42  @pytest.mark.skip added to test_refund_rounding
  FAIL  test           uv run pytest -q (exit 1) — last lines:
        FAILED tests/test_api.py::test_refund_total - assert 99 == 100
Fix the code, not the check. Do not delete, skip or weaken tests, or edit CI or test
configuration to get green. If the requirement itself changed, stop and tell the user
which test and why.
```

Output tails are capped at 40 lines in hook mode and 200 lines in reports.

## 7. `init`

1. Detect stacks present (multiple allowed): Python (`pyproject.toml`, `setup.cfg`,
   `requirements*.txt`; runner uv/poetry/pip by lockfile), Node (`package.json`; package
   manager by lockfile: pnpm/yarn/bun/npm), Go (`go.mod`), Rust (`Cargo.toml`), plus
   `Makefile`/`justfile` targets named `test`, `lint`, `check`, `typecheck`.
2. Propose `[commands]` from what exists (only real scripts/targets/tools; never invent a
   tool the repo does not use). Unless `--no-verify`, run each proposed command once with
   the timeout and record pass/fail; a command that fails at adoption is kept but reported
   ("the suite is red before greenproof; fix or remove before relying on the gate").
3. Write `.greenproof.toml` (refuse to overwrite without `--yes`).
4. Write or update a `## Verify` section in `AGENTS.md` between
   `<!-- greenproof:begin -->` / `<!-- greenproof:end -->` markers listing the commands and
   the rule "run `greenproof check` before saying you are done". If `CLAUDE.md` exists,
   add an `@AGENTS.md` import line inside the same markers (Claude Code only reads
   AGENTS.md when there is no CLAUDE.md).
5. Install hooks for detected agents (`.claude/`, `.cursor/`, `.codex/`, `.gemini/`
   present, or explicitly listed), and the CI workflow if `--ci github`.
6. Print a summary of every file written or changed. Re-running `init` is idempotent.

## 8. Commands, revert and smoke

- **commands**: run each configured command with `sh -c` in the candidate tree (in place by
  default; inside the isolated worktree with `--isolate`, after `setup`). The environment
  is inherited unchanged except for `GREENPROOF=1`. Kill the whole process group on timeout.
  Record exit code, duration, timed-out flag, output tail.
- **revert** (`revert.passes_without_change`, default warn; block in CI if configured):
  only when the change touches both test files and non-test source. Build a worktree from
  the **base tree with the candidate's test files overlaid**, run `setup`, then run only
  the changed/added tests. If every changed/added test passes without the source change,
  the tests do not exercise it. Supported runners in v0.1: pytest (node ids), vitest/jest
  (file paths), `go test -run`. Other runners: skip with an info note, never fail.
- **smoke** (`smoke.failed`, block): start `smoke.start` in its own process group, poll
  `ready_url` (stdlib `urllib`) until 2xx/3xx or timeout, run each probe, then terminate
  the group (SIGTERM, then SIGKILL after 5 s). Any failure → exit 3 with the probe output.

## 9. Reports

- Text (default, stdout): one line per finding/command, then the verdict.
- JSON (`--json`): `{"schema": "greenproof/report@1", "version", "mode", "base": {"ref",
  "sha"}, "candidate": {"tree", "dirty"}, "policy": {"source", "sha256"}, "checks":
  [{"id", "status", "duration_ms"}], "findings": [{"rule", "severity", "file", "line",
  "message", "hint"}], "commands": [{"name", "cmd", "exit_code", "duration_ms",
  "timed_out", "output_tail"}], "verdict": "pass|fail|error", "exit_code", "cached",
  "error"?}`. Check status is `pass|fail|skipped`. In an `error` report (exit 4 or 5,
  still written when `--json` is given) `base`, `candidate` and `policy` may be null and
  `error` carries the message. Ship a JSON Schema file and test the output against it.
- Markdown (`--markdown`): table of findings and commands for the job summary.
- GitHub annotations (`--github-annotations`): `::error file=…,line=…,title=<rule>::msg`
  for block, `::warning` for warn.

## 10. Repository layout and engineering rules

```
src/greenproof/
  cli.py            argparse entry point, exit codes
  gitops.py         repo discovery, snapshot tree, base resolution, worktrees, diff
  policy.py         load/validate .greenproof.toml (base vs candidate)
  changeset.py      changed files with both-side contents, test-file classification
  rules/            one module per rule family: tests_python.py, tests_lines.py,
                    suppress.py, runner.py, ci.py, hardcoded.py, policy.py
  runner.py         command execution, timeouts, process groups
  revert.py, smoke.py
  report.py         text/json/markdown/annotations, verdict and exit code
  cache.py
  init/             detect.py, write_policy.py, agents_md.py, hooks/{claude,cursor,codex,gemini}.py, ci_github.py
  schema/report-v1.json
tests/              pytest; fixture repos are built in tmp dirs by helpers (real git)
bench/              testwashing benchmark (milestone 4)
```

- Python ≥ 3.11, zero runtime dependencies. Dev: pytest, pytest-cov, ruff, mypy (strict).
- Conventions copy `~/personal_projects/evalproof`: hatchling, `src/` layout, Apache-2.0
  with NOTICE, ruff line length 100 with the same rule selection, mypy strict, coverage
  floor 85%, `[tool.uv] exclude-newer = "3 days"`, CI jobs lint/test/package, actions
  pinned to full SHAs with the release in a comment, zizmor audit, CHANGELOG (Keep a
  Changelog), CONTRIBUTING, SECURITY, CODE_OF_CONDUCT, CITATION.cff.
- CI matrix: Python 3.11–3.14 on ubuntu-latest and macos-latest.
- Every rule: fixtures for a true positive and a must-not-fire legitimate change.
- Tests use real git in temp directories (no mocking of git).
- Commits: small, imperative subject, no `Co-Authored-By` or other AI attribution trailers.
  Work on a branch per milestone; do not push; do not create remote repos; do not publish.

## 11. Milestones

- **M1 — engine and static rules (Python first).** Scaffold; gitops (snapshot, base
  resolution incl. session marker, diff, worktrees with guaranteed cleanup); policy from
  base; all static rules in section 5 (Python AST + line-level for JS/TS/Go/Rust);
  commands runner; cache; text + JSON report with schema; exit codes; `check` and
  `explain`. Done when: `uv run pytest` green with ≥ 85% coverage, ruff + mypy strict
  clean, and the M1 acceptance scenarios below pass.
- **M2 — adoption.** `init` (detection, verification run, policy, AGENTS.md/CLAUDE.md,
  hooks for Claude Code and Cursor verified against current docs, Codex/Gemini verified or
  experimental, CI workflow), `session-start`, hook modes, Markdown + annotations,
  `action.yml`, `.pre-commit-hooks.yaml`. Done when: `init` on fixture repos for each stack
  produces working config, and a scripted Claude Code Stop-hook payload is blocked/passed
  correctly.
  **Accepting a finding.** A legitimate change (dropping a test for removed behaviour, a
  platform skip) needs an acceptance an agent cannot forge. Locally, an agent shares the
  user's shell, so acceptance there is advisory. `greenproof accept <fingerprint>` records
  it under the git dir with the time and git identity. Hook messages tell agents never to
  run it, and the report lists every accepted finding. In CI, the authoritative gate,
  acceptance is a PR label applied by a maintainer (`greenproof:accept`, name set in the
  base policy). The action reads it from the event and honours it only when the label was
  applied after the head commit was pushed. The fingerprint is the rule, path and test name
  plus a digest of the finding's evidence, stable across reruns of the same change.
- **M3 — execution depth.** revert check, smoke runner, `code.hardcoded`, `tests.self_mocked`,
  `--isolate`.
- **M4 — benchmark and release prep.** `bench/`: ≥ 30 cases (tamper patterns from the
  typia port, the Skylos testwashing set and ImpossibleBench-style edits, plus ≥ 10 clean
  controls: refactors, legitimate spec changes, added tests; seeded with the 20 review
  probes P1–P12 and C1–C8 from M1); a runner that reports
  detection and false-positive rates for greenproof and, when installed, other tools;
  a replay harness that runs the static rules over the last 200 test-touching commits of
  ≥ 3 real repositories (pallets/click first) and reports the block rate per rule with a
  triaged sample. The M1 baseline on click was 30 of 200 commits blocked, before the M1d
  calibration. MEASUREMENTS.md; README; dogfood `greenproof init` on this repo. Release (GitHub repo,
  PyPI) only on the maintainer's explicit go-ahead.

### M1 acceptance scenarios (each is an automated test)

1. Agent deletes `tests/test_math.py` → exit 1, `tests.deleted`.
2. Agent adds `@pytest.mark.skip` to a failing test → exit 1, `tests.skipped` at the
   decorator line.
3. Agent replaces `assert total == 100` with `assert total`, `assert total > 0` or
   `assert isinstance(total, int)` → exit 1, `tests.weakened`.
4. Agent deletes `test_a` (`assert total() == 100`) and adds `test_b` in the same file
   (`assert total() > 0` and `assert total() < 1000`) → exit 1, `tests.replaced` naming
   both.
5. Agent edits `.greenproof.toml` to set `test = "true"` → exit 1 `policy.changed`, and the
   test command that runs is the base's, which fails → also reported (exit stays 1).
6. Agent adds `--deselect tests/test_api.py::test_x` to `addopts` → `runner.tampered`.
7. Agent adds `continue-on-error: true` to the test step in `.github/workflows/ci.yml` →
   `ci.tampered`.
8. Legitimate refactor (rename a helper, move a test unchanged to another file, add a new
   test) → exit 0, no findings.
9. Code fixed, tests unchanged, suite green → exit 0; second run hits the cache.
10. Test command fails → exit 2 with the output tail; a timeout kills the process group.
11. Untracked new test file with a failing test → included in the snapshot; exit 2.
12. Not a git repo → exit 4; internal exception → exit 5 (fail closed), never 0.
