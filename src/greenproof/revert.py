"""The revert check (SPEC 8.1): do the added or changed tests pass without the source change?

Build the base tree with every added or modified test file overlaid at its candidate version,
check it out in a temporary worktree, run `setup`, then run only the selected tests. If they
all pass on the base source, they do not exercise the change.
"""

from __future__ import annotations

import json
import posixpath
import re
import shlex
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from greenproof import gitops
from greenproof.changeset import ChangeSet
from greenproof.errors import UsageError
from greenproof.findings import Finding
from greenproof.policy import Policy
from greenproof.rules._common import CODE_SUFFIXES, JS_SUFFIXES, finding
from greenproof.rules.collect import collection_from_base
from greenproof.rules.tests_integrity import analyse, pair_units
from greenproof.rules.tests_model import Unit
from greenproof.runner import TAIL_LINES, run_command

RULE = "revert.passes_without_change"
_SCRIPT_RUNNERS = ("npm", "pnpm", "yarn")  # `<pm> test`; bun's `bun test` is bun's own runner
# pytest reports an import or syntax error in a selected file as "errors during collection"
# (exit 2) or, when node ids select the file, as an `ERROR <file>` summary line (exit 4).
_COLLECTION_ERROR = re.compile(
    r"errors? during collection|ERROR collecting|while importing test module|^ERROR \S+\.py",
    re.IGNORECASE | re.MULTILINE,
)
_ENV_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_SHOWN_TESTS = 5
READONLY_SKIP = "the git directory is read-only here; the stop hook runs this check"


@dataclass(frozen=True)
class RevertOutcome:
    """`finding` is set when the tests pass on the base code. `note` explains a skip."""

    skipped: bool
    note: str | None = None
    finding: Finding | None = None


@dataclass(frozen=True)
class Plan:
    """What to run on the overlaid base tree."""

    command: str
    runner: str  # pytest, js or go
    overlay: list[str]
    tests: list[str]  # human-readable names, for the finding message


@dataclass
class _Selection:
    overlay: list[str] = field(default_factory=list)
    py_units: list[Unit] = field(default_factory=list)
    go_units: list[Unit] = field(default_factory=list)
    js_files: list[str] = field(default_factory=list)


def _skip(note: str) -> RevertOutcome:
    return RevertOutcome(True, note)


def _changed_tests(changes: ChangeSet) -> _Selection:
    """Added or modified test files, and within them the tests added or whose body changed."""
    sel = _Selection()
    base_units: list[Unit] = []
    cand_units: list[Unit] = []
    cfg = collection_from_base(changes)
    for f in changes.tests():
        if f.was_test and f.old_text is not None:
            base = analyse(f.old_path or f.path, f.old_text)
            if base is not None:
                base_units.extend(u for u in base.units if cfg.unit_collected(u))
        if f.is_test and f.status != "D" and f.new_text is not None:
            sel.overlay.append(f.path)
            if f.path.endswith(JS_SUFFIXES):
                sel.js_files.append(f.path)
            cand = analyse(f.path, f.new_text)
            if cand is None:
                continue
            if cand.engine.startswith("python") and not cfg.file_collected(f.path):
                continue
            cand_units.extend(u for u in cand.units if cfg.unit_collected(u))
    pairing = pair_units(base_units, cand_units)
    changed = [*pairing.left_cand, *(c for b, c in pairing.pairs if b.body_hash != c.body_hash)]
    for u in sorted(changed, key=lambda u: (u.path, u.line)):
        if u.engine.startswith("python") and u.path.endswith(".py"):
            sel.py_units.append(u)
        elif u.engine == "go" and u.path.endswith("_test.go") and u.name.startswith("Test"):
            sel.go_units.append(u)
    return sel


