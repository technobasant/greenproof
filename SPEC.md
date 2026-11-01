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
greenproof init   [--yes] [--agents auto|none|claude,cursor,codex,gemini] [--ci github|none]
                  [--no-verify] [--hook-command CMD]
greenproof check  [--base REF] [--mode local|hook|ci] [--hook claude|cursor|codex|gemini]
                  [--checks static,commands,revert,smoke] [--json PATH] [--markdown PATH]
                  [--github-annotations] [--github-accept] [--allow-policy-change]
                  [--no-cache] [--isolate]
greenproof session-start [--hook claude|cursor|codex|gemini]
greenproof accept FINGERPRINT... [--reason TEXT] | --list | --revoke FINGERPRINT
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
| `tests.expected_changed` | warn | a literal in an assertion's expected side changed in a kept test (same number of expected literals), whether or not non-test code changed. With no non-test code change it is the "edit the test to match what the code returns" pattern, which commands and revert cannot see (the suite passes and revert has no source change to undo); the message says which case it is. Legitimate when the old value was wrong or the spec changed, so warn |
| `suppress.added` | warn | an added line introduces a type/lint/coverage suppression (see 5.5) |
| `runner.tampered` | block | a change narrows or neuters how checks run: test selection, thresholds, strictness, test scripts (see 5.6) |
| `ci.tampered` | block | a CI workflow change removes or neuters a test/lint step, adds `continue-on-error`, `|| true`, `if: false`, path filters excluding tests, or changes the greenproof step itself. The greenproof step is a line, outside a YAML comment, that runs `greenproof check` (any prefix such as `uvx`, `uv run` or a path, any `@version`) or `uses:` the greenproof action; other mentions (`greenproof --version` in a packaging job) do not count. Only those lines count, and a workflow whose base version has none gaining them is adoption, not a change (a new workflow file included); commenting out or deleting an existing greenproof line still fires. A ref-only version bump of the greenproof action (same inputs) is `warn` |
| `static.parse_error` | warn | a changed file in a parsed language (Python, TOML, JSON, INI) does not parse; that file falls back to the line-level checks |
| `tests.self_mocked` | warn | an added or changed test patches (`monkeypatch.setattr`, `mock.patch`, `jest.mock`, `vi.mock`) the same callable its assertions call, so it tests the mock (5.8) |
| `code.hardcoded` | warn | non-test code gains a literal table (≥ 8 entries) or an `if x == <literal>` branch whose literal also appears in added lines of a test file changed in this diff (5.8; Python AST only in v0.1) |

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
  (`assert x == 3 or True`), or a comparison of an expression with itself, also after
  single-assignment locals (assigned exactly once in the test body, before the assertion,
  from any expression, and not used before it in any other way) are inlined:
  `expected = refund(100)` then `assert refund(100) == expected` checks nothing;
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
  compared as shapes after single-assignment locals are inlined (names bound inside the
  test, its locals and parameters, are alpha-renamed; globals, attributes and builtins keep
  their spelling, so `resp.status` and `resp.ok` differ). A **loose check** on `X`
  is any other assertion in which `X` is: the whole test (`assert X`, `assertTrue(X)`); an
  operand of `is not`, `!=`, `<`, `<=`, `>`, `>=`, `not in` (`assert X > 0`,
  `assert 0 < X < 10`, `assertNotEqual`, `assertGreater`, `assertGreaterEqual`,
  `assertLess`, `assertLessEqual`, `assertIsNotNone`); the first argument of `isinstance`,
  `issubclass` or `assertIsInstance`; or wrapped as `type(X)`, `len(X)` or `bool(X)` in
  any comparison (`assert type(X) is int`, `assert len(X) == 3`). Negations count as
  loose checks on what they negate (`assert not X`, `assertFalse(X)`, `assert not X == 1`,
  `assertNotIn`, `assertNotAlmostEqual`); `and`/`or` operands and each link of a chained
  comparison are classified separately. A subject of an exact
  check in the base test that is the subject of no exact check in the candidate test, and
  is the target of a loose check there, is loosened: `tests.weakened` (block), "exact
  check on `<X>` loosened to `<the new assertion's source>`". This includes
  `assert total() == 100` → `assert total()`, → `assert total() > 0`, →
  `assert isinstance(total(), int)` and `assert rows == [a, b]` → `assert len(rows) == 2`.
  Any other comparison on `X` replaced by a truthiness check on `X` alone
  (`assert x < 5` → `assert x`) is also `tests.weakened` (block). When the new assertion
  is a different expression (`assert f(x) == 1` → `assert is_one(x)`) it is a `warn` for a
  strong check replaced by a different one, because that is often a legitimate refactor.
- a check-count drop made only of **loose checks** (every base assertion missing from the
  candidate is a loose check as defined above, or a negation), with every exact-check
  subject of the base test still the subject of an exact check in the candidate, is
  `tests.weakened` **warn** ("only loose checks dropped"), not block. Dropping
  `assert isinstance(stream, Iterable)` while every `==` stays is a cleanup more often
  than a tamper; dropping any exact check still blocks.

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

Line-engine assertion vocabulary also includes: JS/TS `expectTypeOf(`, `assertType(` and
a testing-library query that throws when the element is missing (`getBy…(`,
`getAllBy…(`, and awaited `findBy…(`/`findAllBy…(`, bare or on `screen.`), counted only on
a line with no other assertion call, so `expect(getByText('a')).toBeVisible()` counts once
and `findByText` → `getByText` is not a drop; `queryBy…` returns null and is not counted. Go **assertion helpers**: a
function declared in a `_test.go` file of the same directory (package) that takes a
`*testing.T`, `*testing.B`, `*testing.F` or `testing.TB` parameter and contains an
assertion call, directly or by calling another assertion helper, is an assertion helper.
Each call to one inside a test counts as one assertion, and its exact form (5.7) is the
call line with whitespace removed. `t.Helper()` is not required.

