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
        "A test file was deleted, or renamed to a path that is not a test file. Deleting the "
        "test is the simplest way to make a failing gate pass.",
    ),
    RuleInfo(
        "tests.removed",
        BLOCK,
        "A test function or case that exists at the base revision is gone and nothing "
        "replaced it, so the behaviour it checked is no longer checked.",
    ),
    RuleInfo(
        "tests.replaced",
        BLOCK,
        "In one file a test was removed and a different test was added (delete-and-replace). "
        "Both names are reported. Rename a test in its own change if that is what you mean.",
    ),
    RuleInfo(
        "tests.skipped",
        BLOCK,
        "A skip, xfail, ignore or todo marker was added to a test, or to collection "
        "configuration such as conftest.py. The test no longer runs, so it can no longer fail.",
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
        "assertion replaced by a weaker one, a wider tolerance, fewer parametrize cases, or an "
        "assertion wrapped in a try/except that swallows the failure.",
    ),
    RuleInfo(
        "tests.expected_changed",
        WARN,
        "The expected value in an assertion changed while non-test code also changed. That is "
        "legitimate when the specification changed, so it is a warning: confirm the new value "
        "comes from the requirement, not from what the code now returns.",
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
        "A change narrows or neuters how checks run: test selection, coverage thresholds, "
        "type-checker or linter strictness, or the scripts that run the tests.",
    ),
    RuleInfo(
        "ci.tampered",
        BLOCK,
        "A CI workflow change removes or neuters a test or lint step, adds continue-on-error, "
        "|| true or if: false, adds path filters that exclude tests, or changes the greenproof "
        "step itself.",
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
