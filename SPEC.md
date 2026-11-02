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

   **Read-only git directory (local mode).** Agent sandboxes (Codex's workspace-write, for
   one) let the agent write the working tree but not the git directory, so `git add -A`
   cannot store the snapshot's objects. In local mode only, when the snapshot fails and a
   probe file cannot be created in the object directory (`git rev-parse --git-path
   objects`), greenproof retries with a temporary object directory for the rest of the run:
   `GIT_OBJECT_DIRECTORY` set to it and `GIT_ALTERNATE_OBJECT_DIRECTORIES` set to the
   repository's object directory, for every git command greenproof runs. The directory is
   deleted on exit, and nothing is written under the git directory (no cache, no state).
   Checks that need a worktree cannot run: the revert check is skipped with the note "the
   git directory is read-only here; the stop hook runs this check", and `--isolate` exits 4
   saying it needs a writable git directory. The report carries the note "the git directory
   is read-only here (an agent sandbox?), so greenproof used a temporary object store". Hook
   and CI mode are unchanged: a git failure there fails closed.
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
      `origin/HEAD`'s target, else `main`, else `master`. If HEAD *is* the default branch,
      the base is `git merge-base HEAD @{upstream}`: the commits not yet pushed are part of
      the change under test, and so are uncommitted changes. When HEAD equals its upstream
      (CI on a push) the base is `HEAD`, and only uncommitted changes are compared. When the
      branch has no upstream the base is `HEAD` and the report carries the note "on the
      default branch with no upstream: only uncommitted changes are checked; pass --base REF
      to include commits" (`gitops.NO_UPSTREAM_NOTE`). When an upstream exists but shares no
      history with HEAD (`merge-base` fails, as in a shallow clone), that is not "no upstream":
      it is exit 4 with the fix (`fetch-depth: 0`), never a silent fall back to `HEAD`.
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
| `tests.expected_changed` | warn | a literal in an assertion's expected side changed in a kept test (same number of expected literals), or a modified expected-output data file (5.4), whether or not non-test code changed. With no non-test code change it is the "edit the test to match what the code returns" pattern, which commands and revert cannot see (the suite passes and revert has no source change to undo); the message says which case it is. Legitimate when the old value was wrong or the spec changed, so warn. It also fires when a `@pytest.fixture` function in a `conftest.py` or test file returns or yields a different literal (number, string, tuple, list, dict) than at the base; the message names the fixture and file, and every test using the fixture compares against that value |
| `suppress.added` | warn | an added line introduces a type/lint/coverage suppression (see 5.5) |
| `runner.tampered` | block | a change narrows or neuters how checks run: test selection, thresholds, strictness, test scripts, a script or file a policy command runs, a Go `TestMain` that always exits 0 or no longer runs the tests (see 5.6) |
| `ci.tampered` | block | a CI workflow change removes or neuters a test/lint step, adds `continue-on-error`, `if: false`, a swallowed failure in a test or lint step (`|| true`, `|| echo ...`, `; exit 0`, `; true`, an `exit 0` after the check, `set +e` that never reads the status, each only after a check command; under GitHub's default `bash -e`, `; exit 0` and `; true` swallow nothing), `--collect-only` or `--passWithNoTests`, an `exit 0` before the first check, a selection flag on a test command (`-k`, `-m`, `--deselect`, `--ignore`, `-run`, `-short`, `-t`, `--testNamePattern`, `--skip`, `--grep`), a trigger that runs on fewer events or paths (see 5.6), or changes the greenproof step itself, `with:` inputs included (see 5.6 for what is read where). The greenproof step is a line, outside a YAML comment, that runs `greenproof check` (any prefix such as `uvx`, `uv run` or a path, any `@version`) or `uses:` the greenproof action; other mentions (`greenproof --version` in a packaging job) do not count. Only those lines count, and a workflow whose base version has none gaining them is adoption, not a change (a new workflow file included); commenting out or deleting an existing greenproof line still fires. A ref-only version bump of the greenproof action (same inputs) is `warn` |
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
- Go build constraints: every `//go:build <expr>` and `// +build <tags>` line above the
  package clause of a `_test.go` file is compared between the base and the candidate
  version of the file. A file that had no constraint in either syntax and gains one is
  `tests.skipped` (block), naming the constraint: with it `go test ./...` no longer compiles
  the file. A file that had a constraint whose constraint lines now differ is
  `tests.skipped` (warn): widening `//go:build linux` to `linux || darwin` is legitimate,
  narrowing it is a skip, and the rule cannot tell which. Two edits are no finding: adding a
  `//go:build` line over `// +build` lines that stay the same and name the same tags
  (gofmt's automatic migration), and removing `// +build` lines under a `//go:build` line
  that stays the same (`go fix`). An unchanged constraint gives nothing, and neither does a
  constraint on a brand-new test file (it hides no existing verification, the same rule as
  a conditional skip on a new test).

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
- an exact numeric check loosened to an explicit tolerance: `assert f(x) == 99` (or
  `assertEqual`) replaced by `pytest.approx(99, rel=..., abs=...)`, `math.isclose(f(x), 99,
  rel_tol=..., abs_tol=...)` or `assertAlmostEqual(f(x), 99, delta=... | places=...)` is
  weakened when the band the tolerance allows is at least 1% of the absolute expected
  value (`rel`/`rel_tol` of 0.01 or more; `abs`/`abs_tol`/`delta` of at least 1% of the
  expected value; `places` giving `0.5 * 10**-places` that wide). For an expected value of
  0 the absolute band must be at least 0.01. A bare `pytest.approx(99)`/`math.isclose`
  (the library defaults) and a tight explicit tolerance are the honest fix for floating
  point and stay clean. The existing widening check above has no threshold and is
  unchanged;
- `@pytest.mark.parametrize` cases removed, unless every removed case (the exact form of
  its argvalues element) appears among the cases the change adds (all cases of new tests,
  plus the cases a paired test gained, as a multiset difference). Splitting cases out
  into other tests or files is not weakening;
- an assertion moved inside `try:` whose handler catches `AssertionError`/`Exception`
  without re-raising;
- an assertion made vacuous or tautological: `assert <truthy constant>` (`assert True`,
  `assert 1`, a non-empty string, f-string or bytes literal, a non-zero number, a non-empty
  list, tuple, set or dict display such as `assert [x]` or `assert (x, "message")`,
  `assert not []`), `self.assertTrue(True)`, an `or` with a truthy constant operand
  (`assert x == 3 or True`), or a comparison of an expression with itself (`==`, `is`,
  `<=`, `>=`; `assertEqual(E, E)`, `assertIs(E, E)`, `assertTrue(E == E)`), also after
  single-assignment locals (assigned exactly once in the test body, before the assertion,
  from any expression, and not used before it in any other way) are inlined. An operand with
  no call (names, attributes, subscripts, literals: `x == x`, `a.b is a.b`) is the same
  value on both sides and is vacuous, like `assert True`. A comparison where either side
  contains a call is never vacuous in itself, because a call can return a fresh or a
  stateful value: `get_settings() is get_settings()` pins a singleton, `Money(5) ==
  Money(5)` runs `__eq__`, `f(x) == f(x)` pins determinism. It is vacuous only where it
  replaces a real comparison: the same test at the base had an assertion that compared that
  call (any call inside the operand, locals alpha-renamed) with a different expression, and
  the candidate has more self-comparisons of that call than the base and no longer compares
  it with anything else. So `assert refund(100)
  == 99` becoming `assert refund(100) == refund(100)` blocks, and a determinism or
  singleton test that is new, unchanged, or whose call the base never compared passes.
  Stateful calls (`next`, `pop`, `random`, `uuid`, `time`, `now`, `utcnow`, `today`,
  `urandom`, `token_hex`, `token_bytes`, `rand`, `randn`, `randint`, `uniform`, `sample`,
  `shuffle`, `choice`, `await`, `yield` and walrus expressions) never count, in either form;
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
  subject of the base test still the subject of an exact check in the candidate, and every
  loose-check target of the base test still checked (exactly or loosely) in the candidate, is
  `tests.weakened` **warn** ("only loose checks dropped"), not block. Dropping
  `assert isinstance(stream, Iterable)` while every `==` stays is a cleanup more often
  than a tamper; dropping any exact check, or the only check on a target, still blocks.

Python also treats these as weakening:
- **shadowed and deleted tests.** Python collects only the last binding of a name in a
  module or class body. A test `def` that is not the last binding of its name (a later `def`
  of the same name, a later `del name`, or a later non-def rebinding such as `name = None`
  or an import alias) is not collected and is not a unit; the earlier checks are gone, so
  the usual removed/weakened comparison applies. An assignment that reads the same name
  (`test_x = pytest.mark.slow(test_x)`) is a wrapper, not a rebinding. Duplicates that
  already exist in an unchanged base file produce no finding;
- a **swallowing decorator** added to a test. The decorator resolves to a function in the
  same file, or in a test-path or test-support module the file imports (5.4 helpers below).
  It swallows only when its wrapper (a nested def, at any depth, that may be a decorator
  factory's inner def) has a `try` whose body calls the wrapped callable (the decorator's
  own parameter, or a name bound in an enclosing wrapper) and a handler that catches
  `AssertionError`, `Exception`, `BaseException` or a bare `except` without re-raising. A
  `raise` anywhere in the wrapper, outside nested defs, means it is not swallowing, and a
  `try` that does not call the wrapped callable is ignored. So a retry decorator that
  re-raises after its loop, and a cleanup `finally: try: rmtree() except Exception: pass`
  around the wrapped call, are clean. The test's checks are treated as swallowed, with the
  same message as an assertion wrapped in such a `try`;
- **cross-file helpers.** A test that calls a function imported from a test-path Python
  module (`from helpers import f`, `from tests.helpers import f`, `from .helpers import f`,
  `import helpers` with `helpers.f()`) gets the same helper credit as a helper in the test's
  own file: the helper's own assertions count, and a helper that only does `return a == b`
  still counts as no check. Credit follows helper calls up to 8 levels deep (`check_order`
  calls `_check_fields`, which calls `_check_total` and `_check_id`); a cycle is cut where it
  closes. The module is resolved against the test file's directory, the repository root and
  the test roots, and read at the same revision as the test file. A module outside the test
  paths gets the same credit only if it is **test support**: it imports `pytest`,
  `unittest` or `hypothesis`, or a segment of its path is `testing`, `test_utils`,
  `testutils`, `pytest_plugin` or `pytest_plugins`. An ordinary application module
  (`app/service.py` with a defensive `assert`) is never credited, so a test that calls it
  gets no check from it;
- **helper and conftest edits.** When a helper module (a test-path or test-support `.py`
  file with no tests of its own) or a `conftest.py` changes, both sides are analysed and the
  names (functions, fixtures) compared. A name that lost credited checks (fewer assertions or
  strong assertions, more weak, broad, vacuous or swallowed ones, a lost exact check, a new
  swallowing decorator, a call compared with itself in place of a comparison) or was removed
  pulls in its dependents; a change that loses none (a comment, a rename, an added check)
  analyses no dependent. The dependents are the unchanged test files that contain one of those
  names as a word, and for a helper module also the module's stem, and that import the module
  (before or after the change). For a helper module these are the test-path files anywhere in
  the tree, so `tests/support/helpers.py` reaches `tests/unit/test_o.py`. A conftest's scope
  stays its own directory and below, and it needs no import. They are analysed as if they
  had changed, so a helper that is neutered weakens every test that relied on it. At most 300
  files are analysed per check and at most 200 compared; greenproof reads the rest as
  unchanged. Rules return findings only, so a run that hits the caps carries no report
  note. Files not in the changeset are identical at both revisions and are read once.

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

Line-engine detections added in 0.3.0 (JS/TS, Go, Rust), all matched on comment-free text with
string contents blanked, so brackets inside strings do not count:
- **Unreachable after a return.** A bare `return` (Go, Rust), or `return;` / `return` followed
  by a newline (JS/TS), at the top level of a test body makes every later check in the test
  unreachable: uncounted, and `tests.weakened` when the count of unreachable checks rises.
  A `return` inside a branch, a closure or a callback is not an early return, and neither is
  `return expect(...)`, `return promise.then(...)` or `return request(...)`, which return
  the check itself. A bare `return` whose previous code line is a control header with no `{`
  (`if (...)`, `else if (...)`, `for (...)`, `while (...)` ending in the closing parenthesis,
  or `else`, also when the condition spans lines) is that branch's body: conditional, not an
  early return. A one-line `if` on an always-true condition whose only body is a bare
  `return` at the top level counts as an early return: JS/TS `if (true) return;` and
  `if (true) { return; }`, Go `if true { return }`, Rust `if true { return; }`. The condition
  is `true`, or `true` joined to anything by `||` (`x || true`); `x && true` and a
  `return value;` body are ordinary.
- **Always-false conditions.** Go `if … && false {`, `if false && … {`, `if false {`; JS/TS
  `if (false)`, `if (… && false)`, `if (false && …)`; Rust `if false {`, `&& false`.
  Checks inside the block are unreachable; an `else` branch is live. `x || false` and
  `x && y` are ordinary conditions.
- **Self-equality is vacuous.** JS `expect(X).toBe/toEqual/toStrictEqual(X)`; Node
  `assert.equal/strictEqual/deepEqual/deepStrictEqual(X, X)`; Go `assert.Equal(t, X, X)`,
  `require.Equal(t, X, X)` (also `EqualValues`, `Exactly`); Rust
  `assert_eq!(X, X)` and `assert!(X == X)`. Operands are compared with whitespace removed.
  An `X` that contains a call (any `(`) is not vacuous by itself: `expect(select(state)).toBe(
  select(state))` checks memoization and `assert_eq!(h("ab"), h("ab"))` checks determinism.
  Such a call self-comparison is vacuous only when it replaces a real comparison: the base
  version of the same test compared the same normalised expression with a different one
  (`expect(add(1, 2)).toBe(3)` → `expect(add(1, 2)).toBe(add(1, 2))`); one already in the base
  test, in a new test, or on an expression the base did not compare is a real check.
  Go's `if got != got {` is not vacuous either: it is the NaN check. An `X` that names a
  nondeterministic source (`random`, `Date`, `now(`, `uuid`, `nanoid`,
  `rand`, `Instant`, `SystemTime`) is exempt. A vacuous check is not counted and raises the
  vacuous count, so it blocks only when it appears in a test that exists on both sides.
- **Table rows.** `it.each`, `test.each` and `describe.each` with a literal array of arrays
  or objects, or a tagged-template table, count their rows as parametrize cases (a
  `describe.each` multiplies the tests inside it). Fewer rows than at base is `tests.weakened`
  ("parametrize cases reduced from N to M"); adding rows is fine. A table that is not a
  literal (`it.each(cases)`), or whose array has an element starting with `...`
  (`it.each([...small, [3, 4, 7]])`), is not counted.
- **Helper credit.** JS/TS: a function declared in the same file outside any `it`/`test`
  (top level or inside a `describe`) whose body contains an assertion counts as one check
  per call inside a test. Go: the assertion helpers above are read from every `_test.go`
  file in the directory at the same revision as the test file (base text for the base side,
  the candidate tree for the candidate side), and when a changed `_test.go` file declares
  helpers, unchanged same-directory test files that call them are re-analysed as if they
  had changed, so a helper that loses its checks weakens its callers. A helper without an
  assertion earns nothing.
- **Rust char literals.** A Rust character literal (`'}'`, `'"'`, `'\n'`, `'\''`, matched
  by `'(?:\\.|[^\\'])'`) is masked like a string, so its brace or quote is not code. A lifetime
  (`'a`) is left alone.
- **Expected-output data files.** A modified, or renamed with changed content (not added,
  not deleted, not a pure rename), non-code file with
  extension `.json`, `.yaml`, `.yml`, `.txt`, `.csv`, `.snap`, `.golden`, `.out`,
  `.expected`, `.html` or `.xml` under a test path, or under `testdata`, `fixtures`,
  `golden` or `__snapshots__`, gives one `tests.expected_changed` (warn), "expected-output
  file changed: PATH". Files another rule owns (runner configuration such as
  `package.json` and `tsconfig*.json`, and CI workflows) are not reported here, and neither
  are files that are configuration rather than expected output: `requirements*.txt`,
  `constraints*.txt`, `*compose*.yml`, `*compose*.yaml`, `Dockerfile*`, `.pre-commit*`,
  `codecov.yml` and `.env*`.

Line-engine assertion vocabulary also includes: JS/TS `expectTypeOf(`, `assertType(` and
a testing-library query that throws when the element is missing (`getBy…(`,
`getAllBy…(`, and awaited `findBy…(`/`findAllBy…(`, bare or on `screen.`), counted only on
a line with no other assertion call, not inside the arguments of an `expect(` still open from
an earlier line (parenthesis depth on the comment- and string-masked text, so a
Prettier-wrapped `expect(\n  await screen.findByText('a'),\n).toBeInTheDocument()` counts
once), and not when its result is bound to a name
(`const el = await findByText('a')`) that a later `expect(el` in the same test reads, since
that `expect` already counts. So `expect(getByText('a')).toBeVisible()` and the bound pair
`const el = await findByText('a')` + `expect(el).toBeVisible()` both count once, and
`findByText` → `getByText` is not a drop; `queryBy…` returns null and is not counted. Go **assertion helpers**: a
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

Each pattern applies only to files where it has an effect (M5). The comment and type
patterns keep their languages (Python, JavaScript/TypeScript, Go, Rust). `|| true`,
`; true$`, `set +e` and the git `--no-verify` pattern apply only to **shell-capable
files**: suffixes `.sh`, `.bash`, `.zsh`, `.ksh`, `.mk`, `.yml`, `.yaml`, `.dockerfile`;
basenames `Makefile`, `GNUmakefile`, `makefile`, `justfile`, `Justfile`, `.justfile`,
`package.json`, `pyproject.toml`, `tox.ini`, `setup.cfg`; basenames starting with
`Dockerfile` or `Containerfile`; any file under a `.husky/` or `.githooks/` directory.
`--passWithNoTests` applies to shell-capable files and JavaScript/TypeScript files (jest
and vitest configs). Data and fixture files (`.toml` other than `pyproject.toml`, `.json`
other than `package.json`, `.txt`, `.csv`) and prose never match.

### 5.6 Runner tampering (`runner.tampered`)

Structural diff (parse TOML/JSON/INI with stdlib; YAML by line heuristics) of:
- `pyproject.toml` `[tool.pytest.ini_options]` (`testpaths`, `addopts` adding `-k`,
  `--ignore`, `--deselect`, `-p no:`), `[tool.coverage.report] fail_under` lowered,
  `[tool.mypy]` (`strict` off, `ignore_errors`, `disable_error_code` added), `fail_under`
  removed (counts as lowered), `python_files`/`python_functions`/`python_classes` narrowed,
  `[tool.ruff.lint]` (`ignore`/`extend-ignore`/`per-file-ignores` added);
- `pytest.ini`, `setup.cfg`, `tox.ini`, `noxfile.py` equivalents. In `tox.ini` and
  `noxfile.py` (read with `ast`, so a reformatted call is the same call) a selection flag
  counts only after the `pytest` token and only when the base's pytest commands did not
  already have it; `coverage run -m pytest` is not a marker flag, and `*session.posargs` is
  not a selection. Arguments are read through starred list or tuple literals
  (`*["-k", "x"]`), `*"-k x".split()` and names bound once to such lists, `+` joins included
  (a name assigned twice, mutated, or shadowed by a parameter is unreadable). A starred
  argument other than `session.posargs` that cannot be read, and is new or changed, is a
  `warn`;
- `-p no:<plugin>` in `addopts` is judged by the plugin: harmless ones (`cacheprovider`,
  `randomly`, `xdist`, `logging`, `faulthandler`, `legacypath`, `pastebin`, `junitxml`, `sugar`,
  `instafail`) say nothing, ones that decide outcomes (`python`, `unittest`, `doctest`, `cov`,
  `pytest_cov`, `asyncio`, `anyio`, `trio`) block, `warnings` blocks only where the base turns
  warnings into errors (`filterwarnings` with `error`, `-W error`, `-Werror`), and an unknown
  plugin is a `warn`. `--disable-plugin-autoload` added to `addopts` is a `warn` too (every
  installed plugin stops loading unless named with `-p`);
- `package.json` `scripts` whose names appear in the base policy's commands, plus
  `test`, `lint`, `typecheck`, and the scripts those call through `npm run NAME`, `run-s`,
  `run-p`, `npm-run-all` or `pnpm run '/regex/'`, followed transitively (`npm run a && npm run
  b` and `run-s a b` are the same steps). The regex comes from the candidate, so it is matched
  by a linear-time automaton over a safe subset (literal text, `^`, `$`, `.`, character classes,
  `*`, `+` and `?` on one atom, `|` between literals, one group at most, 200 characters); a
  pattern outside it matches every script, which keeps the block. `pre<name>` and `post<name>` run with `<name>`, so a
  step split off into `pretest` is the same steps. Only checks count towards "fewer steps": a
  cleanup line (`rm -rf`, `echo`, `mkdir`) removed, or moved to a prerequisite, is not a lost
  check. A script the base did not run that the candidate's `test` now runs (a new alias) is
  read as new: a narrowing flag, `--passWithNoTests` or a swallow in it blocks. A swallow added
  to an opaque script (one with no recognised check, such as `ng test`) blocks when the edited
  statement is one the base had. A candidate script that runs a kind of check the base's did
  not (`jest` became `eslint .`) blocks, whatever the step count: the kinds of check
  (`check_kind`) are compared with `npm run x` read into its body, lint tools count as one
  kind, a wrapper (`npm test`, `make test`) stands for any test runner, and a script with a
  statement that cannot be classified is not judged by kind. Two invocations of the same tool
  are the same opaque step only when their subcommand matches (`ng test` and `ng lint`
  differ, `ng test` and `ng test --no-watch` do not; the subcommands are `test`, `lint`,
  `check`, `build`, `run`, `e2e`, `typecheck`, `format`, `verify`, `ci`, `coverage`, `spec`,
  `unit`, `integration`, `serve`, `start`, `watch`). A nested `package.json` is a `warn`, not
  a block, only when it is known that no policy command reaches it: every policy command is a
  plain root package-manager run (`npm test`, `pnpm run x`, `yarn test`, `bun test`) with no
  workspace, filter, prefix or directory option, and the root scripts it reaches contain no
  `cd`, `--prefix`/`-C`/`--dir`/`--cwd`, workspace or recursive flag, `lerna`/`nx`/`turbo`,
  script file or `make`; or a workspace selector (`-w`, `--filter`, `-r`) is used and the
  package is provably not a member of the workspace globs. Anything else, including a
  `lerna.json`, a make-target policy command or an unreadable file, keeps the block.
  `jest.config.*`/`vitest.config.*` (`testPathIgnorePatterns`,
  `exclude`, `coverageThreshold` lowered, `passWithNoTests`); `tsconfig*.json` (`strict`
  or `noImplicitAny` set false, `skipLibCheck` is ignored);
- `Makefile`/`justfile` targets referenced by the base policy's commands (all targets are
  read, `define ... endef` bodies are not targets; a recipe emptied while gaining
  prerequisites, or either side has prerequisites, is judged by the recipes of those
  prerequisites, up to four deep). The Makefile's own variables (`=`, `:=`, `::=`, `?=`,
  `+=`, optional `export`) are expanded in recipes and prerequisites first, recursively to a
  depth limit, so `PYTEST = true` behind `$(PYTEST) -q` is a no-op; a variable the Makefile
  does not define is left as written. A target that is not in `.PHONY` (continuations
  included), has no `FORCE` prerequisite and is named like a file or directory beside the
  Makefile in the candidate tree never runs its recipe: it counts as running nothing. A `-`
  prefix on a line blocks only when the line is a check (`-rm -rf build` is fine);
