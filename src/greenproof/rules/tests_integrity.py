"""Test-integrity rules: tests.deleted, removed, replaced, skipped, focused, weakened and
expected_changed (SPEC 5.2 to 5.4).

Identity: a test is `key` (its qualified name without the path) plus a normalised body hash.
Base and candidate tests are paired first by path and key, then by key anywhere (a move), then
by body hash (a rename); only what is left over counts as removed, replaced or deleted.
"""

from __future__ import annotations

import ast
import posixpath
import re
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field

from greenproof.changeset import ChangedFile, ChangeSet
from greenproof.findings import BLOCK, WARN, Finding
from greenproof.rules._common import CODE_SUFFIXES, finding, language_of, sev_finding
from greenproof.rules.collect import Collection, collection_from_base
from greenproof.rules.self_mocked import check_autouse_fixtures, check_self_mocked
from greenproof.rules.tests_data import check_expected_data
from greenproof.rules.tests_lines import analyse_lines, go_call_pattern
from greenproof.rules.tests_model import FileAnalysis, Marker, Unit
from greenproof.rules.tests_python import (
    HelperEffect,
    Sources,
    analyse_python,
    call_self_replaced,
    helper_effects,
    is_test_support_text,
    lost_helpers,
    marker_display,
)
from greenproof.rules.tests_tables import MAX_MATCH, fits, marker_excused, match_rows

_RUST_TEST_ATTR = re.compile(r"#\[\s*(?:\w+::)*(?:test|rstest|test_case)\b")


def analyse(
    path: str, text: str, siblings: Sequence[str] = (), sources: Sources | None = None
) -> FileAnalysis | None:
    lang = language_of(path)
    if lang is None:
        return None
    if lang == "python":
        return analyse_python(path, text, sources) or analyse_lines(path, text, "python-lines")
    return analyse_lines(path, text, lang, siblings)


_GO_TEST_SUFFIX = "_test.go"


def _go_siblings(changes: ChangeSet, path: str, base: bool) -> tuple[str, ...]:
    """Texts of the other `_test.go` files of the directory: where Go assertion helpers live."""
    if not path.endswith(_GO_TEST_SUFFIX):
        return ()
    return tuple(changes.siblings(path, _GO_TEST_SUFFIX, base).values())


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


def _band_floor(expected: float) -> float:
    """The smallest tolerance that counts as loosening an exact numeric check: 1% of the
    expected value, or 0.01 when it is 0 (SPEC 5.4)."""
    return 0.01 * abs(expected) if expected else 0.01


def _loosened_numeric(b: Unit, c: Unit) -> list[_Reason]:
    """An exact (or default-`approx`) comparison to a number at the base that the candidate makes
    with an explicit tolerance of 1% of the expected value or more."""
    out: list[_Reason] = []
    for (shape, expected), (band, src) in c.numeric_bands.items():
        floor = _band_floor(expected)
        before = b.numeric_bands.get((shape, expected))
        if before is not None and before[0] < floor <= band:
            out.append(
                (
                    f"exact check on `{src}` against {expected:g} loosened to a tolerance "
                    f"of {band:g} (1% of the expected value or more)",
                    c.line,
                    BLOCK,
                )
            )
    return out


def _only_loose_dropped(b: Unit, c: Unit, added: _Added) -> bool:
    """SPEC 5.4: every base assertion that is gone is a loose check, and every subject of an
    exact check at the base is still the subject of one in the candidate. Beyond the spec text
    (replay: httpx ff9813e84, 5ee613525): everything checked loosely at the base is still
    checked, exactly or loosely, so a lone `assert not x.closed` dropped to nothing still blocks."""
    gone = Counter(b.assertion_forms) - Counter(c.assertion_forms)
    gone_forms = [f for f in gone if f not in added.forms]
    return (
        bool(gone_forms)
        and all(f in b.loose_forms for f in gone_forms)
        and set(b.exact_subjects) <= set(c.exact_subjects)
        and set(b.loose_targets) <= set(c.exact_subjects) | set(c.loose_targets)
    )