### 5.5 Suppressions (`suppress.added`, warn by default; each pattern its own sub-id in the message)

`# type: ignore`, `# noqa`, `# pragma: no cover`, `# pyright: ignore`, `@ts-ignore`,
`@ts-expect-error`, `@ts-nocheck`, `eslint-disable`, `biome-ignore`, `//nolint`,
`#[allow(`, `as any`, `: any` (TS), `|| true`, `; true$`, `set +e`, `--passWithNoTests`,
`--no-verify` on a `git commit`, `git push`, `git merge`, `git am`, `git rebase` or
`git cherry-pick` line (other tools' `--no-verify` flags are not git's hook bypass).
Matching is on added lines only, ignoring lines whose content already
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

A linter or type-checker configuration that does not exist at the base (a new
`[tool.ruff]`/`[tool.ruff.lint]` or `[tool.mypy]` table, a new `ruff.toml`, `.ruff.toml`,
`mypy.ini` or `.flake8`) is adoption or a migration: its ignores, per-file ignores and
disabled error codes are `runner.tampered` **warn**, not block, because there was no
stricter configuration to loosen. Ignores added to a table that existed at the base
still block.

### 5.7 Test identity and covered removals

Base tests are paired with candidate tests in passes, each pairing a test at most once:
1. same path and qualified name; 2. same path and same **leaf name** (the last segment of
the qualified name: the test function, or the JS/TS test title) when that leaf name is
unique among the still-unpaired tests of that file on both sides (a test moved between
classes or describe blocks, or a describe title edited); 3. same qualified name in
another file; 4. same normalised body hash; 5. same *structural* hash (identifiers
alpha-renamed; constants, operators and assertion shapes kept) when the match is
one-to-one within the change set.

Line engines hash a test with its own name (the Go or Rust function name, the JS/TS
title literal) replaced by a placeholder, so a pure rename pairs in pass 4. Their
structural hash also replaces every identifier token that is not a language keyword or
5.4 assertion vocabulary (`expect`, matcher names, `t.Errorf`, `assert.Equal`, ...) by a
placeholder numbered by first occurrence, keeping string and number literals and
operators. Go `Benchmark*` functions are not tests (`go test` runs them only with
`-bench`) and are never units, so they are neither removed tests nor replacements. Line
engines find test declarations on comment-masked lines: a test inside `/* */` does not
exist.
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
replaces nothing. Renames that also rename identifiers are handled by pass 5, not by
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
- the new tests pin at least as many values as the removed tests. A **pinned check** is
  an exact check whose other side, after inlining single-assignment locals, is a literal
  (a constant, a negated number, a tuple, list, set or dict display of literals, or
  `approx(<literal>)`) or a parametrize argument whose cases are all literals; it counts
  once per parametrize case, like effective checks (5.4). An expected value computed from
  the inputs (`refund(x) == x - 1`) or by calling code is not pinned. A rewrite may change
  the expected values (the requirement may have changed); it may not stop pinning them.

Otherwise it is `block`.