- a swallowed failure in any of those scripts or recipes, found by the shared shell reader.
  A *check* is a statement whose command, in command position (after environment
  assignments and wrappers such as `uv run`, `python -m`, `npx`, `time`, `sudo`), is a known
  test runner, linter, type checker, or a policy command; a runner name that is only an
  argument (`pip install pytest-xdist`, `grep pytest`, `echo pytest`), `--version` and
  `--help`, and `make` with `-C` or `-f` are not checks. A swallow blocks only on a check:
  `|| true`, `|| :`, `|| echo ...` after it, `; true`, and `; exit 0` or an unconditional
  `exit 0` after it when the shell is not running with errexit, and `set +e` before it, up
  to the next `set -e`, when its status is never read. So `rm -rf build || true` and
  `pip install -e . || pip install .` are not swallows. A script with no recognised check is
  opaque: its statements from the base count as checks, so a base line that gains a swallow
  blocks, and a new line carrying one does not. Heredoc bodies are data. Only swallows the
  base did not already have are reported. The selection-flag table (`-k`, `-m`,
  `--deselect`, `--ignore`, `-run`, `-short`, `-t`, `--testNamePattern`, `--skip`,
  `--grep` and their relatives, by runner) is shared with `ci.tampered`; a flag before the
  runner (`python -m pytest`) does not count. In `runner.tampered` the flags belong to their
  tool: `-k` narrows only pytest, `-t` narrows jest and vitest but is mocha's timeout, mocha
  narrows with `-g`, `--grep`, `-f`, `--fgrep` and `--only`, and `make -k` narrows nothing. Detection is linear in the script: 8000 checks
  and 2000 `exit 0` lines are read in under 3 seconds;
