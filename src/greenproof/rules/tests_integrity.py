"""Test-integrity rules: tests.deleted, removed, replaced, skipped, focused, weakened and
expected_changed (SPEC 5.2 to 5.4).

Identity: a test is `key` (its qualified name without the path) plus a normalised body hash.
Base and candidate tests are paired first by path and key, then by key anywhere (a move), then
by body hash (a rename); only what is left over counts as removed, replaced or deleted.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from greenproof.changeset import ChangedFile, ChangeSet
from greenproof.findings import BLOCK, WARN, Finding
from greenproof.rules._common import CODE_SUFFIXES, finding, language_of, sev_finding
from greenproof.rules.collect import Collection, collection_from_base
from greenproof.rules.self_mocked import check_self_mocked
from greenproof.rules.tests_lines import analyse_lines
from greenproof.rules.tests_model import FileAnalysis, Marker, Unit
from greenproof.rules.tests_python import analyse_python, marker_display

_RUST_TEST_ATTR = re.compile(r"#\[\s*(?:\w+::)*test\b")


def analyse(path: str, text: str) -> FileAnalysis | None:
    lang = language_of(path)
    if lang is None:
        return None
    if lang == "python":
        return analyse_python(path, text) or analyse_lines(path, text, "python-lines")
    return analyse_lines(path, text, lang)


def _is_test_side(path: str, text: str | None, flagged: bool) -> bool:
    if text is None:
        return False
    if flagged:
        return True
    return path.endswith(".rs") and _RUST_TEST_ATTR.search(text) is not None


@dataclass
class _Side:
    base: FileAnalysis | None
    cand: FileAnalysis | None
    file: ChangedFile


@dataclass
class _Pairing:
    pairs: list[tuple[Unit, Unit]] = field(default_factory=list)
    left_base: list[Unit] = field(default_factory=list)
    left_cand: list[Unit] = field(default_factory=list)


def _leaf(u: Unit) -> str:
    """The last segment of a test's qualified name: the function, or the JS/TS test title."""
    if u.engine.startswith("python"):
        return u.key.rsplit("::", 1)[-1]
    if u.engine == "js":
        return u.key.rsplit(" > ", 1)[-1]
    return u.key


def pair_units(base: list[Unit], cand: list[Unit]) -> _Pairing:
    result = _Pairing()
    free = list(range(len(cand)))
    unmatched: list[Unit] = []

    def take(predicate_index: int) -> Unit:
        free.remove(predicate_index)
        return cand[predicate_index]

    for b in base:  # 1. same path and key
        hit = next((i for i in free if cand[i].path == b.path and cand[i].key == b.key), None)
        if hit is None:
            unmatched.append(b)
        else:
            result.pairs.append((b, take(hit)))
    # 2. same path and leaf name, unique among the still-unpaired tests of the file on both
    # sides: a test moved between classes or describe blocks, or a describe title edited.
    base_leaves = Counter((b.path, _leaf(b)) for b in unmatched)
    cand_leaves = Counter((cand[i].path, _leaf(cand[i])) for i in free)
    unpaired: list[Unit] = []
    for b in unmatched:
        ident = (b.path, _leaf(b))
        if base_leaves[ident] == 1 and cand_leaves[ident] == 1:
            result.pairs.append(
                (b, take(next(i for i in free if (cand[i].path, _leaf(cand[i])) == ident)))
            )
        else:
            unpaired.append(b)
    remaining: list[Unit] = []
    for b in unpaired:  # 3. same key, another path: a move
        hits = [i for i in free if cand[i].key == b.key]
        hits.sort(key=lambda i: cand[i].body_hash != b.body_hash)
        if hits:
            result.pairs.append((b, take(hits[0])))
        else:
            remaining.append(b)
    leftover: list[Unit] = []
    for b in remaining:  # 4. same body, another name: a rename
        hit = next((i for i in free if cand[i].body_hash == b.body_hash), None)
        if hit is None:
            leftover.append(b)
        else:
            result.pairs.append((b, take(hit)))
    # 5. Same structure (identifiers alpha-renamed), only when the match is one-to-one.
    base_counts = Counter(b.struct_hash for b in leftover if b.struct_hash)
    cand_counts = Counter(cand[i].struct_hash for i in free if cand[i].struct_hash)
    for b in leftover:
        h = b.struct_hash
        if h and base_counts[h] == 1 and cand_counts[h] == 1:
            result.pairs.append((b, take(next(i for i in free if cand[i].struct_hash == h))))
        else:
            result.left_base.append(b)
    result.left_cand = [cand[i] for i in free]
    return result