**Tests with no counted check** (all engines). A removed uncovered test whose effective
check count at the base is zero (a type-only test checked by the compiler, a "does not
throw" smoke test) in a file that gains new tests or has absorbing tests is
`tests.replaced` **warn**: it had no counted check to lose. With no new or absorbing test
in its file it stays `tests.removed` (block). A deleted test file that contained no test
units (helpers, fixtures) is not `tests.deleted`; `runner.tampered` still covers conftest
hooks.

**Absorbing tests.** A paired test in the same file whose effective check count (5.4)
grew is an absorbing test: removed tests were often folded into it (two tests merged into
one parametrized test that keeps one of the names). When a file has removed uncovered
tests and absorbing tests, the four rewrite conditions above are evaluated on the removed
tests plus the base versions of the absorbing tests, against the new unpaired tests plus
the candidate versions of the absorbing tests. When they hold, the removed tests are
`tests.replaced` (warn), and the message names the absorbing tests; when they do not, the
removed tests keep the finding they would have without absorbing tests. Example:
`test_refund` (`refund(100) == 99`) and `test_refund_small` (`refund(2) == 1`) folded into
`test_refund` parametrized over `[(100, 99), (2, 1)]` warns; folded into one case
`[(100, 99)]`, or with expected values computed by calling code, still blocks.

A removed test with no new test and no absorbing test in its file stays
`tests.removed` (block). Replacing `test_parse` with a test that calls `total`, with
a test whose inputs are constants, or with `test_total_in_range` checking
`0 < total() < 1000` where `test_total` checked `total() == 100`, still blocks, and so does
replacing `assert refund(100) == 99` with a parametrized `assert refund(x) == x - 1`. A
rename that rewrites the test into a parametrized superset with literal expected values
warns, even when the old test's literal no longer appears.

### 5.8 `tests.self_mocked` and `code.hardcoded`

**`tests.self_mocked`** (warn). It fires only for tests that are added or changed, when the
patch is not in that test at the base. A test patches a callable and one of its assertions
calls that callable, so the assertion checks the mock.

- Python (AST). Patch targets in the test body, its decorators and its `with` items:
  `monkeypatch.setattr(obj, "name", ...)` and `monkeypatch.setattr("pkg.mod.name", ...)`;
  `mock.patch("pkg.mod.name", ...)`, `patch(...)` and `mocker.patch(...)` used as a call, a
  decorator or a context manager; `patch.object(obj, "name", ...)` and
  `mocker.patch.object(obj, "name", ...)`. Each target is a qualifier (the dotted `obj`
  expression, or the string minus its last component) plus a name.
- It fires when an assertion contains a call whose dotted name is the qualifier plus the
  name (`mathx.total(...)`), or is the bare name when the test file imports it with
  `from <qualifier-module> import <name>` (also when the qualifier is that module's alias).
  It still fires when the patch does not reach the from-imported name; an agent that
  patches the subject of its own test intended to.
- JS/TS (line level). An added `jest.mock(P)` or `vi.mock(P)`, or an added
  `jest.spyOn(X, 'n')` / `vi.spyOn(X, 'n')` followed by `.mockReturnValue`,
  `.mockResolvedValue` or `.mockImplementation`, where the same file imports from `P` (or
  `n` from `X`) and an imported name is **called** inside an `expect(` argument
  (`expect(total([1, 2]))`, `expect(X.n(...))`). `expect(sendMail).toHaveBeenCalled()`
  asserts on the mock of a collaborator, which is what mocks are for, and does not fire.
- Message: "test <name> patches <target> and asserts on <call>; it tests the mock".

**`code.hardcoded`** (warn, Python AST only). It only looks at literals that also appear in
**added lines of test files changed in this diff**, after this filter: strings of three or
more characters, and numbers other than 0, 1 and -1 (other literals never count). In
non-test Python files changed in the diff, it fires when the candidate gains, compared with
the same function or module at the base:

1. a literal dict, list, tuple or set display with eight or more constant entries, at
   least one of which is a counted test literal; or
2. a branch that compares a name, attribute or subscript with a counted test literal
   (`x == L`, `L == x`, `x in (L, ...)`, `case L:`) and whose body returns or assigns a
   literal that is also a counted test literal: both the input and the answer come from
   the tests (`if x == 250: return 240` with a test asserting `refund(250) == 240`). A new
   branch whose value the tests do not mention (`if mode == "fast": t = 5` tested only
   by `timeout_for("fast") < timeout_for("slow")`) does not fire.

Message: "<path>: <function> gained <a table | a branch on L> whose value also appears in
<test file>; it may special-case the test input". New modules count as gaining everything
in them.

## 6. Agent and CI adapters

All adapters call the same `greenproof check`. Each adapter module names the vendor
document it was verified against (fetched 2026-11-01) in a comment. An adapter whose format
cannot be verified ships as `experimental`: `init` installs it only when it is named in
`--agents`, and the README says so.

- Claude Code: https://code.claude.com/docs/en/hooks.md
- Cursor: https://cursor.com/docs/agent/hooks
- Codex CLI: https://developers.openai.com/codex/hooks, schemas in
  `openai/codex` under `codex-rs/hooks/schema/generated`
- Gemini CLI: https://geminicli.com/docs/hooks/reference/

### 6.1 Hook protocol (`check --hook AGENT`)

`--hook AGENT` implies `--mode hook`; `--hook` with `--mode local` or `--mode ci` → exit 4.
greenproof reads the hook payload from stdin: a JSON object of at most 1 MiB. A TTY, empty
or unparseable stdin counts as `{}` and the gate still runs. From the payload it uses:

| agent | session id | start directory | loop fields |
|---|---|---|---|
| claude | `session_id` | `cwd` | `stop_hook_active` (unused, see 6.2) |
| cursor | `conversation_id` | `workspace_roots[0]` | `status`, `loop_count` |
| codex | `session_id` | `cwd` | `stop_hook_active` (unused) |
| gemini | `session_id` | `cwd` | `stop_hook_active` (unused) |

The start directory, when it exists, replaces the process cwd for repo discovery. In hook
mode stdout carries only the protocol output below, never the text report; `--json` and
`--markdown` files are still written. The outcomes:

| outcome | claude `Stop` | cursor `stop` | codex `Stop` | gemini `AfterAgent` |
|---|---|---|---|---|
| pass | exit 0, no output | exit 0, `{}` | exit 0, no output | exit 0, no output |
| block | exit 2, agent message on stderr | exit 0, `{"followup_message": msg}` | exit 0, `{"decision": "block", "reason": msg}` | exit 0, `{"decision": "deny", "reason": msg}` |
| release | exit 0, `{"continue": false, "stopReason": note}` | exit 0, `{}` | exit 0, `{"continue": false, "stopReason": note}` | exit 0, `{"continue": false, "stopReason": note}` |

A Cursor payload whose `status` is not `completed` (the user aborted, or the turn errored)
→ exit 0, `{}`, nothing runs. An error inside the check (what would be exit 4 or 5) is a
failure for the rules in 6.2, with the message "greenproof could not run: <error>. Tell the
user; do not edit greenproof's configuration to get past it." Fail closed, but never trap
the agent.

**Agent message** (block) — short, because it lands in the agent's context:

```
greenproof: not done.
  BLOCK tests.skipped  tests/test_api.py:42  @pytest.mark.skip added to test_refund_rounding
  FAIL  test           uv run pytest -q (exit 1) — last lines:
        FAILED tests/test_api.py::test_refund_total - assert 99 == 100
Fix the code, not the check. Do not delete, skip or weaken tests, or edit CI or test
configuration to get green. If the requirement itself changed, stop and tell the user
which test and why. Never run `greenproof accept`; accepting a finding is the user's call.
```

Output tails are capped at 40 lines in hook mode and 200 lines in reports. The **release
note** is for the user: "greenproof: the agent stopped with the gate failing", the first
five findings or failed commands, and "run `greenproof check` for the full report".

### 6.2 Blocking without trapping the agent

The gate cannot force an agent to keep working, and an agent that cannot fix something (a
changed requirement, a suite that was red before it started) must be able to stop and tell
the user. State lives in `<git-dir>/greenproof/hook-state.json` (per worktree): session id,
candidate tree, consecutive block count and the last release note. Missing or malformed
state counts as empty. In order:

1. The candidate tree equals the base tree (nothing changed since the base) → pass without
   running anything; the report notes "nothing to verify". A question-only turn never runs
   the suite.
2. Same session and same candidate tree as the last block → **release** without running
   again: the agent was told and chose to stop without changing anything.
3. Otherwise run the check. Pass → clear the state, pass. Failure → block and record the
   tree, unless this would be the 8th consecutive block in the session, which releases.
   A release starts the count again and keeps the tree, so the same tree stays released
   and later work in the session is gated again.

Outside a git repository there is nowhere to keep state, so a hook releases with the
"could not run" note instead of blocking.

Local acceptances apply in hook mode (6.6). Cursor's own `loop_limit` (default 5) and
Claude Code's cap of 8 consecutive stop-hook continuations still apply on top. All of this
is advisory: the agent shares the user's shell and can edit the state file or the session
marker. CI (6.5) is the authoritative gate.

### 6.3 `session-start --hook AGENT`

Writes the session marker and gives the agent its instructions:

- The payload's `source` is `resume`, `compact` or `fork` → keep the existing marker while it
  is still valid (an ancestor of HEAD); otherwise (`startup`, `clear`, absent, cursor) write a
  new one.
- Context, at most six lines: greenproof is the done gate here; the commands it runs (from
  the policy at HEAD, when there is one); that it inspects the diff for deleted, skipped or
  weakened tests and edits to test, runner or CI configuration; "Fix the code, not the
  check. Never run `greenproof accept`; accepting a finding is the user's call."
- Output: claude, codex and gemini `{"hookSpecificOutput": {"hookEventName":
  "SessionStart", "additionalContext": text}}`; cursor `{"additional_context": text}`.
- Never blocks: with `--hook`, any error → exit 0, nothing on stdout, one line on stderr.
  Without `--hook`, errors use the normal exit codes.

### 6.4 Hook configuration written by `init`

The hook command is `greenproof` when it is on PATH at init time, else
`uvx greenproof@<version>`; `--hook-command CMD` overrides it (for example
`uv run greenproof`). Hook timeout = policy timeout × runnable commands + 60 seconds, at
most 3600. An entry is greenproof's when its command matches
`greenproof(@\S+)?\s+(check\s+--hook|session-start)`, so `uvx greenproof@0.1.0 check --hook
claude` and `uv run greenproof session-start` are recognised; a `--hook-command` that does
not contain `greenproof` (a wrapper script) is not, and re-running `init` adds a second
entry beside it. Re-running `init` replaces greenproof's entries and leaves every other key
and hook untouched. An existing file that is not UTF-8, not valid JSON, not a JSON object,
or whose `hooks` (or `hooks.<event>`) has the wrong type → exit 4, nothing written. JSON is
written with two-space indent and a trailing newline.

- **Claude Code**: `.claude/settings.json`, the committed project file the team shares:

  ```json
  {"hooks": {
    "Stop": [{"hooks": [{"type": "command", "command": "greenproof check --hook claude", "timeout": 960}]}],
    "SessionStart": [{"matcher": "startup|resume|clear|compact",
                      "hooks": [{"type": "command", "command": "greenproof session-start --hook claude"}]}]}}
  ```

  No `SubagentStop`: a subagent stopping midway through the parent's task would be blocked
  for the parent's unfinished work. The parent's `Stop` is the gate.
- **Cursor**: `.cursor/hooks.json`:
  `{"version": 1, "hooks": {"stop": [{"command": "greenproof check --hook cursor", "timeout": 960}], "sessionStart": [{"command": "greenproof session-start --hook cursor"}]}}`.
  No `subagentStop`, for the same reason.
- **Codex CLI**: `.codex/hooks.json`, verified against `codex-rs/config/src/hook_config.rs`
  (`HooksFile`) and `codex-rs/protocol/src/hooks.rs`: top-level keys `description` and
  `hooks` only (unknown keys are rejected), events nested like Claude's (`Stop`;
  `SessionStart` with matcher `startup|resume|clear|compact`), handler
  `{"type": "command", "command": ..., "timeout": seconds}`. `init` prints: "Codex: review
  and trust the new hooks once with /hooks; Codex skips untrusted project hooks."
- **Gemini CLI**: experimental. Gemini CLI was superseded by Antigravity CLI on 2026-06-18,
  and the open issue google-gemini/gemini-cli#27712 reports `AfterAgent` hooks not firing.
  `.gemini/settings.json`: `AfterAgent` (matcher `*`, `"name": "greenproof"`, timeout in
  **milliseconds**) and `SessionStart` (matcher `startup|resume|clear`).

Every agent treats a hook that cannot start as a non-blocking error, so hooks fail open
when the greenproof executable is missing. The README says so; CI is the backstop.

### 6.5 CI

- **pre-commit framework**: `.pre-commit-hooks.yaml` exposes `id: greenproof`,
  `entry: greenproof check`, `language: python`, `pass_filenames: false`,
  `always_run: true`, `stages: [pre-push]`. `init` never changes `core.hooksPath` or git
  config.
- **GitHub Action**: a composite `action.yml` at the repo root. Inputs: `base`, `checks`,
  `allow-policy-change` (default `false`), `accept-comments` (default `true`), `token`
  (default `${{ github.token }}`). Output: `exit-code`. Steps: setup-uv; when the checkout
  is shallow, `git fetch --no-tags --unshallow origin` and fetch the base branch; run
  `uvx --from "$GITHUB_ACTION_PATH" greenproof check --mode ci --github-annotations
  --markdown "$GITHUB_STEP_SUMMARY" --json greenproof.json`, plus `--github-accept` when
  `accept-comments` is true; upload `greenproof.json` with upload-artifact (`if: always()`);
  exit with greenproof's code. Inputs reach `run:` only through `env:`, never by `${{ }}`
  interpolation.
- **Workflow from `init --ci github`**: `.github/workflows/greenproof.yml`, independent of
  the action so that it needs no release SHA of greenproof. `on: pull_request`;
  `permissions: {contents: read, pull-requests: read}`; checkout with `fetch-depth: 0` and
  `persist-credentials: false`; toolchain steps for the detected stacks (section 7);
  setup-uv; `uvx greenproof@<version> check --mode ci --github-annotations --github-accept
  --markdown "$GITHUB_STEP_SUMMARY" --json greenproof.json` with `GITHUB_TOKEN` in `env`;
  upload-artifact with `if: always()`. An existing workflow file is kept unless `--yes`.
- Every third-party action is pinned to a full commit SHA with the release tag in a
  comment. The pins live in one module, `pins.py`, and a test keeps `action.yml` in step
  with it.

### 6.6 Accepting a finding

A legitimate change (dropping the test of a removed feature, a platform skip) needs a way
through that an agent cannot forge.

- Every finding carries a `fingerprint`: the first 12 hex digits of sha256 over rule, path
  and message, with line references (`line N`, `:N`) removed from the message. Messages
  name the test and the evidence, so a fingerprint survives reruns and unrelated edits, and
  a different tamper gets a different one.
- **Locally (advisory).** `greenproof accept FINGERPRINT... [--reason TEXT]` records each in
  `<git-common-dir>/greenproof/accepted.json` with the time and `git config user.name` /
  `user.email`; `accept --list` prints them and `accept --revoke FINGERPRINT` removes one.
  Local and hook modes honour these; ci mode ignores them. Agent messages say never to run
  it, and every report lists accepted findings with who accepted them and when.
- **In CI (authoritative).** With `--github-accept` in ci mode on a `pull_request` or
  `pull_request_target` event, greenproof reads the PR's issue comments (`GITHUB_API_URL`,
  `GITHUB_REPOSITORY`, the PR number from `GITHUB_EVENT_PATH`, `GITHUB_TOKEN`; stdlib
  `urllib`, 10-second timeout, following `Link` pagination). A line
  `/greenproof accept FP [FP ...]` in a comment whose `author_association` is `OWNER`,
  `MEMBER` or `COLLABORATOR` and whose `user.type` is `User` accepts those fingerprints.
  Bots and outside contributors cannot accept. Any API failure → no acceptances and a note
  in the report (fail closed). When a finding blocks, the job summary shows the exact
  comment to post and says to re-run the job. The workflow does not trigger on comments:
  an `issue_comment` workflow runs with base-branch privileges, a known injection path.