- a file a base policy command runs (`sh scripts/test.sh`, `python scripts/run_tests.py`,
  `./ci/check`), and the files those run, one level deep: deleted, emptied, replaced by a
  no-op, given a swallow, a new selection flag or an unconditional `exit 0` before its
  checks, or (for a non-shell script) given a top-level `sys.exit(0)`. Test files named as
  arguments (`pytest tests/test_a.py`) are not scripts and are left to the test rules;
- a Go `TestMain(<name> *testing.M)` (the parameter name is the one in the signature, and
  strings, raw strings, runes and comments are skipped when matching braces) that gains a
  body which never calls `<name>.Run()` or calls `os.Exit(0)` unconditionally after it: a
  block, unless the base already had the same flaw. An `os.Exit(1)` inside a setup guard, and
  an `os.Exit(0)` inside a guard that tests the result of `Run`, are not the flaw. A
  conditional `os.Exit(0)` is a `warn`;
- `conftest.py` or a local pytest plugin gaining a hook that can rewrite outcomes or
  collection (`pytest_runtest_makereport`, `pytest_runtest_call`, `pytest_runtest_protocol`,
  `pytest_pyfunc_call`, `pytest_report_teststatus`, `pytest_ignore_collect`,
  `pytest_collect_file`, or `pytest_sessionfinish` assigning `exitstatus`), and
  `os._exit(` or `sys.exit(0)` added to a test or conftest file.