def _added_forms(pairing: _Pairing) -> set[str]:
    """Exact assertion forms the change adds: new tests, and what each paired test gained."""
    universe = {form for u in pairing.left_cand for form in u.assertion_forms}
    for b, c in pairing.pairs:
        universe.update((Counter(c.assertion_forms) - Counter(b.assertion_forms)).keys())
    return universe


def _covered(unit: Unit, universe: set[str]) -> bool:
    """Every assertion of `unit` reappears, in exact form, among the assertions the change adds."""
    return bool(unit.assertion_forms) and all(f in universe for f in unit.assertion_forms)


_Reason = tuple[str, int, str]  # text, line, severity


def _effective(b: Unit, c: Unit) -> tuple[int, int]:
    """Effective check counts (assertions x parametrize cases); without the case factor when
    either side has a non-literal argvalues (SPEC 5.4)."""
    if b.param_literal and c.param_literal:
        return b.asserts * (b.param_cases or 1), c.asserts * (c.param_cases or 1)
    return b.asserts, c.asserts


def _added_cases(pairing: _Pairing) -> set[str]:
    """Exact parametrize cases the change adds: those of new tests, and each paired test's gain."""
    universe = {form for u in pairing.left_cand for form in u.param_forms}
    for b, c in pairing.pairs:
        universe.update((Counter(c.param_forms) - Counter(b.param_forms)).keys())
    return universe


@dataclass
class _Added:
    forms: set[str]
    cases: set[str]


def _loosened(b: Unit, c: Unit) -> list[tuple[str, str]]:
    """(subject, new assertion) for each subject of an exact check at the base that the candidate
    no longer pins exactly but still checks loosely (SPEC 5.4)."""
    return [
        (source, c.loose_targets[shape])
        for shape, source in b.exact_subjects.items()
        if shape not in c.exact_subjects and shape in c.loose_targets
    ]


def _weak_reasons(b: Unit, c: Unit, count_drop_allowed: bool, added: _Added) -> list[_Reason]:
    if b.engine != c.engine:
        return []
    out: list[_Reason] = []
    if b.engine == "python":
        before, after = _effective(b, c)
        if after < before and not set(b.assertion_forms) <= set(c.assertion_forms) | added.forms:
            out.append((f"checks dropped from {before} to {after}", c.line, BLOCK))
    elif count_drop_allowed and c.asserts < b.asserts:
        out.append((f"assertions dropped from {b.asserts} to {c.asserts}", c.line, BLOCK))
    if c.vacuous > b.vacuous:
        out.append(
            ("an assertion was made vacuous: it can never fail", c.vacuous_line or c.line, BLOCK)
        )
    if c.unreachable > b.unreachable:
        out.append(
            (
                "an assertion was made unreachable (after a return or raise, or under `if False`)",
                c.unreachable_line or c.line,
                BLOCK,
            )
        )
    loosened = _loosened(b, c) if b.engine == "python" else []
    out.extend((f"exact check on `{x}` loosened to `{src}`", c.line, BLOCK) for x, src in loosened)
    if c.strong < b.strong and c.weak > b.weak and not loosened:
        # Cutting a comparison to a truthiness check on its own operand is a plain weakening
        # (an exact check loosened is reported above); a different expression is often a
        # legitimate refactor, so on Python it only warns.
        own = bool(c.weak_operands & b.strong_operands) or b.engine != "python"
        out.append(
            (
                "a strong assertion was replaced by a weaker one",
                c.line,
                BLOCK if own else WARN,
            )
        )
    if c.broad_raises > b.broad_raises and c.specific_raises < b.specific_raises:
        out.append(("a specific expected exception was widened to Exception", c.line, BLOCK))
    for kind in ("rel", "abs", "delta"):
        if (
            kind in b.tolerances
            and kind in c.tolerances
            and c.tolerances[kind] > b.tolerances[kind]
        ):
            out.append((f"tolerance `{kind}` widened to {c.tolerances[kind]:g}", c.line, BLOCK))
    places = (b.tolerances.get("places"), c.tolerances.get("places"))
    if places[0] is not None and places[1] is not None and places[1] < places[0]:
        out.append((f"`places` lowered to {places[1]:g}", c.line, BLOCK))
    if (
        b.param_cases is not None
        and c.param_cases is not None
        and c.param_cases < b.param_cases
        and not set(b.param_forms) <= set(c.param_forms) | added.cases
    ):
        out.append(
            (
                f"parametrize cases reduced from {b.param_cases} to {c.param_cases}",
                c.line,
                BLOCK,
            )
        )
    if c.swallowed > b.swallowed:
        out.append(
            (
                "an assertion sits inside a try/except that swallows the failure",
                c.swallow_line or c.line,
                BLOCK,
            )
        )
    return out