- An accepted finding keeps its severity, is marked accepted and does not affect the exit
  code. It shows as `ACCEPTED` in text, in its own Markdown table and as a `::notice`
  annotation.

## 7. `init`

`greenproof init [--yes] [--agents auto|none|LIST] [--ci github|none] [--no-verify]
[--hook-command CMD]`. It never prompts, so agents and CI can run it.

1. **Detect** stacks at the repository root (several may apply):
   - Python: `pyproject.toml`, `setup.py`, `setup.cfg` or `requirements*.txt`. Runner
     prefix: `uv.lock` → `uv run `, `poetry.lock` → `poetry run `, else none and tools run
     as `python -m <tool>`.
   - Node: `package.json`. Package manager by lockfile: `pnpm-lock.yaml` pnpm, `yarn.lock`
     yarn, `bun.lock` or `bun.lockb` bun, else npm.
   - Go: `go.mod`. Rust: `Cargo.toml`.
   - Targets `test`, `lint`, `check`, `typecheck` in `Makefile`, `GNUmakefile` or `justfile`
     (a rule line starting at column 0: `name:` for make; `name:` or `name args...:` for
     just).
2. **Propose `[commands]`** from evidence only; never a tool the repo does not use:
   - `test`: a make/just `test` target; else pytest (evidence: `[tool.pytest.ini_options]`,
     `pytest.ini`, a `conftest.py`, or `pytest` named in pyproject dependencies,
     optional-dependencies or dependency-groups, or in `requirements*.txt`) →
     `<prefix>pytest -q` (`python -m pytest -q` without a prefix); Node `scripts.test`,
     unless it is npm's placeholder (`echo "Error: no test specified" && exit 1`) →
     `npm test`, `pnpm test`, `yarn test` or `bun run test`; Go → `go test ./...`;
     Rust → `cargo test`.
   - `lint`: a make/just `lint` target, else a `check` target; else ruff (`[tool.ruff]`,
     `ruff.toml`, `.ruff.toml` or named in dependencies) → `<prefix>ruff check .`; Node
     `scripts.lint` → `<pm> run lint`; Go: golangci config (`.golangci.yml`, `.yaml` or
     `.toml`) → `golangci-lint run`, else `go vet ./...`; Rust with `clippy.toml` or
     `.clippy.toml` → `cargo clippy --all-targets`.
   - `typecheck`: a make/just `typecheck` target; else mypy (`[tool.mypy]`, `mypy.ini`,
     `.mypy.ini` or named in dependencies) → `<prefix>mypy .`; pyright (`[tool.pyright]` or
     `pyrightconfig.json`) → `<prefix>pyright`; Node `scripts.typecheck` or
     `scripts["type-check"]` → `<pm> run <name>`.
   - `build`: Node `scripts.build` → `<pm> run build`. Nothing else in v0.1.
   - `setup` (section 8): `uv.lock` → `uv sync --frozen`; `poetry.lock` →
     `poetry install`; `requirements.txt` → `python -m pip install -r requirements.txt`;
     npm with a lockfile → `npm ci`; pnpm → `pnpm install --frozen-lockfile`; yarn →
     `yarn install --immutable` when `.yarnrc.yml` exists, else
     `yarn install --frozen-lockfile`; bun → `bun install --frozen-lockfile`; Go →
     `go mod download`; Rust → `cargo fetch`.
   - When several stacks fill one slot, the commands are joined with ` && ` in the order
     Python, Node, Go, Rust. A make/just target replaces the language-derived command for
     its slot.
   - Nothing detected → a policy with empty commands and a summary line saying so; the
     static checks still work.