When structural parsing fails, fall back to a file-level `warn` naming the file.

`ci.tampered` reads CI files by indentation, with no YAML library; every workflow file is
read together, so a change is judged against the whole CI, not one file.

*Steps.* In a GitHub `run:` step (`run: |` and `>`, `>-`, `>+` folded scalars, folded as
YAML does), a swallow of a check the base step did not have blocks, by the shared detector
and its definition of a check. Which swallows exist depends on the step's shell, chosen like
GitHub does: an explicit `shell:`, else the job or workflow `defaults.run.shell`, else bash
on a Linux or macOS runner and PowerShell on a Windows one (a `runs-on` of `windows-*`, or a
`${{ matrix.* }}` that resolves to one; a matrix with both kinds is judged under both
shells, and a finding under either blocks).

- Default bash (and `shell: bash`, `sh`) runs with errexit on, so `pytest; exit 0` and
  `pytest; true` swallow nothing there. A custom shell string holding `{0}` and no `-e`
  (`bash {0}`, `sh {0}`) has no errexit: the step's status is the last command's, so `pytest`
  followed by `true`, on the same line or a later one, swallows the check. Only a swallow the
  base step did not have blocks.
- `pwsh`, `powershell` and `cmd` have no errexit either. The status is the last native
  command's (`$LASTEXITCODE`), so a check that is not the last top-level command and whose
  status is not propagated is swallowed, and so are `; exit 0`, a trailing `exit 0`,
  `|| true` and `$LASTEXITCODE = 0` after a check.
- `shell: python` (and any other interpreter the rule does not model) is not read; the step
  is skipped rather than guessed at.

The reader looks through launchers and wrappers before it classifies a command: `docker
compose run`, `docker-compose`, `conda run`, `hatch`, `poetry`, `pdm`, `uv`, `pipenv` and
`rye run`, `bash -c` and `sh -c` bodies (also scanned for swallows), and interpreter
variables (`$(PYTHON)`, `${PYTHON}`, `$PYTHON`, `$(VENV)/bin/python`, `.venv/bin/python3`,
`env/bin/pytest`, `$(BIN)/pytest`); `runner.tampered` shares this reading of Makefiles and
scripts. `A || B` is not a swallow when B is a check of the same kind with no narrowing flag
other than the rerun family (`--lf`, `--last-failed`, `--ff`, `--failed-first`, `--sw`,
`--stepwise`, `--reruns N`), so `pytest || pytest --lf` passes and `pytest || pytest -k
smoke` blocks.