def _expected_change(b: Unit, c: Unit) -> int | None:
    """Line of the first changed expected literal when the number of literals is unchanged."""
    if b.engine != c.engine or len(b.expected) != len(c.expected) or not c.expected:
        return None
    before = Counter(lit for lit, _ in b.expected)
    if before == Counter(lit for lit, _ in c.expected):
        return None
    for lit, line in c.expected:
        if before[lit] > 0:
            before[lit] -= 1
        else:
            return line
    return None  # pragma: no cover - unreachable: the multisets differ


def _target(scope: str) -> str:
    if scope.endswith("::<module>"):
        return "the module"
    return scope.rsplit("::", 1)[-1] if "::" in scope else scope


def _marker_findings(sides: list[_Side], base_keys: set[str], cand_keys: set[str]) -> list[Finding]:
    remaining: Counter[tuple[str, str]] = Counter()
    for s in sides:
        if s.base:
            remaining.update((m.scope, m.kind) for m in s.base.markers)
    out: list[Finding] = []
    for s in sides:
        if not s.cand:
            continue
        for m in s.cand.markers:
            ident = (m.scope, m.kind)
            if remaining[ident] > 0:
                remaining[ident] -= 1
                continue
            # A skip on a brand-new test hides no existing verification: a conditional one is
            # fine, an unconditional one only warns. Module, class and conftest scope always
            # block, except a conditional skip in a brand-new file (SPEC 5.3).
            fresh = m.scope in cand_keys and m.scope not in base_keys
            skip = m.rule == "tests.skipped"
            if skip and m.conditional and (fresh or s.base is None):
                continue
            out.append(_marker_finding(s.cand.path, m, warn=fresh and skip))
    return out


def _marker_finding(path: str, m: Marker, warn: bool = False) -> Finding:
    text = marker_display(m.text)
    where = _target(m.scope)
    if m.rule == "tests.focused":
        return finding(
            "tests.focused",
            path,
            m.line,
            f"focus marker `{text}` added to {where}; every other test in the run is skipped",
            "Remove the focus marker so the whole suite runs.",
        )
    return sev_finding(
        "tests.skipped",
        WARN if warn else BLOCK,
        path,
        m.line,
        f"`{text}` added to {where}",
        f"Fix the code so {where} passes instead of skipping it; if the test is wrong, say so.",
    )


