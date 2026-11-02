"""The rule registry: the one place rule ids, default severities and descriptions live.

Policy validation (`[rules]` keys), `greenproof explain` and the report all read from here.
"""

from __future__ import annotations

from dataclasses import dataclass

from greenproof.findings import BLOCK, WARN


@dataclass(frozen=True)
class RuleInfo:
    id: str
    default_severity: str
    description: str


_RULES = [
    RuleInfo(
        "policy.changed",
        BLOCK,
        "The candidate's .greenproof.toml differs from the one at the base revision. The "
        "commands, test globs and severities for a run always come from the base policy, so "
        "an agent cannot weaken the gate in the same change that needs it weakened; a policy "
        "change is therefore reported for a human to review. Pass --allow-policy-change once "
        "the change has been reviewed.",
    ),
    RuleInfo(
        "tests.deleted",
        BLOCK,
        "A test file was deleted, or renamed to a path that is not a test file or that the "
        "test runner does not collect (for pytest: python_files from the base revision's "
        "configuration, else test_*.py and *_test.py). Deleting or hiding the test is the "
        "simplest way to make a failing gate pass.",
    ),
    RuleInfo(
        "tests.removed",
        BLOCK,
        "A test function or case that exists at the base revision is gone and is not covered: "
        "some of its assertions appear in no collected test of the candidate, so the behaviour "
        "it checked is no longer checked. A test moved to a file or name the runner does not "
        "collect counts as gone. Renamed, moved, split or merged tests whose assertions all "
        "survive are covered and do not fire.",
    ),
    RuleInfo(
        "tests.replaced",
        BLOCK,
        "In one file a test was removed and a different test was added (delete-and-replace), "
        "and the removed test is not covered: its assertions do not all survive in a collected "
        "test. Both names are reported. A rename with an identical or structurally identical "
        "body is clean, and so is splitting or merging tests that keep every assertion. "
        "Severity is warn when the new tests are at least as strong (a rewrite: they call "
        "every subject the removed tests called, with at least as many checks and no "
        "weaker check on a compared operand), block otherwise.",
    ),
    RuleInfo(
        "tests.skipped",
        BLOCK,
        "A skip, xfail, ignore or todo marker (or `__test__ = False`, or a skip in `pytestmark`) "
        "was added to a test that exists at the base revision, or at module, class or "
        "conftest scope. The test no longer runs, so it can no longer fail. On a brand-new "
        "test an unconditional skip or xfail is only a warning, and a conditional one "
        "(skipif with a condition, importorskip) does not fire: neither hides existing "
        "verification. In Go, a `//go:build` or `// +build` constraint added above "
        "the package clause of an existing `_test.go` file that had none blocks: `go test ./...` "
        "no longer compiles it. A constraint that differs from one the file already had is a "
        "warning. Adding `//go:build` over unchanged `// +build` lines (gofmt's migration) or "
        "dropping `// +build` lines under an unchanged `//go:build` gives nothing, and neither "
        "does a constraint on a brand-new test file.",
    ),
    RuleInfo(
        "tests.focused",
        BLOCK,
        "A focus marker (it.only, fit, describe.only, test.only) was added. It silently skips "
        "every other test in the run.",
    ),
    RuleInfo(
        "tests.weakened",
        BLOCK,
        "A test present on both sides checks less than before: fewer assertions, a strong "
        "assertion replaced by a weaker one, a wider tolerance, fewer parametrize cases, an "
        "assertion made vacuous (`assert True`, `assert [x]`, `... or True`, `x == x`, "
        "or `f(x) == f(x)` where it replaces a real comparison of `f(x)` in the same test; a "
        "determinism or singleton check that was already there, or is new, is a real check) "
        "or unreachable (after an unconditional return or raise, or under "
        "`if False`; in JS, Go and Rust a bare `return` at the top level of a test, a one-line "
        "`if (true) return;`, `if true { return }` or `if (x || true) return;` there, or an "
        "`if false`, `if (false)` or `... && false` block; a `return` that is the body of a "
        "brace-less `if`, `else`, `for` or `while` is conditional and does not count), an exact "
        "check loosened to a wide `pytest.approx`/`math.isclose`/`assertAlmostEqual` tolerance, "
        "or an assertion wrapped in a try/except or a swallowing decorator (one whose wrapper "
        "calls the test inside a try that catches AssertionError or Exception without "
        "re-raising; a retry or cleanup decorator is fine) that swallows the failure. In JS, Go "
        "and Rust, comparing a call-free expression with itself (`expect(X).toBe(X)`, "
        "`assert.Equal(t, X, X)`, `assert_eq!(X, X)`) is vacuous; an operand with a call "
        "(`expect(f(s)).toBe(f(s))`, a determinism or memoization check) is vacuous only when "
        "it replaces a comparison of the same expression with a different one at the base; "
        "Go's `got != got` NaN check is not vacuous. Rows removed from an `it.each`, "
        "`test.each` or `describe.each` table count as fewer cases; a table with a spread "
        "(`[...rows, [3, 4, 7]]`) is not a literal and is not counted. In Python a later def, del "
        "or rebinding of a test name shadows the earlier test, which Python never collects. A "
        "helper that checks counts as one check per call (Python: helpers imported from other "
        "test modules, up to 8 calls deep, and from non-test modules that import pytest, "
        "unittest or hypothesis or sit under a testing, test_utils or pytest_plugin path; JS: "
        "same file; Go: same directory), so a helper or conftest that loses its checks "
        "weakens the tests that use it: only a helper that lost checks pulls in its importers, "
        "and at most 300 files are read and 200 compared, with no note when that cap is hit. "
        "Replacing a comparison with its own "
        "operand "
        "(`assert f() == 1` to `assert f()`) blocks; replacing it with a different expression "
        "is a warning, because that is often a legitimate refactor.",
    ),
    RuleInfo(
        "tests.expected_changed",
        WARN,
        "The expected value in an assertion, or the literal a `@pytest.fixture` returns, changed, "
        "whether or not non-test code changed. That is "
        "legitimate when the specification changed, so it is a warning: confirm the new value "
        "comes from the requirement, not from what the code returns. A modified expected-output "
        "data file (.json, .yaml, .yml, .txt, .csv, .snap, .golden, .out, .expected, .html, "
        ".xml) under a test path or a fixture directory (testdata, fixtures, golden, "
        "__snapshots__), modified or renamed with changed content, is reported the same way, "
        "once per file. Dependency, container and tool configuration there (requirements*.txt, "
        "constraints*.txt, *compose*.yml/.yaml, Dockerfile*, .pre-commit*, codecov.yml, .env*) "
        "is not expected output.",
    ),
    RuleInfo(
        "suppress.added",
        WARN,
        "An added line introduces a type, lint or coverage suppression (type: ignore, noqa, "
        "pragma: no cover, ts-ignore, eslint-disable, || true and similar). Moved code that "
        "already existed in the file at base is not counted.",
    ),
    RuleInfo(
        "runner.tampered",
        BLOCK,
        "A change narrows or neuters how checks run: test selection or collection patterns, "
        "coverage thresholds (lowered or removed), type-checker or linter strictness, the "
        "scripts that run the tests (including a swallowed failure such as `|| true` or "
        "`; exit 0` after a test, lint or type-check command, and scripts or files a policy "
        "command runs, one level deep), a conftest or plugin hook that can rewrite outcomes or "
        "collection, a Go TestMain that always exits 0 or never runs the tests (a conditional "
        "exit is a warning), or os._exit / sys.exit(0) added to a test file.",
    ),
    RuleInfo(
        "ci.tampered",
        BLOCK,
        "A CI workflow change removes or neuters a test or lint step (judged across all "
        "workflow files, looking through local composite actions, local scripts and launchers "
        "such as docker compose run or uv run), adds continue-on-error: true or if: false to a "
        "step that runs a check, swallows a check's failure (|| true, || echo, ; exit 0, "
        "set +e, or under bash {0}, pwsh and cmd a check whose status is lost), runs a check "
        "with --collect-only or --passWithNoTests, exits 0 before the first check, adds a "
        "selection flag (-k, -m, --deselect, -run, -short, -t), narrows the triggers (fewer "
        "events, or a paths or paths-ignore filter that hides a test or source file), or "
        "changes the greenproof step, its with: inputs included. The selection-flag, "
        "collect-only, early exit and set +e checks read GitHub Actions run steps only; "
        "GitLab CI and CircleCI get the line-level swallow check. Narrowed branch, tag or "
        "type filters, a matrix-chosen selection flag, continue-on-error with an expression, "
        "fewer steps of a kind that still exists, a replacement the rule cannot classify, and "
        "a ref-only version bump of the greenproof action are warnings.",
    ),
    RuleInfo(
        "static.parse_error",
        WARN,
        "A changed file in a parsed language (Python, TOML, JSON, INI) does not parse. "
        "greenproof falls back to line-level checks for that file, which see less than the "
        "structural ones, so fix the file.",
    ),
    RuleInfo(
        "tests.self_mocked",
        WARN,
        "An added or changed test patches (monkeypatch.setattr, mock.patch, patch.object, "
        "jest.mock, vi.mock, a mocked spyOn) the same callable its assertions call, so the "
        "assertion checks the mock instead of the code. Patching a collaborator is fine; "
        "patching the subject of the test is not. A patch that was already in the same test "
        "at the base does not fire. An added or changed autouse fixture that patches a "
        "callable the tests in its scope assert on fires too.",
    ),
    RuleInfo(
        "code.hardcoded",
        WARN,
        "Non-test code gained a literal lookup table or an equality branch on a literal that "
        "also appears in a test or fixture changed in the same diff, which is how code is "
        "made to pass a test without implementing the behaviour.",
    ),
    RuleInfo(
        "revert.passes_without_change",
        WARN,
        "Every test changed or added in this diff also passes against the base source with "
        "only the test files overlaid, so the tests do not exercise the source change.",
    ),
    RuleInfo(
        "smoke.failed",
        BLOCK,
        "The smoke check failed: the built artifact did not become ready at ready_url within "
        "the timeout, or a probe command failed.",
    ),
]

RULES: dict[str, RuleInfo] = {r.id: r for r in _RULES}


def rule_ids() -> list[str]:
    return [r.id for r in _RULES]
