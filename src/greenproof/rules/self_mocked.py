"""tests.self_mocked: an added or changed test patches the callable its assertions call (SPEC 5.8).

Python is analysed on the AST the test analyser already built (`Unit.node`); JS/TS on the masked
source lines. Either way the rule only looks at tests that are new or changed, and only at
patches the same test did not already contain at the base.
"""

from __future__ import annotations

import ast
import posixpath
import re
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass

from greenproof.changeset import ChangeSet
from greenproof.findings import Finding
from greenproof.rules._common import finding
from greenproof.rules.tests_lines import mask_source
from greenproof.rules.tests_model import FileAnalysis, Fixture, Unit
from greenproof.rules.tests_python import dotted

_HINT = (
    "Patch a collaborator (the network, the clock) instead, and assert on the real code's "
    "result; a test that replaces the code under test only checks the replacement."
)

# ---- Python -------------------------------------------------------------------------------

_PATCH = {"patch", "mock.patch", "mocker.patch", "unittest.mock.patch"}
_PATCH_OBJECT = {f"{p}.object" for p in _PATCH}


@dataclass(frozen=True)
class _Imports:
    froms: Mapping[str, tuple[str, str, bool]]  # local name -> (module, name, relative)
    modules: Mapping[str, str]  # alias -> module


def _imports(tree: ast.AST) -> _Imports:
    froms: dict[str, tuple[str, str, bool]] = {}
    modules: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name != "*":
                    froms[alias.asname or alias.name] = (
                        node.module or "",
                        alias.name,
                        node.level > 0,
                    )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    modules[alias.asname] = alias.name
    return _Imports(froms, modules)


def _resolve(name: str, imports: _Imports) -> tuple[str, bool]:
    """The dotted name with its first component resolved through the file's imports; the flag
    says the name went through a relative `from . import`, so only its tail is reliable."""
    head, _, rest = name.partition(".")
    suffix = f".{rest}" if rest else ""
    if head in imports.froms:
        module, original, relative = imports.froms[head]
        return f"{module}.{original}".lstrip(".") + suffix, relative
    if head in imports.modules:
        return imports.modules[head] + suffix, False
    return name, False


def _string(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _keyword(call: ast.Call, name: str) -> ast.AST | None:
    return next((k.value for k in call.keywords if k.arg == name), None)


def _target(call: ast.Call) -> tuple[str, str] | None:
    """(qualifier, name) of what a patch call replaces, or None when it is not a patch."""
    func = dotted(call.func)
    args = call.args
    if func.rsplit(".", 1)[-1] == "setattr" and "monkeypatch" in func.split(".")[:-1]:
        first = _string(args[0]) if args else None
        if first is not None and len(args) == 2 and "." in first:
            qual, _, name = first.rpartition(".")
            return qual, name
        second = _string(args[1]) if len(args) > 1 else None
        if second is not None and args:
            qual = first if first is not None else dotted(args[0])
            return (qual, second) if qual else None
        return None
    if func in _PATCH:
        spec = _string(args[0] if args else _keyword(call, "target"))
        if spec is not None and "." in spec:
            qual, _, name = spec.rpartition(".")
            return qual, name
        return None
    if func in _PATCH_OBJECT:
        attr = _string(args[1] if len(args) > 1 else _keyword(call, "attribute"))
        obj = dotted(args[0]) if args else ""
        return (obj, attr) if attr is not None and obj else None
    return None


def _patches(fn: ast.AST) -> list[tuple[str, str, int]]:
    """Every patch in the test: body, decorators and `with` items are all calls in the tree."""
    out: list[tuple[str, str, int]] = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            target = _target(node)
            if target is not None:
                out.append((*target, node.lineno))
    return out


def _asserted_calls(fn: ast.AST) -> list[str]:
    """Dotted names of the calls inside assertions: `assert` statements and `self.assert*(...)`."""
    roots: list[ast.AST] = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Assert):
            roots.append(node.test)
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            tail = dotted(node.value.func).rsplit(".", 1)[-1]
            if tail.startswith("assert") and not tail.startswith("assert_"):
                roots.extend([*node.value.args, *(k.value for k in node.value.keywords)])
    names: list[str] = []
    for root in roots:
        for node in ast.walk(root):
            if isinstance(node, ast.Call) and dotted(node.func):
                names.append(dotted(node.func))
    return names


def _same_callable(
    patched: str, call: str, imports: _Imports, call_imports: _Imports | None = None
) -> bool:
    """Whether the call is to the patched callable; `imports` resolves the patch target, and
    `call_imports` (default: the same) the name the call is written with."""
    if patched == call:
        return True
    want, _ = _resolve(patched, imports)
    got, relative = _resolve(call, call_imports or imports)
    return want == got or (relative and want.endswith(f".{got}"))