3. **Verify**, unless `--no-verify`: run each proposed command except `setup` once, in place,
   with the timeout, and record pass, fail or timeout. A red command is kept and reported:
   "the suite is red before greenproof; fix it or remove it before relying on the gate".
4. **Write `.greenproof.toml`**: `version = 1`; the commands, each with a comment saying
   where it came from; `timeout = 900`; `[modes.hook]` and `[modes.local]` checks
   `["static", "commands"]`, `[modes.ci]` checks `["static", "commands", "revert"]`. init
   never writes a `[smoke]` table, because it cannot know how to start the application;
   the user adds `[smoke]` and `"smoke"` in `[modes.ci]` by hand (8.2). An existing
   policy is kept (and used for the rest of init) unless `--yes`, which rewrites the whole
   file: hand-written tables such as `[rules]` and `[smoke]` are not carried over.
5. **AGENTS.md**: a section between `<!-- greenproof:begin -->` and `<!-- greenproof:end -->`
   with the heading `## Verify`, the commands, "Before you say a task is done, run
   `greenproof check`." (with `--hook-command C`, "run `C check`"), the fix-the-code rule and "Never run `greenproof accept`". Create
   the file when it is missing, append the section when there are no markers, replace
   between the markers otherwise. A begin marker without an end marker (or the reverse) →
   exit 4, nothing written; the user fixes the markers by hand. **CLAUDE.md**: when it exists and has no `@AGENTS.md`
   line, add one inside its own marker pair at the end (so Claude Code, which
   reads CLAUDE.md, sees the section too). Never create CLAUDE.md.