def _weak_reasons(b: Unit, c: Unit, count_drop_allowed: bool, added: _Added) -> list[_Reason]:
    if b.engine != c.engine:
        return []
    out: list[_Reason] = []
    if b.engine == "python":
        before, after = _effective(b, c)
        if after < before and not set(b.assertion_forms) <= set(c.assertion_forms) | added.forms:
            only_loose = _only_loose_dropped(b, c, added)
            note = " (only loose checks dropped)" if only_loose else ""
            out.append(
                (
                    f"checks dropped from {before} to {after}{note}",
                    c.line,
                    WARN if only_loose else BLOCK,
                )
            )
    elif count_drop_allowed and c.asserts < b.asserts:
        if c.credited >= b.credited:  # helper bodies or table rows make up the lines (SPEC 5.4)
            note = f"; helper checks or table rows give {c.credited} against {b.credited}"
            out.append(
                (f"assertions dropped from {b.asserts} to {c.asserts} by line{note}", c.line, WARN)
            )
        else:
            out.append((f"assertions dropped from {b.asserts} to {c.asserts}", c.line, BLOCK))
    replaced = [line for expr, line in c.self_calls if expr in b.compared]
    base_replaced = sum(1 for expr, _ in b.self_calls if expr in b.compared)
    if c.vacuous + len(replaced) > b.vacuous + base_replaced:
        line = c.vacuous_line or (replaced[0] if replaced else c.line)
        out.append(("an assertion was made vacuous: it can never fail", line, BLOCK))
    if b.engine == "python" and call_self_replaced(b, c):
        out.append(
            (
                "a comparison was replaced by a call compared with itself: it can never fail",
                c.call_self_line or c.line,
                BLOCK,
            )
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
    if b.engine == "python":
        out.extend(_loosened_numeric(b, c))
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
            remaining.update((m.scope, m.kind) for m in s.base.markers if not _is_build(m))
    out: list[Finding] = []
    for s in sides:
        if not s.cand:
            continue
        out.extend(_build_findings(s.base, s.cand))
        for m in s.cand.markers:
            if _is_build(m):
                continue
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
            if skip and marker_excused(s.base, s.cand, m):
                continue  # a skip only the test's new table rows can reach (SPEC 5.3)
            out.append(_marker_finding(s.cand.path, m, warn=fresh and skip))
    return out


def _is_build(m: Marker) -> bool:
    return m.kind.startswith(("go:build", "+build"))


_BUILD_TAG = re.compile(r"[A-Za-z0-9_.]+")


def _tags(kinds: Counter[str]) -> set[str]:
    """The tag names in build-constraint kinds, ignoring syntax, `!`, `&&`, `||`, `,` and spaces."""
    return {t for k in kinds for t in _BUILD_TAG.findall(k.split(" ", 1)[1])}


def _build_findings(base: FileAnalysis | None, cand: FileAnalysis) -> list[Finding]:
    """Go build constraints of one `_test.go` file (SPEC 5.3), comparing the two revisions.

    No constraint at base and one now: block. A constraint at base that now differs: warn,
    since a narrower expression skips the file on more platforms but a wider one does not.
    Adding `//go:build` over unchanged `// +build` lines (gofmt's migration, which keeps the
    same tags), or dropping `// +build` lines under an unchanged `//go:build` (`go fix`), is
    no finding. A brand-new file is never flagged: it hides no existing verification."""
    if base is None:
        return []
    now = [m for m in cand.markers if _is_build(m)]
    if not now:
        return []
    before = [m for m in base.markers if _is_build(m)]
    modern_b = Counter(m.kind for m in before if m.kind.startswith("go:build"))
    legacy_b = Counter(m.kind for m in before if m.kind.startswith("+build"))
    modern_c = Counter(m.kind for m in now if m.kind.startswith("go:build"))
    legacy_c = Counter(m.kind for m in now if m.kind.startswith("+build"))
    if modern_b + legacy_b == modern_c + legacy_c:
        return []
    if not before:
        return [_marker_finding(cand.path, now[0])]
    if legacy_b == legacy_c and not modern_b and _tags(modern_c) == _tags(legacy_c):
        return []  # gofmt added the `//go:build` for the `// +build` lines already there
    if modern_b == modern_c and not legacy_c - legacy_b:
        return []  # `go fix` dropped `// +build` lines under the `//go:build` that stays
    new = [m for m in now if m.kind not in modern_b + legacy_b]
    return [_marker_finding(cand.path, new[0], warn=True)] if new else []


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
    if _is_build(m):
        if warn:
            return sev_finding(
                "tests.skipped",
                WARN,
                path,
                m.line,
                f"build constraint changed to `{text}`: `go test ./...` may compile this file "
                "on fewer platforms",
                "Check the new constraint still builds the file wherever its tests must run.",
            )
        return finding(
            "tests.skipped",
            path,
            m.line,
            f"build constraint `{text}` added: `go test ./...` no longer compiles this file",
            "Remove the constraint so the file's tests run in the default build.",
        )
    return sev_finding(
        "tests.skipped",
        WARN if warn else BLOCK,
        path,
        m.line,
        f"`{text}` added to {where}",
        f"Fix the code so {where} passes instead of skipping it; if the test is wrong, say so.",
    )


# Unchanged test files looked at because a helper or conftest changed: at most this many are
# analysed, and at most `_MAX_KEPT` of those are compared as if they had changed. Rules have no
# channel to the report's notes, so a file past a cap is skipped silently (SPEC 5.4).
_MAX_ANALYSED = 300
_MAX_KEPT = 200


def _below(path: str, directory: str) -> bool:
    return not directory or path.startswith(directory + "/")


def _stem(path: str) -> str:
    """The name an importer writes for a module: `helpers` for `tests/helpers.py` and for
    `tests/helpers/__init__.py`."""
    name = posixpath.basename(path)
    if name == "__init__.py":
        name = posixpath.basename(posixpath.dirname(path))
    return name.removesuffix(".py")


@dataclass
class _Module:
    """A changed helper module (importable from any test file) or conftest.py (its directory
    and below), and the names whose checks the change lost."""

    paths: set[str]
    scope: str | None  # the conftest's directory; None for a helper module
    names: set[str]
    stems: set[str] = field(default_factory=set)

    def reaches(self, path: str) -> bool:
        return self.scope is None or _below(path, self.scope)

    def mentioned_in(self, text: str) -> bool:
        """The text names a lost helper as a word and, for a helper module, the module too."""
        if not _word_pattern(self.names).search(text):
            return False
        return self.scope is not None or bool(_word_pattern(self.stems).search(text))


def _word_pattern(words: set[str]) -> re.Pattern[str]:
    return re.compile(r"\b(?:" + "|".join(re.escape(w) for w in sorted(words)) + r")\b")


def _tree(text: str | None) -> ast.Module | None:
    if text is None:
        return None
    try:
        return ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return None


def _changed_modules(
    changes: ChangeSet, sides: list[_Side], base_src: Sources, cand_src: Sources
) -> list[_Module]:
    """The changed Python helper modules and conftest.py files that lost something their callers
    relied on. A helper module is a changed file that holds no test of its own and is in the test
    paths or is a test-support module outside them (SPEC 5.4). Each is analysed on both sides: a
    helper whose credited checks dropped or weakened, that now swallows, or that is gone puts
    its name in `names`; a change that loses nothing yields no module, and re-analyses no file."""
    sided = {s.file.path: s for s in sides}
    out: list[_Module] = []
    for f in changes.files:
        path, old = f.path, f.old_path or f.path
        if not path.endswith(".py") and not old.endswith(".py"):
            continue
        conftest = "conftest.py" in (posixpath.basename(path), posixpath.basename(old))
        side = sided.get(path)
        if not conftest:
            if side is not None:
                if any(a is not None and a.units for a in (side.base, side.cand)):
                    continue  # a test file, not a helper module
            elif not (
                is_test_support_text(old, f.old_text) or is_test_support_text(path, f.new_text)
            ):
                continue
        base_tree = _tree(f.old_text)
        cand_tree = _tree(f.new_text)
        before = helper_effects(old, base_tree, base_src) if base_tree is not None else None
        if f.new_text is None:
            after: dict[str, HelperEffect] | None = {}
        else:
            after = helper_effects(path, cand_tree, cand_src) if cand_tree is not None else None
        lost = lost_helpers(before, after)
        if not lost:
            continue
        out.append(
            _Module(
                {path, old},
                None if not conftest else posixpath.dirname(path),
                lost,
                {_stem(path), _stem(old)},
            )
        )
    return out


class _Rescan:
    """The unchanged Python test files a change can reach, analysed on demand and at most
    `_MAX_ANALYSED` of them. Shared by the helper dependents and the autouse fixture scope."""

    def __init__(
        self,
        changes: ChangeSet,
        cfg: Collection,
        base_src: Sources,
        cand_src: Sources,
        known: set[str],
    ) -> None:
        self.changes, self.cfg, self.base_src, self.cand_src = changes, cfg, base_src, cand_src
        self.known = known
        self.analysed = 0
        self._cand: dict[str, FileAnalysis | None] = {}

    @property
    def full(self) -> bool:
        return self.analysed >= _MAX_ANALYSED

    def paths(self, directory: str | None = None) -> Iterator[str]:
        """Unchanged collected test files at or below `directory` (anywhere when None)."""
        for path in sorted(self.changes.cand_paths()):
            if (
                path not in self.known
                and path.endswith(".py")
                and (directory is None or _below(path, directory))
                and self.changes.is_test(path)
                and self.cfg.file_collected(path)
            ):
                yield path

    def cand(self, path: str, text: str) -> FileAnalysis | None:
        if path not in self._cand:
            self.analysed += 1
            self._cand[path] = analyse_python(path, text, self.cand_src)
        return self._cand[path]

    def mentioning(self, directory: str, name: str) -> list[FileAnalysis]:
        """Candidate analyses of the unchanged test files below `directory` whose text has `name`
        as a word: the autouse fixture scope (SPEC 5.8)."""
        pattern = _word_pattern({name})
        out: list[FileAnalysis] = []
        for path in self.paths(directory):
            if self.full and path not in self._cand:
                break
            text = self.changes.cand_text(path)
            if text is None or not pattern.search(text):
                continue
            analysis = self.cand(path, text)
            if analysis is not None:
                out.append(analysis)
        return out


def _dependents(rescan: _Rescan, sides: list[_Side]) -> list[_Side]:
    """Unchanged Python test files that a changed helper module or conftest.py can affect,
    analysed as if they had changed, so a helper that lost its assertions shows up as weakened
    tests in its callers. Only a helper or fixture that lost checks pulls anything in: files that
    name it (and, for a helper module, the module) as a word, among the test files anywhere in
    the tree that import it, or for a conftest.py those below its directory."""
    changes = rescan.changes
    modules = _changed_modules(changes, sides, rescan.base_src, rescan.cand_src)
    if not modules:
        return []
    out: list[_Side] = []
    for path in rescan.paths():
        if len(out) >= _MAX_KEPT or rescan.full:
            break
        new_text = changes.cand_text(path)
        if new_text is None:
            continue
        near = [m for m in modules if m.reaches(path) and m.mentioned_in(new_text)]
        if not near:
            continue
        old_text = changes.base_text(path)
        if old_text is None:
            continue
        cand = rescan.cand(path, new_text)
        if cand is None:
            continue
        base = analyse_python(path, old_text, rescan.base_src)
        if base is None:
            continue
        imported = base.imports | cand.imports
        if not any(m.scope is not None or m.paths & imported for m in near):
            continue
        file = ChangedFile(
            path, "M", None, old_text.encode(), new_text.encode(), is_test=True, was_test=True
        )
        out.append(_Side(base, cand, file))
    return out


def check_tests(changes: ChangeSet) -> list[Finding]:
    base_src = Sources(changes.base_text, changes.is_test, changes.listing(True))
    cand_src = Sources(changes.cand_text, changes.is_test, changes.listing(False))
    sides: list[_Side] = []
    for f in changes.files:
        old_path = f.old_path or f.path
        base = cand = None
        if _is_test_side(old_path, f.old_text, f.was_test) and f.old_text is not None:
            base = analyse(f.path, f.old_text, _go_siblings(changes, old_path, True), base_src)
        if _is_test_side(f.path, f.new_text, f.is_test) and f.new_text is not None:
            cand = analyse(f.path, f.new_text, _go_siblings(changes, f.path, False), cand_src)
        if base is None and cand is None:
            continue
        if cand is None and base is not None and f.status not in ("D", "R"):
            cand = FileAnalysis(f.path, base.engine)  # the file stopped being a test file
        sides.append(_Side(base, cand, f))
    sides.extend(_go_callers(changes, sides))

    cfg = collection_from_base(changes)
    known = {s.file.path for s in sides} | {s.file.old_path for s in sides if s.file.old_path}
    rescan = _Rescan(changes, cfg, base_src, cand_src, known)
    sides.extend(_dependents(rescan, sides))
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
    out.extend(check_expected_data(changes))
    out.extend(
        check_self_mocked(
            [(None, u) for u in pairing.left_cand] + [(b, c) for b, c in pairing.pairs],
            {s.base.path: s.base for s in sides if s.base},
            {s.cand.path: s.cand for s in sides if s.cand},
            changes,
        )
    )
    out.extend(
        check_autouse_fixtures(
            {s.base.path: s.base for s in sides if s.base},
            {s.cand.path: s.cand for s in sides if s.cand},
            rescan.mentioning,
        )
    )
    out.extend(_fixture_changes(sides, changes))
    return out


def _go_callers(changes: ChangeSet, sides: list[_Side]) -> list[_Side]:
    """Unchanged Go test files that call an assertion helper declared in a changed one (SPEC
    5.4): re-analysed on both sides, so a helper that lost its checks weakens its callers."""
    seen = {s.file.path for s in sides} | {s.file.old_path for s in sides if s.file.old_path}
    out: list[_Side] = []
    for s in sides:
        names: set[str] = set()
        for a in (s.base, s.cand):
            if a is not None:
                names |= a.helpers
        if not names or not s.file.path.endswith(_GO_TEST_SUFFIX):
            continue
        call = go_call_pattern(names)
        old_path = s.file.old_path or s.file.path
        base_files = changes.siblings(old_path, _GO_TEST_SUFFIX, True)
        cand_files = changes.siblings(s.file.path, _GO_TEST_SUFFIX, False)
        for path in sorted(set(base_files) & set(cand_files)):
            if path in seen:
                continue
            old, new = base_files[path], cand_files[path]
            if not (call.search(old) or call.search(new)):
                continue
            seen.add(path)
            file = ChangedFile(path, "M", None, old.encode(), new.encode(), True, True)
            out.append(
                _Side(
                    analyse(path, old, _go_siblings(changes, path, True)),
                    analyse(path, new, _go_siblings(changes, path, False)),
                    file,
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


def _row_gaps(
    pairs: list[tuple[Unit, Unit]], added: list[Unit], removed: Sequence[Unit] = ()
) -> tuple[str, bool]:
    """Why a parametrized Python test that took the place of removed tests does not earn the fold
    warning (SPEC 5.7): a row can finish without asserting its values, or a row new to it is
    marked skip or xfail; a body too large to read is said (not blocking), never guessed. Returns
    (the text, whether it blocks). A mark of a kind the tests it replaces already carried (their
    own marks, or marked rows of theirs) is not new."""
    big = ""
    carried = {k for u in (*removed, *(b for b, _ in pairs)) for k in u.mark_kinds}
    carried.update(k for b, _ in pairs for _, kinds in b.param_marked for k in kinds)
    for before, c in [*pairs, *((None, u) for u in added)]:
        old = set(before.param_forms) if before is not None else set()
        why = c.row_gap
        if not why and any(f not in old and not set(k) <= carried for f, k in c.param_marked):
            why = "a row marked skip or xfail never asserts"
        if why:
            return f"the parametrized test {c.name} was not credited as a fold: {why}", True
        if c.row_big and not big:
            big = (
                f"the parametrized test {c.name} is too large to check for rows that "
                "leave before asserting"
            )
    return big, False


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


def _unit_count(u: Unit) -> int:
    """How many tests a unit stands for: the rows of a table that runs, the parametrize or
    `each` cases when the rows are not literal, else one."""
    if u.table is not None and u.table.runs:
        return len(u.table.rows)
    if u.table is None and u.param_cases:
        return u.param_cases
    return 1


def _not_credited(unit: Unit, reason: str) -> str:
    return f"the table-driven test {unit.name} was not credited as a fold: {reason}"


def _fold_into_tables(
    s: _Side, names: list[Unit], partner: dict[int, Unit]
) -> tuple[dict[int, Unit], str]:
    """Removed JS, Go or Rust tests (by id) accounted for by a row of a table-driven test of the
    same file, the way pytest parametrize folds tests: the table runs (every wrapper live, no row
    skipped), a row of its own carries the removed test's literals (the numbers and strings its
    body passes and expects, never its name), an assertion of the table's body reads the
    parameters that carry them, and the file's total of tests and rows did not fall. All or
    nothing: when one removed test has no row of its own, none is accounted for. A row a paired
    test already had at the base is not available, and a plain test that became the table needs
    a row for itself. The text says why a table was not credited, when one was not."""
    if s.base is None or s.cand is None or not names:
        return {}, ""
    if any(u.engine not in ("js", "go", "rust") for u in names):
        return {}, ""
    # a table that carries the removed tests' values but is not credited: the first reason
    why = ""
    wanted = [Counter(u.literals) for u in names]
    for c in s.cand.units:
        t = c.table
        if t is None or t.runs or not t.why or len(t.rows) * len(wanted) > MAX_MATCH:
            continue
        body = Counter(c.literals)
        if any(fits(need, Counter(row), body) for need in wanted for row in t.rows):
            why = _not_credited(c, t.why)
            break
    if sum(map(_unit_count, s.base.units)) > sum(map(_unit_count, s.cand.units)):
        return {}, why
    pool: list[tuple[Unit, Counter[str], Counter[str], int]] = []
    needs: list[Counter[str]] = []
    for c in s.cand.units:
        t = c.table
        if t is None or not t.runs or (c.asserts == 0 and not t.implicit):
            continue
        rows = list(enumerate(zip(t.rows, t.forms, strict=True)))
        b = partner.get(id(c))
        if b is not None and b.table is not None:
            spent = Counter(b.table.forms)
            fresh = []
            for i, (row, form) in rows:
                if spent[form] > 0:
                    spent[form] -= 1
                else:
                    fresh.append((i, (row, form)))
            rows = fresh
        elif b is not None and b.literals:
            needs.append(Counter(b.literals))
        pool.extend((c, Counter(row), Counter(c.literals), i) for i, (row, _) in rows)
    if not pool:
        return {}, why
    needs.extend(Counter(u.literals) for u in names)
    hit = match_rows(needs, [(row, body) for _, row, body, _ in pool])
    if hit is None:
        return {}, why
    for need, j in zip(needs, hit, strict=True):
        unit, _, body, index = pool[j]
        table = unit.table
        gap = table.unasserted(index, need - body) if table is not None else ""
        if gap:
            return {}, _not_credited(unit, gap)
    return {
        id(u): pool[j][0] for u, j in zip(names, hit[len(needs) - len(names) :], strict=True)
    }, ""


def _deleted_and_removed(sides: list[_Side], pairing: _Pairing) -> list[Finding]:
    out: list[Finding] = []
    partner = {id(c): b for b, c in pairing.pairs}
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
        folded_into, not_credited = _fold_into_tables(s, names, partner)
        for removed in (u for u in names if id(u) in folded_into):
            table = folded_into[id(removed)]
            limit = f" ({table.table.note})" if table.table is not None and table.table.note else ""
            out.append(
                sev_finding(
                    "tests.replaced",
                    WARN,
                    path,
                    table.line,
                    f"test {removed.name} was removed and folded into table-driven test "
                    f"{table.name}: a row of its own carries the same values{limit}",
                    "Keep the original test; add new tests alongside it, not in its place.",
                )
            )
        names = [u for u in names if id(u) not in folded_into]
        used = {id(u) for u in folded_into.values()}
        added = [u for u in added if id(u) not in used]
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
        if rewrite:
            gap, blocks = _row_gaps(absorbing, added, names)
            not_credited = not_credited or gap
            rewrite = rewrite and not blocks
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
                    + (" (a rewrite: same subjects, at least as many checks)" if rewrite else "")
                    + (f" ({not_credited})" if not_credited else ""),
                    "Keep the original test; add new tests alongside it, not in its place.",
                )
            )
        for removed in [] if rewrite else names[len(added) :]:
            out.append(
                finding(
                    "tests.removed",
                    path,
                    None,
                    f"test {removed.name} was removed and nothing replaced it"
                    + (f" ({not_credited})" if not_credited else ""),
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


def _fixture_changes(sides: list[_Side], changes: ChangeSet) -> list[Finding]:
    """SPEC 5.4: a fixture whose returned or yielded literal changed (tests.expected_changed)."""
    out: list[Finding] = []
    code_changed = any(f.path.endswith(CODE_SUFFIXES) for f in changes.non_tests())
    for s in sides:
        if s.base is None or s.cand is None:
            continue
        for name, fx in s.cand.fixtures.items():
            old = s.base.fixtures.get(name)
            if old is None or not old.literals or not fx.literals:
                continue
            if Counter(old.literals) == Counter(fx.literals):
                continue
            shown = name.rsplit("::", 1)[-1]
            tail = "while non-test code changed" if code_changed else "and no non-test code changed"
            out.append(
                finding(
                    "tests.expected_changed",
                    s.cand.path,
                    fx.line,
                    f"expected value changed in fixture {shown} ({s.cand.path}) {tail}",
                    "Confirm the new value comes from the requirement, not from what the code "
                    "now returns; every test that uses the fixture compares against it.",
                )
            )
    return out