def _python(
    unit: Unit, base: Unit | None, imports: _Imports, base_imports: _Imports | None
) -> Iterator[tuple[int, str, str]]:
    fn = unit.node
    if fn is None:
        return
    seen: set[tuple[str, str]] = set()
    if base is not None and base.node is not None and base_imports is not None:
        for qual, name, _ in _patches(base.node):
            seen.add((qual, name))
            seen.add((_resolve(qual, base_imports)[0], name))
    calls = _asserted_calls(fn)
    for qual, name, line in _patches(fn):
        if (qual, name) in seen or (_resolve(qual, imports)[0], name) in seen:
            continue
        patched = f"{qual}.{name}"
        hit = next((c for c in calls if _same_callable(patched, c, imports)), None)
        if hit is not None:
            yield line, patched, f"{hit}(...)"


def _fixture_patches(fn: ast.AST) -> list[tuple[str, str, int]]:
    """The patches in a fixture: `_patches`, plus a plain assignment to a module attribute
    (`shop.refund = lambda a: a`)."""
    out = _patches(fn)
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Attribute) and (qual := dotted(target.value)):
                    out.append((qual, target.attr, node.lineno))
    return out


def _fixture_scope(
    fx: Fixture,
    files: Mapping[str, FileAnalysis],
    name: str,
    unchanged: Callable[[str, str], Sequence[FileAnalysis]] | None,
) -> list[Unit]:
    """The collected Python tests an autouse fixture applies to: those of its own file (its class,
    for a class fixture), or for a conftest.py every test at or below its directory. Of the
    unchanged files below a conftest.py, `unchanged(directory, name)` gives the ones whose text
    mentions the patched callable's `name`; the rest cannot assert on it."""
    here = posixpath.dirname(fx.path)
    if posixpath.basename(fx.path) == "conftest.py":
        scope = {p: files[p] for p in sorted(files) if not here or p.startswith(here + "/")}
        if unchanged is not None:
            for analysis in unchanged(here, name):
                scope.setdefault(analysis.path, analysis)
        analyses = list(scope.values())
    else:
        analyses = [files[fx.path]] if fx.path in files else []
    units: list[Unit] = []
    for analysis in analyses:
        if not analysis.engine.startswith("python") or not analysis.collected:
            continue
        units.extend(u for u in analysis.units if u.node is not None and u.key.startswith(fx.scope))
    return units


def check_autouse_fixtures(
    base_files: Mapping[str, FileAnalysis],
    cand_files: Mapping[str, FileAnalysis],
    unchanged: Callable[[str, str], Sequence[FileAnalysis]] | None = None,
) -> list[Finding]:
    """An added or changed autouse fixture (conftest.py or a test file) patches a callable that
    tests in its scope call in their assertions: every such test runs against the mock."""
    out: list[Finding] = []
    imports: dict[str, _Imports] = {}
    seen_files: dict[str, FileAnalysis] = dict(cand_files)

    def scoped(directory: str, name: str) -> Sequence[FileAnalysis]:
        found = unchanged(directory, name) if unchanged is not None else []
        seen_files.update((a.path, a) for a in found)
        return found

    def imports_of(path: str) -> _Imports | None:
        analysis = seen_files.get(path)
        if analysis is None or analysis.tree is None:
            return None
        if path not in imports:
            imports[path] = _imports(analysis.tree)
        return imports[path]

    for path, analysis in cand_files.items():
        if not analysis.engine.startswith("python"):
            continue
        for name, fx in analysis.fixtures.items():
            if not fx.autouse or fx.node is None:
                continue
            before = base_files.get(path)
            old = before.fixtures.get(name) if before is not None else None
            if old is not None and old.body_hash == fx.body_hash:
                continue
            fx_imports = imports_of(path)
            if fx_imports is None:
                continue
            seen: set[tuple[str, str]] = set()
            if old is not None and old.node is not None:
                seen.update((q, n) for q, n, _ in _fixture_patches(old.node))
            for qual, attr, line in _fixture_patches(fx.node):
                if (qual, attr) in seen:
                    continue
                patched = f"{qual}.{attr}"
                units = _fixture_scope(fx, cand_files, attr, scoped)
                hit = ""
                names: list[str] = []
                for u in units:
                    call = next(
                        (
                            c
                            for c in (_asserted_calls(u.node) if u.node is not None else [])
                            if _same_callable(patched, c, fx_imports, imports_of(u.path))
                        ),
                        None,
                    )
                    if call is not None:
                        hit = hit or call
                        names.append(u.name)
                if names:
                    shown = ", ".join(names[:3]) + (
                        f" and {len(names) - 3} more" if len(names) > 3 else ""
                    )
                    out.append(
                        finding(
                            "tests.self_mocked",
                            path,
                            line,
                            f"autouse fixture {fx.name.rsplit('::', 1)[-1]} patches {patched} and "
                            f"the tests in its scope assert on {hit}(...) ({shown}); "
                            "they test the mock",
                            _HINT,
                        )
                    )
    return out


