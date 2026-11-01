"""Test-integrity rules: tests.deleted, removed, replaced, skipped, focused, weakened and
expected_changed (SPEC 5.2 to 5.4).

Identity: a test is `key` (its qualified name without the path) plus a normalised body hash.
Base and candidate tests are paired first by path and key, then by key anywhere (a move), then
by body hash (a rename); only what is left over counts as removed, replaced or deleted.
"""

from __future__ import annotations

import posixpath
import re
from collections import Counter
from dataclasses import dataclass, field

from greenproof.changeset import ChangedFile, ChangeSet
from greenproof.findings import Finding
from greenproof.rules._common import CODE_SUFFIXES, finding, language_of
from greenproof.rules.tests_lines import analyse_lines
from greenproof.rules.tests_model import FileAnalysis, Marker, Unit
from greenproof.rules.tests_python import analyse_python, marker_display

_RUST_TEST_ATTR = re.compile(r"#\[\s*(?:\w+::)*test\b")
_TEST_MODULE_NAME = re.compile(r"(?:^test_.*\.py|.*_test\.(?:py|go)|.*\.(?:test|spec)\.\w+)$")


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


def pair_units(base: list[Unit], cand: list[Unit]) -> _Pairing:
    result = _Pairing()
    free = list(range(len(cand)))
    unmatched: list[Unit] = []

    def take(predicate_index: int) -> Unit:
        free.remove(predicate_index)
        return cand[predicate_index]

    for b in base:  # same path and key
        hit = next((i for i in free if cand[i].path == b.path and cand[i].key == b.key), None)
        if hit is None:
            unmatched.append(b)
        else:
            result.pairs.append((b, take(hit)))
    remaining: list[Unit] = []
    for b in unmatched:  # same key, another path: a move
        hits = [i for i in free if cand[i].key == b.key]
        hits.sort(key=lambda i: cand[i].body_hash != b.body_hash)
        if hits:
            result.pairs.append((b, take(hits[0])))
        else:
            remaining.append(b)
    for b in remaining:  # same body, another name: a rename
        hit = next((i for i in free if cand[i].body_hash == b.body_hash), None)
        if hit is None:
            result.left_base.append(b)
        else:
            result.pairs.append((b, take(hit)))
    result.left_cand = [cand[i] for i in free]
    return result


def _weak_reasons(b: Unit, c: Unit, count_drop_allowed: bool) -> list[tuple[str, int]]:
    if b.engine != c.engine:
        return []
    out: list[tuple[str, int]] = []
    if count_drop_allowed and c.asserts < b.asserts:
        out.append((f"assertions dropped from {b.asserts} to {c.asserts}", c.line))
    if c.strong < b.strong and c.weak > b.weak:
        out.append(("a strong assertion was replaced by a weaker one", c.line))
    if c.broad_raises > b.broad_raises and c.specific_raises < b.specific_raises:
        out.append(("a specific expected exception was widened to Exception", c.line))
    for kind in ("rel", "abs", "delta"):
        if (
            kind in b.tolerances
            and kind in c.tolerances
            and c.tolerances[kind] > b.tolerances[kind]
        ):
            out.append((f"tolerance `{kind}` widened to {c.tolerances[kind]:g}", c.line))
    places = (b.tolerances.get("places"), c.tolerances.get("places"))
    if places[0] is not None and places[1] is not None and places[1] < places[0]:
        out.append((f"`places` lowered to {places[1]:g}", c.line))
    if b.param_cases is not None and c.param_cases is not None and c.param_cases < b.param_cases:
        out.append((f"parametrize cases reduced from {b.param_cases} to {c.param_cases}", c.line))
    if c.swallowed > b.swallowed:
        out.append(
            (
                "an assertion sits inside a try/except that swallows the failure",
                c.swallow_line or c.line,
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


def _marker_findings(sides: list[_Side], base_keys: set[str]) -> list[Finding]:
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
            if m.kind == "pytest.importorskip" and m.scope not in base_keys:
                continue  # only an importorskip added to an existing test counts (SPEC 5.3)
            out.append(_marker_finding(s.cand.path, m))
    return out


def _marker_finding(path: str, m: Marker) -> Finding:
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
    return finding(
        "tests.skipped",
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

    base_units = [u for s in sides if s.base for u in s.base.units]
    cand_units = [u for s in sides if s.cand for u in s.cand.units]
    pairing = pair_units(base_units, cand_units)
    out: list[Finding] = []
    out.extend(_deleted_and_removed(sides, pairing))
    out.extend(_weakened(sides, pairing, changes))
    base_keys = {u.key for u in base_units}
    out.extend(_marker_findings(sides, base_keys))
    return out


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
        if s.cand is None:  # deleted, or renamed to a path that is not a test file
            if names or (
                not s.base.units
                and _TEST_MODULE_NAME.match(posixpath.basename(s.file.old_path or path))
            ):
                listed = ", ".join(u.name for u in names[:5]) or "no test functions"
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
        for removed, new in zip(names, added, strict=False):
            out.append(
                finding(
                    "tests.replaced",
                    path,
                    new.line,
                    f"test {removed.name} was removed and a different test {new.name} added",
                    "Keep the original test; add new tests alongside it, not in its place.",
                )
            )
        for removed in names[len(added) :]:
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
        reasons = _weak_reasons(b, c, allowed)
        if reasons:
            text = "; ".join(r for r, _ in reasons)
            out.append(
                finding(
                    "tests.weakened",
                    c.path,
                    reasons[0][1],
                    f"test {c.name} checks less than at the base: {text}",
                    "Restore the original assertions and fix the code instead.",
                )
            )
        if code_changed:
            line = _expected_change(b, c)
            if line is not None:
                out.append(
                    finding(
                        "tests.expected_changed",
                        c.path,
                        line,
                        f"expected value changed in test {c.name} while non-test code changed",
                        "Confirm the new expected value comes from the requirement, not from "
                        "what the code now returns.",
                    )
                )
    return out