6. **Hooks** (6.4). `--agents auto` (the default) installs claude when `.claude/` or
   `CLAUDE.md` exists, cursor when `.cursor/` exists, codex when `.codex/` exists; never
   gemini. `none` installs nothing. A list installs
   exactly those, experimental ones with a warning.
7. **CI** (`--ci github`; default `none`, and then the summary suggests `--ci github` when
   `.github/workflows/` exists). Toolchain steps by stack: uv → setup-uv only (uv installs
   Python); other Python → actions/setup-python (`python-version-file: .python-version`
   when present, else `python-version: "3.x"`); Node → actions/setup-node
   (`node-version-file` from `.nvmrc` or `.node-version` when present, else
   `node-version: lts/*`) plus `corepack enable` for pnpm and yarn; bun →
   oven-sh/setup-bun; Go → actions/setup-go (`go-version-file: go.mod`); Rust → nothing
   (the runner image ships rustup). Dependencies install through `setup`, which greenproof
   runs in ci mode, so the workflow has no install step of its own.
8. **Summary**: each file `created`, `updated`, `unchanged` (the new content equals the file)
   or `kept` (an existing policy or workflow left alone without `--yes`), the verification
   results and next steps. Exit 0 even when a command was red; 4 outside a git repository or
   on an existing file it cannot safely edit; 5 on an internal error. Every file is planned
   before any is written, so an exit 4 leaves the repository as it was.

`init` works in a repository without commits (the summary says `check` needs one) and never
commits.

## 8. Commands, revert and smoke

- **commands**: run each configured command with `sh -c` in the candidate tree (in place by
  default; inside the isolated worktree with `--isolate`). The environment is inherited
  unchanged except for `GREENPROOF=1`. Kill the whole process group on timeout. Record exit
  code, duration, timed-out flag, output tail. `setup` runs first in ci mode (a fresh
  checkout) and inside isolated worktrees, never in local or hook mode, where the
  developer's environment is already set up. When `setup` fails, the remaining commands do
  not run and the check fails with exit 2.
- **revert** (`revert.passes_without_change`, default warn; block in CI if configured):
  only when the change touches both test files and non-test source. Build a worktree from
  the **base tree with the candidate's test files overlaid**, run `setup`, then run only
  the changed/added tests. If every changed/added test passes without the source change,
  the tests do not exercise it. Supported runners in v0.1: pytest (node ids), vitest/jest
  (file paths), `go test -run`. Other runners: skip with an info note, never fail.
- **smoke** (`smoke.failed`, block): start `smoke.start` in its own process group, poll
  `ready_url` (stdlib `urllib`) until 2xx/3xx or timeout, run each probe, then terminate
  the group (SIGTERM, then SIGKILL after 5 s). Any failure → exit 3 with the probe output.

### 8.1 Revert check details

- It runs when the change adds or modifies test files and also modifies non-test code
  (files with a `CODE_SUFFIXES` suffix outside the test globs). Otherwise it reports
  `skipped` with a note.
- The tree it checks is the base tree with every added or modified test file overlaid at
  its candidate version. Build it in a temporary index (`read-tree <base>`, then
  `update-index --add --cacheinfo` per file, then `write-tree`). Check it out with
  `worktree_for_tree`, which is always removed. Run `setup`, then only the selected tests:
  - **pytest**, when `commands.test` contains `pytest`: append the node ids of the tests
    that were added or whose body changed (`path::Class::name`, parametrized ids dropped).
  - **jest or vitest**, when `commands.test` contains `jest` or `vitest`, or is
    `<pm> test` / `<pm> run test` and that script in `package.json` contains them: append
    the changed test file paths, after `--` for package-manager scripts.
  - **go**, when `commands.test` contains `go test`: `go test <package dirs of the changed
    test files> -run '^(TestA|TestB)$'`.
  - Any other runner, a test command that is a make or just target, or nothing to select:
    `skipped` with a note, never a failure.