# ---- JS / TS ------------------------------------------------------------------------------

_JS_IMPORT = re.compile(r"""\bimport\s+([^;'"`]*?)\s*from\s*(['"`])([^'"`\n]+)\2""", re.S)
_JS_REQUIRE = re.compile(
    r"""\b(?:const|let|var)\s+(\{[^}]*\}|[\w$]+)\s*=\s*require\(\s*(['"`])([^'"`\n]+)\2\s*\)""",
    re.S,
)
_JS_MOCK = re.compile(r"""\b(?:jest|vi)\.mock\(\s*(['"`])([^'"`\n]+)\1""")
_JS_SPY = re.compile(r"""\b(?:jest|vi)\.spyOn\(\s*([\w$.]+)\s*,\s*(['"`])([\w$]+)\2\s*\)""")
_JS_MOCKED = r"\s*\.\s*mock(?:ReturnValue|ResolvedValue|Implementation)\b"
_JS_EXPECT = re.compile(r"\bexpect\s*\(")
_IDENT = re.compile(r"[\w$]+")


def _clause_names(clause: str) -> set[str]:
    names = set(re.findall(r"\*\s*as\s+([\w$]+)", clause))
    brace = re.search(r"\{([^}]*)\}", clause)
    if brace:
        for part in brace.group(1).split(","):
            part = re.sub(r"^\s*type\s+", "", part.strip())
            tail = re.split(r"\s+as\s+|:", part)[-1].strip()
            if _IDENT.fullmatch(tail):
                names.add(tail)
    rest = re.sub(r"\{[^}]*\}|\*\s*as\s+[\w$]+", "", clause)
    names.update(w for w in re.findall(r"[\w$]+", rest) if w != "type")
    return names


def _js_imports(code: str) -> dict[str, set[str]]:
    """Module specifier -> the local names the file imports from it."""
    out: dict[str, set[str]] = {}
    for pattern in (_JS_IMPORT, _JS_REQUIRE):
        for m in pattern.finditer(code):
            out.setdefault(_strip_ext(m.group(3)), set()).update(_clause_names(m.group(1)))
    return out


def _strip_ext(spec: str) -> str:
    return re.sub(r"\.(?:[cm]?[jt]sx?)$", "", spec)


def _expect_bodies(masked: Sequence[str], first: int, last: int) -> list[str]:
    """The text inside each `expect(...)` call of lines first..last (1-based, inclusive)."""
    text = "\n".join(masked[first - 1 : last])
    out: list[str] = []
    for m in _JS_EXPECT.finditer(text):
        depth, i = 1, m.end()
        while i < len(text) and depth:
            depth += {"(": 1, ")": -1}.get(text[i], 0)
            i += 1
        out.append(text[m.end() : i - 1])
    return out


@dataclass(frozen=True)
class _JsPatch:
    line: int
    target: str
    names: frozenset[str]  # identifiers whose appearance in expect(...) means the mock is asserted
    qualified: str = ""  # `X.n` form for a spy, matched as a whole