The next checks apply to GitHub Actions `run:` steps only: a selection flag the base step
did not have, `--co`/`--collect-only` and `--passWithNoTests`, an `exit 0` before the first
check, and `set +e` that never reads the status. GitLab CI and CircleCI files get only the
line-level swallow check (`|| true`, `|| echo`, `; exit 0`, `; true`, with errexit assumed
on), on added lines.

*Flags.* `continue-on-error: true` and `if: false` on a step or job block, except when the
step recognisably runs no check: every command is a recognised non-check (`echo`, `true`,
`:`, `exit 0`, `sleep`, `printf`, `cat`), or the step `uses:` a known auxiliary action
(`actions/checkout`, `actions/setup-*`, `actions/cache`, `actions/upload-artifact`,
`actions/download-artifact`, `codecov/codecov-action`, `coverallsapp/github-action`,
`docker/*`, `peaceiris/actions-gh-pages`, `softprops/action-gh-release`,
`pypa/gh-action-pypi-publish`). Unknown and local actions and check steps keep blocking. A
job whose steps include no check is exempt. The flag is also fine when some base workflow
already had the same step (same normalised `run` body, or the same `uses:` without its ref)
with `continue-on-error: true`, so moving or splitting a step that was already allowed to
fail is not new. A quoted or capitalised `"false"`, `'false'` or `False` is false for
`continue-on-error` (no finding), and `"true"`, `'true'` and `True` are true. For `if:`,
`false` in any quoting still disables the step and blocks. `continue-on-error: ${{ ... }}`
and a selection flag whose value is a `${{ matrix.* }}` expression are `warn`.

*Step kinds.* Test and lint steps are counted by kind (pytest, jest, go test, ruff, ...) over
the `run:` bodies of all workflow files, by the same check test. Beyond the usual unit-test
runners the list covers `playwright test`, `cypress run`, `rspec`, `rake test`/`spec`,
`phpunit`, `dotnet test`, `mvn test`/`verify`, `gradle test`/`check`, `ctest`, `bats`, `tox`,
`nox`, `hatch test`, `ng test`, `karma start`, `ava`, `tap`, `node --test`, `deno test`,
`bun test`, `swift test`, `mix test`, `cargo nextest` and `cargo llvm-cov`, and `gotestsum`.
A candidate step also counts for the checks it runs through another file: a local composite
action (`uses: ./path`, read from `action.yml` or `action.yaml` in the candidate tree, its
`runs.steps[*].run`) and a local script (`./scripts/test.sh`, `bash scripts/test`,
`sh x.sh`), read one level deep with a 64 KiB cap. Expansion only adds kinds on the
candidate side; the base is read as written. A kind that vanishes from every candidate
workflow blocks, unless the step that replaced it is something the rule cannot classify
(`poe test`, `pre-commit run`, `uses: astral-sh/ruff-action@...`, an unknown action): then
it is a `warn`. Replacing a check with a recognised non-check (`echo`, `true`, `exit 0`)
still blocks. Fewer steps of a kind that still exists is `warn`. A kind that vanishes is
also only a `warn` when the candidate tree holds a CI system this rule does not read
(`.travis.yml`, `azure-pipelines.yml`, `Jenkinsfile`, ...) or a surviving `make test`,
`npm test` or `tox`/`nox` step that may run it. A YAML anchor on `steps:` (`steps: &name`
and `steps: *name`) is read; aliased steps report the line of the anchor.

*The greenproof step.* Greenproof steps are compared across all GitHub workflow files as a
multiset of signatures: the `uses:` without its ref, `with:`, `if:`, `continue-on-error` and
`env:`. Moving a step unchanged, or to another file or job, passes. Changing a `with:` input,
`if:` or `env:` blocks, as does removing the step or deleting the workflow that holds it;
adding the first greenproof step is adoption. A ref-only bump is `warn`. Non-GitHub files
compare their greenproof lines per file.

*Triggers.* The `on:` section (scalar, flow or block form) is read; anchors, aliases, an
unclosed flow collection or anything else unreadable make it unknown, and the rule says
nothing about triggers. An event removed from a workflow is fine when another candidate
workflow triggered on that event calls it (`uses: ./.github/workflows/<file>`) or runs every
test-step kind it ran; otherwise it blocks, as does a workflow with a test step that loses
`push` and `pull_request`. A `paths` or `paths-ignore` filter is evaluated for the base and
the candidate with GitHub's glob rules (`*` does not cross `/`, `**` does, the last matching
pattern wins, `!` negates) against the candidate tree: it blocks only when a test or source
file that triggered the workflow under the base filter does not under the candidate's.
Excluding documentation or meta files (`mkdocs.yml`, `.vscode/**`, `*.md`) and renaming a
directory block nothing. An added or narrowed `branches`, `branches-ignore`, `tags`,
`tags-ignore` or `types` filter is `warn`. Swapping `pull_request` for
`pull_request_target`, adding events, matrix changes and new jobs are not narrowing.
Dropping `push` while keeping `pull_request` is narrowing and blocks; so does splitting
`-m 'not slow'` and `-m slow` into separate steps, because each step carries a selection
flag (a known limit: the two halves together run everything, and the rule does not add them up).

Other CI files (GitLab, CircleCI, Jenkins and so on) get the line-level swallow check on
added lines, with errexit assumed on, and none of the GitHub-only checks above.

A linter or type-checker configuration that does not exist at the base (a new
`[tool.ruff]`/`[tool.ruff.lint]` or `[tool.mypy]` table, a new `ruff.toml`, `.ruff.toml`,
`mypy.ini` or `.flake8`) is adoption or a migration: its ignores, per-file ignores and
disabled error codes are `runner.tampered` **warn**, not block, because there was no
stricter configuration to loosen. Ignores added to a table that existed at the base
still block.