- Verdict, from the runner's exit code: 0 (every selected test passes without the source
  change) → `revert.passes_without_change` (default warn; a policy may set block): "the
  added or changed tests pass on the base code; they may not exercise the change". Exit 1
  (some fail without the change) → pass. Anything else (pytest 2–5 such as "no tests
  collected", a timeout, a setup failure) → `skipped` with a note, because it says nothing
  about the tests. A pytest collection error gets the note "the changed tests do not
  collect on the base code; they likely import what the change adds".
- It obeys the policy timeout and kills the process group.

### 8.2 Smoke and isolate details

- smoke runs when `[smoke]` has `start`, `ready_url` and at least one probe; otherwise it
  is `skipped` with a note. It runs in place, or in the isolated worktree with `--isolate`.
  Poll every 0.5 s with a 2-second timeout per request; the overall `smoke.timeout`
  (default 60) covers start-up and the probes. A start process that exits before ready →
  `smoke.failed` with its output tail. The group is always terminated (SIGTERM, then
  SIGKILL after 5 s), also on SIGINT/SIGTERM.
- `--isolate`: `commands` (after `setup`) and smoke run in a worktree of the candidate
  tree; static analysis is unchanged. `--isolate` is a verdict-changing flag in the cache
  key.
- `IMPLEMENTED_CHECKS` becomes all four. `init` writes `[modes.ci] checks = ["static",
  "commands", "revert"]`, plus `"smoke"` when the policy has a `[smoke]` table.

## 9. Reports

- Text (default, stdout): one line per finding/command, then the verdict.
- JSON (`--json`): `{"schema": "greenproof/report@1", "version", "mode", "base": {"ref",
  "sha"}, "candidate": {"tree", "dirty"}, "policy": {"source", "sha256"}, "checks":
  [{"id", "status", "duration_ms"}], "findings": [{"rule", "severity", "file", "line",
  "message", "hint", "fingerprint", "accepted"}], "commands": [{"name", "cmd",
  "exit_code", "duration_ms", "timed_out", "output_tail"}], "notes": [str], "verdict":
  "pass|fail|error", "exit_code", "cached", "error"?}`. `accepted` is null or
  `{"by", "at", "source": "local|github", "reason"}`; `notes` carries lines such as
  "nothing to verify" or "could not read PR comments: ...". Check status is `pass|fail|skipped`. In an `error` report (exit 4 or 5,
  still written when `--json` is given) `base`, `candidate` and `policy` may be null and
  `error` carries the message. Ship a JSON Schema file and test the output against it.
- Text shows each finding's fingerprint (`[fp 3f9a1c2e01ab]`) so a user can accept it.
- Markdown (`--markdown`): verdict; findings table with fingerprints; accepted findings in
  their own table; commands table; output tails of failed commands in `<details>`; when a
  finding blocks on a PR, the comment a maintainer can post to accept it (6.6).
- GitHub annotations (`--github-annotations`): `::error file=…,line=…,title=<rule>::msg`
  for block, `::warning` for warn, `::notice` for accepted.

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
  hooks.py          hook payloads, per-agent output protocol, hook state and release rules
  accept.py, github.py   local acceptances, PR comment acceptances
  pins.py           SHA pins for third-party actions
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
- **M2 — adoption**, in two parts built in parallel on separate branches:
  - **M2a runtime**: the hook protocol for the four agents and the release rules (6.1–6.3),
    fingerprints, `accept` and `--github-accept` (6.6), `setup` in ci mode (section 8),
    the report changes (section 9), `.pre-commit-hooks.yaml`. Done when scripted payload
    tests for each agent cover pass, block, release on an unchanged tree, the 8-block cap,
    nothing to verify, a Cursor payload that is not `completed` and an internal error; and
    the PR comment reader is tested against recorded API JSON (no network in tests),
    including a bot, an outside contributor, pagination and an API failure.
  - **M2b init**: section 7, the hook installers (6.4), the workflow writer, `pins.py` and
    `action.yml` (6.5). Done when `init` on fixture repos (Python with uv, Python with pip,
    Node with npm, Node with pnpm, Go, Rust, and a mixed repo) produces the expected policy,
    AGENTS.md, hooks and workflow; a second run changes nothing byte for byte; an existing
    hook file keeps its other hooks; and the generated workflow and `action.yml` parse as
    YAML in tests (PyYAML as a dev dependency only) and pass `actionlint` and `zizmor` when
    they are installed.
- **M3 — execution depth.** revert check, smoke runner, `code.hardcoded`, `tests.self_mocked`,
  `--isolate` (5.8, 8.1, 8.2). Done when: each form in 5.8 has a firing and a non-firing
  test, and probe P12 (a test that monkeypatches the function it asserts on) warns
  `tests.self_mocked`; the revert check on fixture repos covers passes-without-change
  (warn, and exit 1 when the policy sets block), fails-without-change (pass), no source
  change, an unsupported runner, nothing collected and a timeout (each `skipped` with its
  note), with pytest run for real and the jest, vitest and go command lines unit-tested
  (run for real only when the tool is installed); smoke covers ready plus passing probes,
  a failing probe (exit 3), a start process that exits early, a ready timeout, and leaves
  no process behind; `--isolate` runs `setup` and the commands in a worktree that is
  removed afterwards, also after a timeout and on SIGINT; the M1 and M2 gates stay green.
- **M4 — benchmark and release prep** (sections 12 and 13). `bench/` with at least 45
  cases, the runner and its rot test; the replay harness run on pallets/click,
  encode/httpx, pmndrs/zustand and spf13/cobra with every blocked commit triaged;
  MEASUREMENTS.md; pytest-xdist; dogfood `greenproof init` on this repository and a
  `greenproof` CI job; README.md; the CHANGELOG `0.1.0` entry. The M1 baseline on click
  was 30 of 200 commits blocked, before the M1d calibration. Done when `bench/run.py`
  meets every case, the replay numbers and triage are in MEASUREMENTS.md, and the gates
  stay green. Release (GitHub repo, PyPI) only on the maintainer's explicit go-ahead.

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

## 12. Benchmark and replay (M4)

**`bench/`** is dev tooling in the repository, not part of the wheel. It may import
greenproof and use the dev dependencies; it never needs network access.

- `bench/fixtures/<name>/`: small base repositories, at least `python-shop` (src/shop.py,
  tests with pytest, `.greenproof.toml` whose `test` is `python -m pytest -q -p
  no:cacheprovider`, resolved to the running interpreter at build time), `js-cart`
  (package.json, src/cart.js, test/cart.test.js, empty commands) and `go-calc` (go.mod,
  calc.go, calc_test.go, empty commands).
- `bench/cases/<id>.toml`, one case per file:

  ```toml
  id = "P2"
  fixture = "python-shop"
  title = "an exact assertion replaced by a truthiness check"
  source = "review probe"      # review probe | typia port | Skylos testwashing | ImpossibleBench | control
  kind = "tamper"              # tamper | control
  expect = "block"             # block | warn | pass
  rules = ["tests.weakened"]   # rule ids that must be reported (empty for pass)
  checks = "static"            # default; "static,commands" or "static,commands,revert" when needed

  [[edit]]                     # applied in order: replace (exact, must match once), write, delete
  path = "tests/test_shop.py"
  replace = { old = "assert refund(100) == 99", new = "assert refund(100)" }
  ```

- `bench/run.py [--cases GLOB] [--json PATH]`: for each case build the fixture in a temp
  git repo (commit as the base), apply the edits uncommitted, run `greenproof check --mode
  local --no-cache --checks <checks> --json <tmp>`, and compare. A case **meets** its
  expectation when: `block` → exit 1 and every listed rule reported at block; `warn` →
  exit 0 and every listed rule reported at warn; `pass` → exit 0 and no block finding.
  Output: a Markdown table (id, kind, expect, got, rules, met) and the totals: detection
  rate = tamper cases met / tamper cases; false-positive rate = control cases with any
  block finding / control cases; control warn rate = control cases with any warn / control
  cases. A case may carry `known_gap = "<why>"` when greenproof is not expected to meet it
  yet (a pattern no rule covers); it still counts as missed in the detection rate, is
  listed under "Known gaps" with its reason, and does not affect the exit code. Exit 1
  when any other case is not met.
- At least 45 cases: P1–P17 and C1–C12 (from `tests/test_probes.py`), Q1–Q10 (loosened
  checks, 5.4), and at least 12 more covering the patterns below, each with its source:
  `pytest.skip()` inside a test body; `if False:` / early `return` before the assertions;
  `except Exception: pass` around an assertion; `pytest.approx` tolerance widened;
  `collect_ignore` in conftest; `addopts = "-k not total"`; a test edited to expect the
  buggy output while the code is unchanged; special-casing the test input in code
  (`code.hardcoded`, warn); mocking the function under test (`tests.self_mocked`, warn);
  `it.only`, `it.skip` and `expect(x).toBe(3)` → `toBeTruthy()` in js-cart; `t.Skip()` and
  a removed `t.Errorf` check in go-calc; `continue-on-error: true` on a test step;
  `--passWithNoTests` or `|| true` added to the test script; a new test that passes
  without the source change (revert, warn); plus at least 3 more controls (a legitimate
  rename across code and tests, a new feature with new tests, a spec change that updates
  code and expected values together).
- A test in `tests/` runs `bench/run.py` on all cases (marked slow if it exceeds 60 s with
  xdist) so the benchmark cannot rot.

**Replay harness** `bench/replay.py REPO [--n 200] [--out PATH.tsv] [--details]`: on a
temporary clone of REPO (never the original), for each of the last N first-parent,
non-merge commits whose diff touches a file greenproof classifies as a test: check out the
parent, commit a policy with empty commands as the base, set index and worktree to the
commit's tree with `git read-tree -u --reset <sha>`, rewrite the same policy file (so the
base and candidate policies are equal), and run `greenproof check --checks static --base
HEAD --no-cache --json`. TSV columns: short sha, exit code, sorted `rule(severity)` list,
check duration in ms, subject (70 chars). `--details` also writes, per blocked commit, every
block finding with its path, line and message. Summary on stderr: commits scanned, blocked
count and rate, block count per rule, median and p95 duration.