def check_tests(changes: ChangeSet) -> list[Finding]:
    sides: list[_Side] = []
    for f in changes.files:
        old_path = f.old_path or f.path
        base = cand = None
        if _is_test_side(old_path, f.old_text, f.was_test) and f.old_text is not None:
            base = analyse(f.path, f.old_text)
        if _is_test_side(f.path, f.new_text, f.is_test) and f.new_text is not None:
            cand = analyse(f.path, f.new_text)
        if base is None and cand is None:
            continue
        if cand is None and base is not None and f.status not in ("D", "R"):
            cand = FileAnalysis(f.path, base.engine)  # the file stopped being a test file
        sides.append(_Side(base, cand, f))

    cfg = collection_from_base(changes)
    for s in sides:
        for analysis, real_path in (
            (s.base, s.file.old_path or s.file.path),
            (s.cand, s.file.path),
        ):
            if analysis is not None and analysis.engine.startswith("python"):
                analysis.collected = cfg.file_collected(real_path)
    base_units = _collected(sides, cfg, base=True)
    cand_units = _collected(sides, cfg, base=False)
    pairing = pair_units(base_units, cand_units)
    universe = _added_forms(pairing)
    twins = {(u.body_hash, tuple(sorted(u.param_forms))) for u in cand_units}
    pairing.left_base = [
        u
        for u in pairing.left_base
        if not _covered(u, universe) and (u.body_hash, tuple(sorted(u.param_forms))) not in twins
    ]
    out: list[Finding] = []
    out.extend(_deleted_and_removed(sides, pairing))
    out.extend(_weakened(sides, pairing, changes))
    base_keys = {u.key for s in sides if s.base for u in s.base.units}
    cand_keys = {u.key for s in sides if s.cand for u in s.cand.units}
    out.extend(_marker_findings(sides, base_keys, cand_keys))
    out.extend(
        check_self_mocked(
            [(None, u) for u in pairing.left_cand] + [(b, c) for b, c in pairing.pairs],
            {s.base.path: s.base for s in sides if s.base},
            {s.cand.path: s.cand for s in sides if s.cand},
            changes,
        )
    )
    return out


def _collected(sides: list[_Side], cfg: Collection, base: bool) -> list[Unit]:
    """The tests the runner would collect on one side."""
    out: list[Unit] = []
    for s in sides:
        analysis = s.base if base else s.cand
        if analysis is None or not analysis.collected:
            continue
        out.extend(u for u in analysis.units if cfg.unit_collected(u))
    return out


def _group_checks(units: list[Unit], literal: bool) -> int:
    return sum(u.asserts * ((u.param_cases or 1) if literal else 1) for u in units)


def _group_pinned(units: list[Unit], literal: bool) -> int:
    return sum(u.pinned * ((u.param_cases or 1) if literal else 1) for u in units)


def _is_rewrite(removed: list[Unit], added: list[Unit]) -> bool:
    """SPEC 5.7: the new tests keep every subject, at least as many checks, loosen no exact check
    and pin at least as many expected values as the removed tests."""
    if any(u.engine != "python" for u in (*removed, *added)):
        return False
    subjects = set().union(*(u.subjects for u in removed))
    if not subjects <= set().union(*(u.subjects for u in added)):
        return False
    literal = all(u.param_literal for u in (*removed, *added))
    if _group_checks(added, literal) < _group_checks(removed, literal):
        return False
    exact_after = set().union(*(u.exact_subjects.keys() for u in added))
    checked_after = exact_after | set().union(*(u.loose_targets.keys() for u in added))
    for u in removed:
        if any(s in checked_after and s not in exact_after for s in u.exact_subjects):
            return False
    return _group_pinned(added, literal) >= _group_pinned(removed, literal)


def _absorbing(pairing: _Pairing, path: str) -> list[tuple[Unit, Unit]]:
    """Paired Python tests of `path` whose effective check count grew (SPEC 5.7): removed tests
    were often folded into them."""
    found = []
    for b, c in pairing.pairs:
        if c.path == path and b.engine == c.engine == "python":
            before, after = _effective(b, c)
            if after > before:
                found.append((b, c))
    return found