*Known limits.* A lint ignore for a rule that is new in a version bump of the linter still
blocks (httpx `fa03b489f`: the ignore is the honest answer to a bump, and the rule cannot tell
it from a loosening without the linter's changelog). Coverage floors moved from the pytest
options into a `codecov` block are read as lowered (httpx `46402dd59`: the rule does not read
`codecov.yml`).

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
- Python autouse fixtures. An added or changed `@pytest.fixture(autouse=True)` in a
  `conftest.py` or a test file (a patch not already in the same fixture at the base) that
  patches a callable with the forms above, or by assigning to a module attribute
  (`shop.refund = ...`), fires when a test in its scope calls that callable inside an
  assertion: the fixture's own file (its class, for a class fixture), or for a
  `conftest.py` every collected test at or below its directory. Only files that mention the
  patched callable's name as a word are read for the scope (the same pre-filter as helper
  dependents, no alphabetical cut). The patch target is
  resolved with the fixture file's imports and the call with the test file's. Message:
  "autouse fixture <name> patches <target> and the tests in its scope assert on <call>(...)
  (<tests>); they test the mock".
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
- OpenCode 2.x (M5): the README of the `@opencode/plugin` package (CLI `@opencode/cli`)
  and the `anomalyco/opencode` source. OpenCode 1.x (`opencode-ai`, documented at
  https://opencode.ai/docs/plugins/) is not supported. OpenCode has no command hooks: the
  plugin in 6.8 runs `check --hook opencode` and maps its output.

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
| opencode | `session_id` | `cwd` | none |

The **payload fingerprint** is the sha256 of the payload object serialized with sorted
keys and compact separators. An empty payload (`{}`, which is also what a TTY or
unparseable stdin counts as) has no fingerprint. Two hooks run for the same stop receive
the same payload; a later stop usually differs in at least one field (Claude Code's
`stop_hook_active` and `last_assistant_message`, Codex's `turn_id`, Cursor's
`loop_count`, the OpenCode plugin's `message_id`), but not always: a payload can repeat
across stops. It is used only by 6.2's duplicate rule, which therefore also requires the
same tree: a repeated payload on a changed tree runs the check.

The start directory, when it exists, replaces the process cwd for repo discovery. In hook
mode stdout carries only the protocol output below, never the text report; `--json` and
`--markdown` files are still written. The outcomes:

| outcome | claude `Stop` | cursor `stop` | codex `Stop` | gemini `AfterAgent` |
|---|---|---|---|---|
| pass | exit 0, no output | exit 0, `{}` | exit 0, no output | exit 0, no output |
| block | exit 2, agent message on stderr | exit 0, `{"followup_message": msg}` | exit 0, `{"decision": "block", "reason": msg}` | exit 0, `{"decision": "deny", "reason": msg}` |
| release | exit 0, `{"continue": false, "stopReason": note}` | exit 0, `{}` | exit 0, `{"continue": false, "stopReason": note}` | exit 0, `{"continue": false, "stopReason": note}` |

opencode speaks codex's protocol exactly (pass: no output; block: `{"decision": "block",
"reason": msg}`; release: `{"continue": false, "stopReason": note}`; always exit 0). The
plugin (6.8) turns these into a follow-up prompt or a toast.

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

**Duplicate hooks for one stop (M5).** The gate can be installed twice: a plugin (6.7)
and the project hooks `init` writes, or user and project settings. The agent then runs
both for one stop, in parallel; Claude Code does not deduplicate plugin hooks against
settings hooks. Without coordination the second run would find the first one's block,
apply rule 2 and release, and because Claude Code and Codex let one hook's
`continue: false` override every other hook's block, every block would become a release.
So:

- Everything after repository discovery (rules 1–3 and the state write) runs under an
  exclusive lock on `<git-dir>/greenproof/hook.lock`: `fcntl.flock` on POSIX,
  `msvcrt.locking` on Windows (retried until it succeeds). A second run waits for the
  lock. When the lock file cannot be opened, the hook runs without the lock.
- The state also records the fingerprint of the payload that decided it (6.1), the
  decision (`pass`, `block` or `release`), the agent message of a block, and a replay
  count. A pass now writes state (decision `pass`, zero blocks) instead of clearing it.
  Rule 1 (nothing to verify) still clears it.
- The order under the lock is: locate the candidate tree, replay, rule 1, rule 2, rule 3.
  The tree is `""` when locating it failed; that failure then goes straight to the
  failure path of rule 3, without locating again. The **replay**: when the payload has a
  fingerprint, and the state has the same session, the same fingerprint and the same tree,
  and its replay count is below 3, the hook outputs the recorded decision again in its own
  agent's protocol (pass; block with the recorded message; release with the recorded
  note), adds one to the replay count and changes nothing else. The report notes
  "replayed the decision of another greenproof hook for this stop". From the fourth replay
  on, the normal rules apply. Because the tree must match, a repeated payload on a changed
  tree never replays an earlier decision, a pass included.
- Rule 2 applies only when the recorded decision is `block` or `release`. A rule-2 release
  records itself: it saves the state with the payload's fingerprint, decision `release` and
  zero replays, so a duplicate hook for that stop replays the release and the earlier
  block's fingerprint cannot be replayed later. A recorded pass counts as zero previous
  blocks in rule 3.
- A state file without these fields (written by 0.1.0) counts as decision `block` with no
  fingerprint and zero replays.

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
- Output: claude, codex, gemini and opencode `{"hookSpecificOutput": {"hookEventName":
  "SessionStart", "additionalContext": text}}`; cursor `{"additional_context": text}`.
  The OpenCode plugin adds the context to the session only where OpenCode offers a
  supported way to (6.8); the marker is what matters.
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
    "SessionStart": [{"matcher": "startup|resume|clear|compact|fork",
                      "hooks": [{"type": "command", "command": "greenproof session-start --hook claude"}]}]}}
  ```

  No `SubagentStop`: a subagent stopping midway through the parent's task would be blocked
  for the parent's unfinished work. The parent's `Stop` is the gate. `fork` (M5) is the
  source of a session forked from another; 6.3 keeps its marker.
- **Cursor**: `.cursor/hooks.json`:
  `{"version": 1, "hooks": {"stop": [{"command": "greenproof check --hook cursor", "timeout": 960}], "sessionStart": [{"command": "greenproof session-start --hook cursor"}]}}`.
  No `subagentStop`, for the same reason.
- **Codex CLI**: `.codex/hooks.json`, verified against `codex-rs/config/src/hook_config.rs`
  (`HooksFile`) and `codex-rs/protocol/src/hooks.rs`: top-level keys `description` and
  `hooks` only (unknown keys are rejected), events nested like Claude's (`Stop`;
  `SessionStart` with matcher `startup|resume|clear|compact`, plus `fork` when Codex's
  SessionStart source enum has it (M5: verify in `openai/codex` and cite the file), handler
  `{"type": "command", "command": ..., "timeout": seconds}`. `init` prints: "Codex: review
  and trust the new hooks once with /hooks; Codex skips untrusted project hooks."
- **Gemini CLI**: experimental. Gemini CLI was superseded by Antigravity CLI on 2026-06-18,
  and the open issue google-gemini/gemini-cli#27712 reports `AfterAgent` hooks not firing.
  `.gemini/settings.json`: `AfterAgent` (matcher `*`, `"name": "greenproof"`, timeout in
  **milliseconds**) and `SessionStart` (matcher `startup|resume|clear`).
- **OpenCode** (M5): `.opencode/plugins/greenproof.js`, the plugin of 6.8 with the hook
  command baked in. The file is wholly greenproof's: its first line is
  `// greenproof plugin for OpenCode. Written by greenproof init; re-running init replaces it.`
  Re-running `init` rewrites it; an existing file without that first line → exit 4,
  nothing written. OpenCode 2.x loads it from `.opencode/plugins/` without a config
  entry (6.8).

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

### 6.7 Plugins for Claude Code, Codex and Cursor (M5)

A plugin gives the user the hooks without running `init` in each repository. One plugin
directory serves the three agents; each reads its own manifest.

```
.claude-plugin/marketplace.json     Claude Code marketplace
.agents/plugins/marketplace.json    Codex marketplace (format from openai/codex)
.cursor-plugin/marketplace.json     Cursor marketplace (format from cursor/plugin-template)
plugins/greenproof/
  .claude-plugin/plugin.json
  .claude-plugin/icon.png           square PNG, 512 x 512, under 256 KiB
  .codex-plugin/plugin.json
  .cursor-plugin/plugin.json
  hooks/claude.json  hooks/codex.json  hooks/cursor.json
  skills/greenproof/SKILL.md
  assets/logo.png                   the same image, for Cursor's `logo` and Codex's interface
  README.md
  LICENSE                           a copy of the repository's Apache-2.0 LICENSE
```

- **No `hooks/hooks.json`.** Each manifest names its own hooks file, so no agent loads
  another agent's protocol through a default path. Verify, for each agent, which files it
  loads from a plugin directory that carries other vendors' manifests (Codex probes
  `.codex-plugin`, then `.claude-plugin`, then `.cursor-plugin`), and that none of them
  picks up a hooks file meant for another.