def _words(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def _is_make_or_just(command: str) -> bool:
    words = [w for w in _words(command) if not _ENV_ASSIGNMENT.match(w)]
    return bool(words) and posixpath.basename(words[0]) in ("make", "gmake", "just")


def _package_script(package_json: bytes | None) -> str:
    if package_json is None:
        return ""
    try:
        data = json.loads(package_json)
    except ValueError:
        return ""
    scripts = data.get("scripts") if isinstance(data, dict) else None
    script = scripts.get("test") if isinstance(scripts, dict) else None
    return script if isinstance(script, str) else ""


def _is_js_runner(text: str) -> bool:
    return "jest" in text or "vitest" in text


def _is_pm_test(command: str) -> bool:
    words = _words(command)
    if len(words) == 2 and words[0] in _SCRIPT_RUNNERS and words[1] == "test":
        return True
    return (
        len(words) == 3 and words[0] in (*_SCRIPT_RUNNERS, "bun") and words[1:] == ["run", "test"]
    )


def _names(tests: list[str]) -> str:
    shown = ", ".join(tests[:_SHOWN_TESTS])
    extra = len(tests) - _SHOWN_TESTS
    return shown + (f" and {extra} more" if extra > 0 else "")


def _node_id(unit: Unit) -> str:
    return f"{unit.path}::{unit.key}"


def build_plan(
    changes: ChangeSet, test_command: str, package_json: bytes | None
) -> Plan | RevertOutcome:
    """The command line and overlay for the selected tests, or why the check is skipped."""
    if not any(f.is_test and f.status != "D" for f in changes.tests()):
        return _skip("the change adds or modifies no test files")
    if not any(f.path.endswith(CODE_SUFFIXES) for f in changes.non_tests()):
        return _skip("the change modifies no non-test source code")
    command = test_command.strip()
    if not command:
        return _skip("no commands.test is configured, so there is nothing to run")
    sel = _changed_tests(changes)
    if _is_make_or_just(command):
        return _skip("commands.test is a make or just target; the changed tests cannot be selected")
    if "pytest" in command:
        if not sel.py_units:
            return _skip("no added or changed pytest tests to select")
        ids = list(dict.fromkeys(_node_id(u) for u in sel.py_units))
        line = f"{command} {' '.join(shlex.quote(i) for i in ids)}"
        return Plan(line, "pytest", sel.overlay, ids)
    via_script = _is_pm_test(command) and _is_js_runner(_package_script(package_json))
    if _is_js_runner(command) or via_script:
        if not sel.js_files:
            return _skip("no added or modified JavaScript or TypeScript test files to select")
        files = " ".join(shlex.quote(p) for p in sel.js_files)
        line = f"{command} -- {files}" if via_script else f"{command} {files}"
        return Plan(line, "js", sel.overlay, list(sel.js_files))
    if "go test" in command:
        if not sel.go_units:
            return _skip("no added or changed Go tests to select")
        names = list(dict.fromkeys(u.name for u in sel.go_units))
        dirs = list(dict.fromkeys(posixpath.dirname(u.path) or "." for u in sel.go_units))
        packages = " ".join(shlex.quote(d if d == "." else f"./{d}") for d in dirs)
        pattern = shlex.quote(f"^({'|'.join(names)})$")
        return Plan(f"go test {packages} -run {pattern}", "go", sel.overlay, names)
    return _skip(
        f"unsupported test runner in commands.test ({command!r}); "
        "supported: pytest, jest, vitest and go test"
    )


def overlay_tree(repo: Path, base_sha: str, tree: str, paths: list[str]) -> str:
    """The base tree with each of `paths` at its version in `tree`, built in a temporary index."""
    listing = gitops.git(repo, "ls-tree", "-z", tree, "--", *paths).stdout.decode(
        errors="surrogateescape"
    )
    with tempfile.TemporaryDirectory(prefix="greenproof-revert-") as tmp:
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        gitops.git(repo, "read-tree", base_sha, env=env)
        for entry in filter(None, listing.split("\0")):
            meta, _, path = entry.partition("\t")
            mode, _kind, sha = meta.split(" ")
            gitops.git(
                repo, "update-index", "--add", "--cacheinfo", f"{mode},{sha},{path}", env=env
            )
        return gitops.git(repo, "write-tree", env=env).stdout.decode().strip()


def _verdict(plan: Plan, exit_code: int, timed_out: bool, tail: str) -> RevertOutcome:
    if timed_out:
        return _skip("the selected tests timed out on the base code, which says nothing about them")
    if exit_code == 0:
        names = _names(plan.tests)
        return RevertOutcome(
            False,
            finding=finding(
                RULE,
                None,
                None,
                f"the added or changed tests pass on the base code ({names}); "
                "they may not exercise the change",
                "Assert something that only holds with the source change, or confirm that "
                "another test covers it.",
            ),
        )
    if exit_code == 1:
        return RevertOutcome(False)
    if plan.runner == "pytest" and exit_code in (2, 4) and _COLLECTION_ERROR.search(tail):
        return _skip(
            "the changed tests do not collect on the base code; "
            "they likely import what the change adds"
        )
    return _skip(f"the selected tests gave no verdict on the base code (exit {exit_code})")


def run_revert(
    repo: Path,
    base_sha: str,
    tree: str,
    changes: ChangeSet,
    policy: Policy,
    tail_lines: int = TAIL_LINES,
) -> RevertOutcome:
    plan = build_plan(
        changes, policy.commands.get("test", ""), gitops.read_blob(repo, base_sha, "package.json")
    )
    if isinstance(plan, RevertOutcome):
        return plan
    if gitops.readonly_git():  # the check needs a worktree, which needs a writable git dir
        return _skip(READONLY_SKIP)
    try:
        overlaid = overlay_tree(repo, base_sha, tree, plan.overlay)
    except UsageError as exc:
        return _skip(f"could not build the base tree with the changed tests: {exc}")
    with gitops.worktree_for_tree(repo, overlaid) as work:
        setup = policy.commands.get("setup", "").strip()
        if setup:
            done = run_command("setup", setup, work, policy.timeout, tail_lines)
            if done.failed:
                why = "timed out" if done.timed_out else f"exit {done.exit_code}"
                return _skip(f"setup failed in the revert worktree ({why})")
        result = run_command("revert", plan.command, work, policy.timeout, tail_lines)
    return _verdict(plan, result.exit_code, result.timed_out, result.output_tail)