Repositories: pallets/click, encode/httpx, pmndrs/zustand, spf13/cobra (clones under
`/tmp`, not committed). Results and the triage of every blocked commit go in
MEASUREMENTS.md.

## 13. Release preparation (M4)

- **pytest-xdist** as a dev dependency; the gate becomes `pytest -n auto`; any test that
  is not parallel-safe is fixed or marked to run serially.
- **Dogfood**: run `greenproof init --agents claude --ci none --hook-command "uv run
  greenproof"` on this repository and commit the result (`.greenproof.toml` with
  `uv run ruff check .`, `uv run mypy src`, `uv run pytest -q -n auto`; AGENTS.md section;
  `.claude/settings.json`). The repository's own CI gains a `greenproof` job on
  `pull_request` that runs `uv run greenproof check --mode ci` with `fetch-depth: 0` and
  read-only permissions, with actions pinned like the rest of ci.yml.
- **README.md**: what it is and the problem (agents report done on red or tampered suites);
  install (`uvx greenproof`, `pipx install greenproof`); quick start (`greenproof init`,
  then `greenproof check`); how a check works (snapshot, base, policy from base, static
  rules, commands, revert, smoke, cache, exit codes); the rule table with defaults; the
  agent support matrix (Claude Code, Cursor, Codex CLI: supported; Gemini CLI:
  experimental) with the caveats: hooks are advisory and fail open, an agent can edit
  them, CI is the authority; CI usage (the action and the generated workflow); accepting
  a finding (PR comment by a maintainer; local acceptances are advisory); smoke setup by
  hand (init never writes `[smoke]`, and `init --yes` rewrites the whole policy); the limits
  (heuristics, Python AST and line-level elsewhere, revert runners); a short link to
  MEASUREMENTS.md. No claims that are not measured in MEASUREMENTS.md. No comparison
  table with named competitors.
- **CHANGELOG** `0.1.0` entry; version stays `0.1.0`; nothing is published.