- **Hook command**: `uvx greenproof@<version>`, `<version>` equal to `__version__` (uv's
  `@` form is an exact pin). The hooks are 6.4's: Stop/stop and SessionStart/sessionStart,
  no subagent stop, Claude's matcher `startup|resume|clear|compact|fork`, Codex's matcher
  per 6.4. The Stop timeout is 3600 seconds, because a plugin cannot read the policy.
  Plugins need uv on PATH; without it the hook cannot start and fails open (README).
- **Manifests**: `name` `greenproof`; `version` equal to `__version__`; description
  "Done gate for coding agents: when the agent stops, greenproof runs your checks and
  blocks deleted, skipped or weakened tests."; author Basant Bhattarai; homepage and
  repository https://github.com/technobasant/greenproof; license `Apache-2.0`; keywords.
  Vendor fields follow each vendor's documentation or source, and the test that checks the
  manifests names the source it was written against:
  - Claude Code: `"hooks": "./hooks/claude.json"`; skills from the default `skills/`.
    `claude plugin validate --strict` passes for the plugin and the marketplace.
  - Codex: `.codex-plugin/plugin.json` with the hooks and skills paths and the `interface`
    block (display name, short description, developer, category, logo, and the rest Codex
    defines), field names checked against the Rust structs in `openai/codex` and, where
    published, the agent-plugins JSON schema.
  - Cursor: `.cursor-plugin/plugin.json` with the hooks path and `logo`; cursor's
    `plugin-template` validator passes.
- **Skill** `skills/greenproof/SKILL.md` (frontmatter `name: greenproof` and a
  `description` saying when to use it: a "greenproof: not done" message, or the user asks
  to run the done gate, explain a finding or set greenproof up). At most 60 lines: what
  the gate does; how to answer a block (fix the code; never delete, skip or weaken tests
  or edit test, runner or CI configuration; if the requirement changed, stop and tell the
  user which test and why); `uvx greenproof@<version> check` to run it on demand;
  `greenproof explain RULE_ID`; `uvx greenproof@<version> init --agents none` to add a
  policy in a repository where the plugin already provides the hooks; never run
  `greenproof accept` and never edit `.greenproof.toml`, hook files or greenproof's state
  under the git directory to get past a block.
- **Plugin README** (at least 40 words): exactly what runs and when. On stop:
  `uvx greenproof@<version> check --hook AGENT` in the project, which runs the static
  checks and the commands in the project's `.greenproof.toml` (the project's own tests).
  On session start: writes the session marker under the git directory. uv downloads
  greenproof from PyPI on first use; greenproof itself makes no network calls in hook
  mode and has no telemetry. Install per agent, how to turn it off for one project, and
  that running `init --agents ...` in a repository that has the plugin is unnecessary
  (`init --agents none` adds only the policy and AGENTS.md); if both are installed, 6.2
  makes one decision per stop.
- **Marketplaces**: name `greenproof`, owner Basant Bhattarai, one plugin `greenproof`
  with source `./plugins/greenproof`. Claude Code: `claude plugin marketplace add
  technobasant/greenproof`, then `claude plugin install greenproof@greenproof`. Codex:
  `codex plugin marketplace add technobasant/greenproof` (the public Codex directory does
  not accept plugins with lifecycle hooks). Cursor: what its documentation offers for a
  GitHub-hosted plugin, and the marketplace submission.
- **Directory limits** (Anthropic): fewer than 512 files in the plugin directory, each
  under 256 KiB; a LICENSE; a kebab-case name.
- **Versions in step**: `scripts/bump_version.py X.Y.Z` rewrites `_version.py`, the
  manifests, the marketplace entries that carry a version, every pinned command in the
  plugin (hook files, skill, README) and the OpenCode package (6.8). A test fails when any
  of them differs from `__version__`.

Publishing (directory submissions, marketplace listings) is the maintainer's step, not
part of the implementation.

### 6.8 OpenCode plugin (M5)

OpenCode has no command hooks, so greenproof ships a plugin that runs the command and maps
its output.

- **One ES module**, plain JavaScript, only `node:` built-ins, no build step. Canonical
  source `src/greenproof/init/hooks/opencode_plugin.js` (package data; `init` writes it,
  6.4), mirrored in `integrations/opencode/index.js` (npm package `opencode-greenproof`).
  The two are identical except the `COMMAND` line; a test checks that.
- `const COMMAND = [...]` near the top is the argv prefix. `init` renders it from the hook
  command with `shlex.split`, as a JSON array; the npm copy has
  `["uvx", "greenproof@<version>"]`.