def _deleted_and_removed(sides: list[_Side], pairing: _Pairing) -> list[Finding]:
    out: list[Finding] = []
    lost = {id(u) for u in pairing.left_base}
    added_by_path: dict[str, list[Unit]] = {}
    for u in pairing.left_cand:
        added_by_path.setdefault(u.path, []).append(u)
    for s in sides:
        if s.base is None:
            continue
        path = s.file.path
        names = [u for u in s.base.units if id(u) in lost]
        # Deleted, or renamed to a path that is not a test file or that the runner skips.
        if s.cand is None or (not s.cand.collected and s.file.status == "R"):
            if names:  # a file with no test units (helpers, fixtures) loses nothing (SPEC 5.7)
                listed = ", ".join(u.name for u in names[:5])
                more = f" and {len(names) - 5} more" if len(names) > 5 else ""
                what = "deleted" if s.file.status == "D" else "renamed to a non-test path"
                out.append(
                    finding(
                        "tests.deleted",
                        path,
                        None,
                        f"test file {s.file.old_path or path} {what} (lost: {listed}{more})",
                        "Restore the test file; tests that moved must move unchanged.",
                    )
                )
            continue
        added = added_by_path.get(path, [])
        absorbing = _absorbing(pairing, path) if names else []
        if added or absorbing:
            # No counted check at the base (type-only test, "does not throw" smoke test):
            # nothing to lose, so a file that gained tests only warns (SPEC 5.7).
            at = added[0].line if added else absorbing[0][1].line
            for removed in (u for u in names if u.asserts == 0):
                out.append(
                    sev_finding(
                        "tests.replaced",
                        WARN,
                        path,
                        at,
                        f"test {removed.name} was removed; it had no counted check "
                        "and the file gained tests",
                        "Keep the original test; add new tests alongside it, not in its place.",
                    )
                )
            names = [u for u in names if u.asserts > 0]
        if absorbing:
            rewrite = _is_rewrite(
                [*names, *(b for b, _ in absorbing)], [*added, *(c for _, c in absorbing)]
            )
            folded = ", ".join(c.name for _, c in absorbing)
        else:
            rewrite = bool(names and added) and _is_rewrite(names, added)
            folded = ""
        for i, removed in enumerate(names):
            if rewrite and folded:
                line = added[min(i, len(added) - 1)].line if added else absorbing[0][1].line
                what = f"test {removed.name} was removed and folded into {folded}"
            elif i < len(added):
                line = added[i].line
                what = f"test {removed.name} was removed and a different test {added[i].name} added"
            elif rewrite:
                line = added[-1].line
                what = (
                    f"test {removed.name} was removed and a different test {added[-1].name} added"
                )
            else:
                break
            out.append(
                sev_finding(
                    "tests.replaced",
                    WARN if rewrite else BLOCK,
                    path,
                    line,
                    what
                    + (" (a rewrite: same subjects, at least as many checks)" if rewrite else ""),
                    "Keep the original test; add new tests alongside it, not in its place.",
                )
            )
        for removed in [] if rewrite else names[len(added) :]:
            out.append(
                finding(
                    "tests.removed",
                    path,
                    None,
                    f"test {removed.name} was removed and nothing replaced it",
                    "Restore the test, or say which requirement made it obsolete.",
                )
            )
    return out


def _weakened(sides: list[_Side], pairing: _Pairing, changes: ChangeSet) -> list[Finding]:
    out: list[Finding] = []
    added = _Added(_added_forms(pairing), _added_cases(pairing))
    added_asserts: dict[str, int] = {}
    for u in pairing.left_cand:
        added_asserts[u.path] = added_asserts.get(u.path, 0) + u.asserts
    drops: dict[str, int] = {}
    for b, c in pairing.pairs:
        drops[c.path] = drops.get(c.path, 0) + max(0, b.asserts - c.asserts)
    code_changed = any(f.path.endswith(CODE_SUFFIXES) for f in changes.non_tests())
    for b, c in pairing.pairs:
        # A test split in two keeps its assertions in the new test: not a weakening.
        allowed = added_asserts.get(c.path, 0) < drops.get(c.path, 0)
        reasons = _weak_reasons(b, c, allowed, added)
        if reasons:
            text = "; ".join(r for r, _, _ in reasons)
            severity = BLOCK if any(sev == BLOCK for _, _, sev in reasons) else WARN
            out.append(
                sev_finding(
                    "tests.weakened",
                    severity,
                    c.path,
                    reasons[0][1],
                    f"test {c.name} checks less than at the base: {text}",
                    "Restore the original assertions and fix the code instead.",
                )
            )
        line = _expected_change(b, c)
        if line is not None:
            if code_changed:
                what = f"expected value changed in test {c.name} while non-test code changed"
                hint = (
                    "Confirm the new expected value comes from the requirement, not from "
                    "what the code now returns."
                )
            else:
                what = f"expected value changed in test {c.name} and no non-test code changed"
                hint = (
                    "A test edited to match what the code returns hides the bug; "
                    "confirm the old value was wrong."
                )
            out.append(finding("tests.expected_changed", c.path, line, what, hint))
    return out