class _JsFile:
    """What one JS/TS test file adds in patches, and how its tests assert."""

    def __init__(self, text: str, base_text: str | None) -> None:
        self.masked, code_lines = mask_source(text, "js")
        base_lines: Counter[str] = Counter()
        if base_text is not None:
            base_lines.update(ln.strip() for ln in mask_source(base_text, "js")[1])
        self.added: set[int] = set()
        for number, raw in enumerate(code_lines, start=1):
            line = raw.strip()
            if not line:
                continue
            if base_lines[line] > 0:
                base_lines[line] -= 1
            else:
                self.added.add(number)
        code = "\n".join(code_lines)
        imports = _js_imports(code)
        every = set().union(*imports.values()) if imports else set()
        self.patches: list[_JsPatch] = []

        def line_of(pos: int) -> int:
            return code.count("\n", 0, pos) + 1

        for m in _JS_MOCK.finditer(code):
            names = imports.get(_strip_ext(m.group(2)))
            if names and line_of(m.start()) in self.added:
                self.patches.append(_JsPatch(line_of(m.start()), m.group(2), frozenset(names)))
        for m in _JS_SPY.finditer(code):
            if line_of(m.start()) not in self.added:
                continue
            obj, name = m.group(1), m.group(3)
            if not self._mocked(code, m):
                continue
            root = obj.split(".")[0]
            names_hit = {name} if name in every else set()
            if root in every or names_hit:
                qualified = f"{obj}.{name}" if root in every else ""
                self.patches.append(
                    _JsPatch(line_of(m.start()), f"{obj}.{name}", frozenset(names_hit), qualified)
                )

    @staticmethod
    def _mocked(code: str, m: re.Match[str]) -> bool:
        """The spy is followed by .mockReturnValue/.mockResolvedValue/.mockImplementation,
        chained or through the variable it was assigned to."""
        if re.match(_JS_MOCKED, code[m.end() :]):
            return True
        assigned = re.search(r"(?:const|let|var)\s+([\w$]+)\s*=\s*$", code[: m.start()])
        return bool(assigned and re.search(rf"\b{re.escape(assigned.group(1))}{_JS_MOCKED}", code))

    def asserted(self, patch: _JsPatch, unit: Unit) -> str | None:
        """The imported name an `expect(...)` argument calls (`add(1, 2)`, `math.add(1, 2)`).
        Naming the mock without calling it (`expect(sendMail).toHaveBeenCalled()`) is fine."""
        for body in _expect_bodies(self.masked, unit.line, unit.end_line):
            if patch.qualified:
                found = re.search(rf"(?<![\w$.]){re.escape(patch.qualified)}\s*\(", body)
                if found:
                    return patch.qualified
            for name in sorted(patch.names):
                for m in re.finditer(rf"(?<![\w$.]){re.escape(name)}((?:\.[\w$]+)*)\s*\(", body):
                    if ".mock" not in m.group(1):
                        return name
        return None


def _js(
    unit: Unit, jsfile: _JsFile, spans: Sequence[tuple[int, int]]
) -> Iterator[tuple[int, str, str]]:
    for patch in jsfile.patches:
        inside = unit.line <= patch.line <= unit.end_line
        outside_all = not any(a <= patch.line <= b for a, b in spans)
        if not (inside or outside_all):
            continue
        hit = jsfile.asserted(patch, unit)
        if hit is not None:
            yield patch.line, patch.target, hit


# ---- the rule -----------------------------------------------------------------------------


def check_self_mocked(
    pairs: Sequence[tuple[Unit | None, Unit]],
    base_files: Mapping[str, FileAnalysis],
    cand_files: Mapping[str, FileAnalysis],
    changes: ChangeSet,
) -> list[Finding]:
    """`pairs` holds each collected candidate test with its base twin (None for a new test)."""
    texts = {f.path: f for f in changes.files}
    imports: dict[str, _Imports] = {}
    base_imports: dict[str, _Imports] = {}
    js_files: dict[str, _JsFile] = {}
    out: list[Finding] = []
    for base, unit in pairs:
        # A Python test whose body is unchanged cannot have gained a patch. A JS test can: an
        # added file-level jest.mock changes what every test below it runs against, and the
        # added-line check inside _JsFile already says whether the patch is new.
        if unit.engine == "python" and base is not None and base.body_hash == unit.body_hash:
            continue
        hits: list[tuple[int, str, str]] = []
        if unit.engine == "python" and unit.node is not None:
            analysis = cand_files.get(unit.path)
            if analysis is None or analysis.tree is None:
                continue
            if unit.path not in imports:
                imports[unit.path] = _imports(analysis.tree)
            before = base_files.get(base.path) if base is not None else None
            if before is not None and before.tree is not None and before.path not in base_imports:
                base_imports[before.path] = _imports(before.tree)
            hits = list(
                _python(
                    unit,
                    base,
                    imports[unit.path],
                    base_imports.get(before.path) if before is not None else None,
                )
            )
        elif unit.engine == "js" and unit.end_line:
            changed = texts.get(unit.path)
            if changed is None or changed.new_text is None:
                continue
            if unit.path not in js_files:
                js_files[unit.path] = _JsFile(changed.new_text, changed.old_text)
            spans = [(u.line, u.end_line) for u in cand_files[unit.path].units]
            hits = list(_js(unit, js_files[unit.path], spans))
        for line, target, call in hits:
            out.append(
                finding(
                    "tests.self_mocked",
                    unit.path,
                    line,
                    f"test {unit.name} patches {target} and asserts on {call}; it tests the mock",
                    _HINT,
                )
            )
    return out