- **OpenCode 2.x only**: `@opencode/cli`, plugin contract of `@opencode/plugin`
  (documented in that package's README), source `anomalyco/opencode`, read at v2.2.5 on
  2026-11-01. OpenCode 1.x (`opencode-ai`, the API on opencode.ai/docs/plugins) is not
  supported: its loader rejects the module ("must default export an object with
  server()"), logs that and runs without the plugin; the README says so. Items marked
  *verify* were read in the source, not run; confirm each in the real CLI and stop and
  report when one does not hold. Every check uses a pinned CLI: the newest 2.x the local
  npm age policy allows (2.2.1 on 2026-11-01), run through npx in a temporary directory,
  never installed globally.
- **Export**: exactly one, `export default { id: "greenproof", setup }`, and no other
  export. The loader (`packages/core/src/plugin/module.ts`) decodes a default `{id,
  setup}`; `setup(ctx)` registers what follows.
- **Discovery and config**: the CLI loads plugin files from `.opencode/plugins/`
  (`core/src/plugin/source-directory.ts`; *verify* the directories and extensions), so
  `init`'s `.opencode/plugins/greenproof.js` needs no config entry. The npm package goes
  under `plugins` in `opencode.json` (a string or `{package, options}`).
- **Two copies in one process** (the npm package and `init`'s file, or a user-level and a
  project-level copy): each copy acts on its own, so two blocks would continue the run
  twice. Before running anything for an event, a copy claims the key `<event>:<session
  id>:<message id or "">` in a `Set` kept on `globalThis[Symbol.for("greenproof.opencode")]`
  and does nothing when the key is already claimed. The lock and replays of 6.2 still
  cover copies in different processes.
- **Top-level sessions only.** A session whose `ctx.session.get({sessionID})` has a
  `parentID` is a subagent and is skipped; the idle event carries no parent.
- **New top-level session**: 2.x has no session-start hook, and `ctx.event.subscribe` is
  lazy, so `session.created` is published before any plugin subscription is live and is
  never replayed. The plugin therefore uses `ctx.session.hook("prompt", fn)`: on the first
  prompt this process sees for a top-level session it claims `session-start:<session>:` (a
  later idle of the same session does too), then awaits `COMMAND session-start --hook
  opencode` with stdin `{"session_id", "cwd", "hook_event_name": "session.prompt",
  "source"}` and ignores stdout, so the marker exists before the model starts. `source` is
  `startup` when the session has no assistant message yet and `resume` otherwise; the
  callback never rejects, because a rejection fails the prompt. The AGENTS.md section from
  `init` tells the agent about the gate. A failed registration or read means no session
  start, the base falls back to the default branch, and the README says so.
- **Idle of a top-level session**: `ctx.session.hook("experimental.idle", fn)`, event
  `{sessionID, ...}`. The runner awaits it after the model's work and before the session
  goes idle, and continues the same run when the hook leaves input
  (`core/src/session/runner/llm.ts`); it does not fire after an interrupt or a failure.
  The name marks it experimental: when `ctx.session.hook` is missing or registering
  throws, log once and do nothing (fail open). Run `COMMAND check --hook opencode` with
  stdin `{"session_id", "cwd", "hook_event_name": "session.idle", "message_id"}`. `cwd`
  is the directory the plugin was loaded for (from `ctx.location`, *verify*);
  `message_id` is the id of the session's last assistant message, from
  `ctx.session.context({sessionID})` (*verify* the field names). No timestamps or random
  values: the fingerprint of 6.1 must match across copies and differ across stops. When
  the message id cannot be read, do not run the check (a payload without it would replay
  an earlier stop's decision) and log why. Map stdout:
  - empty → nothing;
  - `{"decision": "block", "reason"}` → `ctx.session.synthetic({sessionID, text:
    reason})` inside the hook, which continues the same run (the documented path; the
    built-in goal plugin uses it);
  - `{"continue": false, "stopReason"}` → `console.warn` the note (2.x gives plugins no
    toast or log API); also record it in the session with `ctx.session.synthetic({...,
    resume: false})` only if a real run shows that this adds a visible note without
    starting a turn;
  - a spawn error, a non-zero exit, invalid JSON or a timeout → fail open:
    `console.warn("greenproof could not run: ...")` and nothing else.
- At most one check per session at a time; an idle event during a running check for the
  same session is ignored. The child runs with `node:child_process` `spawn` (*verify* it
  works in the plugin runtime), without a shell, in the project directory, stdin the
  JSON, killed after 3600 seconds.
- Each block continues the run and fires the idle hook again; 6.2 bounds the loop.
- **npm package** `integrations/opencode/`: `package.json` (`name` `opencode-greenproof`,
  `version` equal to `__version__`, `type: module`, `main` `./index.js`, `exports`
  `{".": "./index.js", "./server": "./index.js"}` because the CLI resolves
  `exports["./server"]` first (`plugin/src/host.ts`), `files` index.js, README.md,
  LICENSE; keywords `opencode`, `opencode-plugin`, `greenproof`; license Apache-2.0;
  repository with `directory`), README (what runs, the uv requirement, OpenCode 2.x only,
  the `plugins` entry), LICENSE copy. `npm pack --dry-run` lists only those files.
  Publishing is the maintainer's step.
- **Tests**: `node --test` with a fake 2.x context and a fake greenproof executable
  (pass, block, release, error, timeout, child session, concurrent idle, two copies in one
  process, a missing message id, a context without `session.hook`), run in CI.
- **End-to-end** `scripts/e2e/opencode.py` (Python stdlib): a fake OpenAI-compatible
  model server (`POST /v1/chat/completions`, streamed SSE ending in `data: [DONE]` and
  plain JSON; it also answers title requests sent to `small_model`, and logs the last
  user message of every request) and the pinned CLI in a temporary git repository, with
  `HOME` and the `XDG_*` directories pointed at a temporary directory. Drive a headless
  session with what 2.x offers (`opencode run`, or `opencode serve` and its HTTP API);
  *verify* that the driver waits for a run the idle hook continued. The harness edits the
  files itself; the fake model makes no tool calls. Provider config (*verify* against the
  2.x config schema): `"provider": {"fake": {"npm": "@ai-sdk/openai-compatible",
  "options": {"baseURL": "http://127.0.0.1:PORT/v1", "apiKey": "x"}, "models":
  {"fake-model": {"tool_call": false, ...}}}}`, `"model"` and `"small_model"`
  `"fake/fake-model"`. It shows: a tampered tree is blocked and the fake model receives
  the greenproof message as the next input; the next idle with the same tree is released
  without another continuation; an honest change is not continued; with the npm copy and
  `init`'s file both loaded, one stop continues once. It needs the network for npx (and
  maybe for the provider package), so it is not part of pytest; it runs on the
  maintainer's machine and in a CI job with pinned versions.

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
   `CLAUDE.md` exists, cursor when `.cursor/` exists, codex when `.codex/` exists, opencode
   when `.opencode/`, `opencode.json` or `opencode.jsonc` exists (M5); never
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
  init/             detect.py, write_policy.py, agents_md.py, ci_github.py,
                    hooks/{claude,cursor,codex,gemini,opencode}.py, hooks/opencode_plugin.js
  schema/report-v1.json
tests/              pytest; fixture repos are built in tmp dirs by helpers (real git)
bench/              testwashing benchmark (milestone 4)
plugins/greenproof/ Claude Code, Codex and Cursor plugin (6.7)
integrations/opencode/  npm package opencode-greenproof (6.8)
scripts/bump_version.py
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
  source = "review probe"      # review probe | typia port | Skylos testwashing | ImpossibleBench | replay triage | control
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

**Replay harness** `bench/replay.py REPO [--n 200] [--select tests|runner] [--command CMD]
[--out PATH.tsv] [--details]`: on a temporary clone of REPO (never the original), for each of
the last N first-parent, non-merge commits whose diff touches a file greenproof classifies as
a test (with `--select runner`: a CI workflow, a test-runner or tool config, a `package.json`,
a Makefile or justfile, or a shell script): check out the parent, commit a policy as the base
(no commands, or `[commands] test = CMD` with `--command`, so the runner rules see the files
the command runs; static checks never run it), set index and worktree to the
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

## 14. M5: OpenCode, plugins and gaps (0.2.0)

Three workstreams, each on its own branch off `m5-spec`, merged by the maintainer:

- **M5a, runtime and init** (`m5a-runtime`): `--hook opencode` (6.1, 6.3); the payload
  fingerprint (6.1); the lock, the recorded decision and replays (6.2); `fork` in the
  SessionStart matchers (6.4; check Claude Code's documentation and Codex's source, and
  stop and report if either lacks it); suppression scoping (5.5); README "Agents" (a row
  for OpenCode pointing at 6.8, duplicates, the uv requirement of `uvx` hook commands);
  CHANGELOG `Unreleased`. Tests: a real concurrent run (two `check --hook claude`
  processes on one payload against a failing tree) gives two blocks with the same message
  and one block in the state; a later payload with the same tree releases; a 0.1.0 state
  file still releases; fingerprints of `{}` never replay; replays stop after 3; the lock
  is skipped when the git directory is read-only; suppression patterns do and do not
  fire per file kind (a `.toml` case file with `|| true` does not; `Makefile` does).
- **M5b, plugins** (`m5b-plugins`): everything in 6.7, including the icon,
  `scripts/bump_version.py`, `tests/test_plugins.py`, README "Plugins", CHANGELOG. Run
  `claude plugin validate --strict` on the plugin and the marketplace, cursor's
  validator, and the Codex checks; record the commands and their output in the final
  report. Add them to CI only if they run there without credentials, with pinned
  versions.
- **M5c, OpenCode 2.x** (`m5c-opencode`, off `main` after M5a and M5b are merged):
  everything in 6.8, `init --agents opencode` (6.4, section 7), the `node --test` job in
  CI with setup-node pinned to a full SHA, the end-to-end script under `scripts/e2e/`,
  README "OpenCode", CHANGELOG, and the OpenCode entries in `scripts/bump_version.py`.

Out of scope for M5: a Gemini or Antigravity extension; publishing anything.

Acceptance for the milestone: AGENTS.md's five checks pass; the plugin validators pass;
the OpenCode end-to-end script passes against the pinned `@opencode/cli` 2.x; a Claude Code end-to-end run
with the plugin loaded through `--plugin-dir` blocks a requested test skip and releases
the next stop; with the plugin and `init` hooks both installed, one stop gives one
decision. Then the maintainer bumps to 0.2.0 with `scripts/bump_version.py`, tags, and
publishes.
