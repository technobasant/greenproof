"""Python test analysis on the stdlib `ast` (SPEC 5.3 and 5.4)."""

from __future__ import annotations

import ast
import posixpath
import re
from collections.abc import Sequence

from greenproof.rules._common import digest
from greenproof.rules.tests_model import FileAnalysis, Marker, Unit

_MARK_SUFFIXES = ("mark.skip", "mark.skipif", "mark.xfail")
_FULL_KINDS = {
    "pytest.skip",
    "pytest.xfail",
    "pytest.importorskip",
    "unittest.skip",
    "unittest.skipIf",
    "unittest.skipUnless",
    "unittest.expectedFailure",
    "unittest.SkipTest",
}
_WEAK_METHODS = {"assertTrue", "assertIsNotNone"}
_RAISES_METHODS = {"assertRaises", "assertRaisesRegex", "assertWarns", "assertWarnsRegex"}
_BROAD = {"Exception", "BaseException", "Warning"}
_SWALLOWED = {"AssertionError", "Exception", "BaseException"}
_ALMOST = {"assertAlmostEqual", "assertAlmostEquals", "assertNotAlmostEqual"}
_EQUAL_METHODS = {
    "assertEqual",
    "assertEquals",
    "assertListEqual",
    "assertDictEqual",
    "assertTupleEqual",
    "assertSetEqual",
    "assertSequenceEqual",
    "assertCountEqual",
    "assertAlmostEqual",
    "assertAlmostEquals",
}
_FuncDef = ast.FunctionDef | ast.AsyncFunctionDef


def dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = dotted(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""


def _is_test_name(name: str, in_class: bool) -> bool:
    return name.startswith("test") if in_class else name.startswith("test_")


def _number(node: ast.AST) -> float | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        if isinstance(node.value, bool):
            return None
        return float(node.value)
    if (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, ast.USub)
        and (inner := _number(node.operand)) is not None
    ):
        return -inner
    return None


def _is_literal(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return isinstance(node.value, (int, float, str, bytes)) and not isinstance(node.value, bool)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return _is_literal(node.operand)
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return all(_is_literal(e) for e in node.elts)
    if isinstance(node, ast.Dict):
        return all(k is not None and _is_literal(k) for k in node.keys) and all(
            _is_literal(v) for v in node.values
        )
    return False


def _expected_of(node: ast.AST) -> ast.AST | None:
    """The literal on the expected side of `x == LIT`, `x == approx(LIT)` or None."""
    if _is_literal(node):
        return node
    if isinstance(node, ast.Call) and dotted(node.func).endswith("approx") and node.args:
        return node.args[0] if _is_literal(node.args[0]) else None
    return None


class _Stats(ast.NodeVisitor):
    """Assertion statistics for one test body, crediting same-module helper functions."""

    def __init__(self, helpers: dict[str, _FuncDef], depth: int = 0, stack: tuple[str, ...] = ()):
        self.helpers = helpers
        self.depth = depth
        self.stack = stack
        self.asserts = 0
        self.strong = 0
        self.weak = 0
        self.broad = 0
        self.specific = 0
        self.tolerances: dict[str, float] = {}
        self.swallowed = 0
        self.swallow_line = 0
        self.expected: list[tuple[str, int]] = []

    # -- assertions ---------------------------------------------------------------------
    def visit_Assert(self, node: ast.Assert) -> None:
        self.asserts += 1
        test = node.test
        if isinstance(test, ast.Compare):
            if (
                len(test.ops) == 1
                and isinstance(test.ops[0], ast.IsNot)
                and isinstance(test.comparators[0], ast.Constant)
                and test.comparators[0].value is None
            ):
                self.weak += 1
            else:
                self.strong += 1
            if len(test.ops) == 1 and isinstance(test.ops[0], ast.Eq):
                for side in (test.left, test.comparators[0]):
                    lit = _expected_of(side)
                    if lit is not None:
                        self.expected.append((ast.dump(lit), node.lineno))
        elif isinstance(test, (ast.Name, ast.Attribute, ast.Subscript)):
            self.weak += 1
        elif isinstance(test, ast.Call):
            # A bare truthiness check on a call is weak; type checks pin a concrete fact.
            if dotted(test.func) in {"isinstance", "issubclass"}:
                self.strong += 1
            else:
                self.weak += 1
        self.generic_visit(node)

    def _raises(self, call: ast.Call) -> None:
        self.asserts += 1
        arg = call.args[0] if call.args else None
        names = (
            [dotted(e) for e in arg.elts]
            if isinstance(arg, ast.Tuple)
            else ([dotted(arg)] if arg is not None else [])
        )
        if any(n.rsplit(".", 1)[-1] in _BROAD for n in names):
            self.broad += 1
        else:
            self.specific += 1

    def _tolerance(self, kind: str, value: float | None) -> None:
        if value is None:
            return
        have = self.tolerances.get(kind)
        if kind == "places":
            self.tolerances[kind] = value if have is None else min(have, value)
        else:
            self.tolerances[kind] = value if have is None else max(have, value)

    def visit_Call(self, node: ast.Call) -> None:
        name = dotted(node.func)
        tail = name.rsplit(".", 1)[-1]
        if tail in {"raises", "warns"} or tail in _RAISES_METHODS:
            self._raises(node)
        elif re.fullmatch(r"assert[A-Z]\w*", tail):
            self.asserts += 1
            if tail in _WEAK_METHODS:
                self.weak += 1
            else:
                self.strong += 1
            if tail in _EQUAL_METHODS:
                for arg in node.args[:2]:
                    lit = _expected_of(arg)
                    if lit is not None:
                        self.expected.append((ast.dump(lit), node.lineno))
            if tail in _ALMOST:
                for kw in node.keywords:
                    if kw.arg in {"places", "delta"}:
                        self._tolerance(kw.arg, _number(kw.value))
                if len(node.args) >= 3:
                    self._tolerance("places", _number(node.args[2]))
        elif tail.startswith("assert_"):
            self.asserts += 1
        elif tail == "approx":
            for kw in node.keywords:
                if kw.arg in {"rel", "abs"}:
                    self._tolerance(kw.arg, _number(kw.value))
        else:
            self._credit_helper(node)
        self.generic_visit(node)

    def _credit_helper(self, node: ast.Call) -> None:
        func = node.func
        target: str | None = None
        if isinstance(func, ast.Name):
            target = func.id
        elif isinstance(func, ast.Attribute) and dotted(func.value) in {"self", "cls"}:
            target = func.attr
        if target is None or target not in self.helpers or self.depth >= 2:
            return
        if target in self.stack:
            return
        sub = _Stats(self.helpers, self.depth + 1, (*self.stack, target))
        for stmt in self.helpers[target].body:
            sub.visit(stmt)
        self.asserts += sub.asserts
        self.strong += sub.strong
        self.weak += sub.weak
        self.broad += sub.broad
        self.specific += sub.specific

    # -- swallowing ---------------------------------------------------------------------
    def _swallows(self, handler: ast.ExceptHandler) -> bool:
        caught: list[str] = []
        if handler.type is None:
            caught = ["BaseException"]
        elif isinstance(handler.type, ast.Tuple):
            caught = [dotted(e) for e in handler.type.elts]
        else:
            caught = [dotted(handler.type)]
        if not any(c.rsplit(".", 1)[-1] in _SWALLOWED for c in caught):
            return False
        return not any(isinstance(n, ast.Raise) for s in handler.body for n in ast.walk(s))

    def _count_inside(self, body: Sequence[ast.stmt], line: int) -> None:
        inner = _Stats(self.helpers, self.depth, self.stack)
        for stmt in body:
            inner.visit(stmt)
        if inner.asserts:
            self.swallowed += inner.asserts
            self.swallow_line = self.swallow_line or line

    def _try(self, node: ast.Try | ast.TryStar) -> None:
        if any(self._swallows(h) for h in node.handlers):
            self._count_inside(node.body, node.lineno)
        self.generic_visit(node)

    def visit_Try(self, node: ast.Try) -> None:
        self._try(node)

    def visit_TryStar(self, node: ast.TryStar) -> None:
        self._try(node)

    def visit_With(self, node: ast.With) -> None:
        for item in node.items:
            ctx = item.context_expr
            if (
                isinstance(ctx, ast.Call)
                and dotted(ctx.func).rsplit(".", 1)[-1] == "suppress"
                and any(dotted(a).rsplit(".", 1)[-1] in _SWALLOWED for a in ctx.args)
            ):
                self._count_inside(node.body, node.lineno)
        self.generic_visit(node)


def _parametrize_cases(fn: _FuncDef) -> int | None:
    total: int | None = None
    for dec in fn.decorator_list:
        if not (isinstance(dec, ast.Call) and dotted(dec.func).endswith("mark.parametrize")):
            continue
        values: ast.AST | None = dec.args[1] if len(dec.args) > 1 else None
        for kw in dec.keywords:
            if kw.arg == "argvalues":
                values = kw.value
        if isinstance(values, (ast.List, ast.Tuple)) and not any(
            isinstance(e, ast.Starred) for e in values.elts
        ):
            total = len(values.elts) * (total if total is not None else 1)
        else:
            return None
    return total


def _body_hash(fn: _FuncDef) -> str:
    body = list(fn.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    return digest(ast.dump(fn.args) + "".join(ast.dump(s) for s in body))


def _unit(path: str, key: str, fn: _FuncDef, helpers: dict[str, _FuncDef]) -> Unit:
    stats = _Stats(helpers)
    for stmt in fn.body:
        stats.visit(stmt)
    return Unit(
        path=path,
        key=key,
        line=fn.lineno,
        body_hash=_body_hash(fn),
        engine="python",
        asserts=stats.asserts,
        strong=stats.strong,
        weak=stats.weak,
        broad_raises=stats.broad,
        specific_raises=stats.specific,
        tolerances=stats.tolerances,
        param_cases=_parametrize_cases(fn),
        swallowed=stats.swallowed,
        swallow_line=stats.swallow_line,
        expected=stats.expected,
    )


def _collect_units(
    path: str, body: Sequence[ast.stmt], prefix: str, helpers: dict[str, _FuncDef], out: list[Unit]
) -> None:
    in_class = bool(prefix)
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if _is_test_name(node.name, in_class):
                out.append(_unit(path, f"{prefix}{node.name}", node, helpers))
        elif isinstance(node, ast.ClassDef):
            _collect_units(path, node.body, f"{prefix}{node.name}::", helpers, out)


class _Markers(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self.path = path
        self.module_scope = f"{path}::<module>"
        self.scope = self.module_scope
        self.in_unit = False
        self.prefix = ""
        self.found: list[Marker] = []

    def _add(self, kind: str, node: ast.AST) -> None:
        self.found.append(
            Marker(self.scope, kind, kind, getattr(node, "lineno", 0), "tests.skipped")
        )

    def visit_Attribute(self, node: ast.Attribute) -> None:
        name = dotted(node)
        if name in _FULL_KINDS or (name.endswith(".skipTest") and name.startswith("self")):
            self._add(name, node)
        else:
            for suffix in _MARK_SUFFIXES:
                if name == suffix or name.endswith("." + suffix):
                    self._add(suffix, node)
                    break
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id == "SkipTest":
            self._add("unittest.SkipTest", node)

    def _enter(self, node: _FuncDef | ast.ClassDef, scope: str, unit: bool) -> None:
        saved = (self.scope, self.in_unit, self.prefix)
        self.scope, self.in_unit = scope, unit
        for dec in node.decorator_list:
            self.visit(dec)
        if isinstance(node, ast.ClassDef):
            self.prefix = f"{scope}::"
        for stmt in node.body:
            self.visit(stmt)
        self.scope, self.in_unit, self.prefix = saved

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        if self.in_unit:
            self.generic_visit(node)
            return
        in_class = bool(self.prefix)
        scope = f"{self.prefix}{node.name}" if in_class else node.name
        self._enter(node, scope, False)

    def _function(self, node: _FuncDef) -> None:
        if self.in_unit:
            self.generic_visit(node)
        elif _is_test_name(node.name, bool(self.prefix)):
            self._enter(node, f"{self.prefix}{node.name}", True)
        else:
            self._enter(node, self.scope, False)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._function(node)


def _conftest_markers(path: str, tree: ast.Module) -> list[Marker]:
    scope = f"{path}::<module>"
    out: list[Marker] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == "pytest_collection_modifyitems":
                out.append(Marker(scope, node.name, node.name, node.lineno, "tests.skipped"))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id in {
                    "collect_ignore",
                    "collect_ignore_glob",
                }:
                    out.append(Marker(scope, target.id, target.id, node.lineno, "tests.skipped"))
    return out


def analyse_python(path: str, text: str) -> FileAnalysis | None:
    """The file's tests and markers, or None when it does not parse (callers fall back)."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return None
    helpers: dict[str, _FuncDef] = {
        n.name: n
        for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and not n.name.startswith("test")
    }
    for cls in tree.body:
        if isinstance(cls, ast.ClassDef):
            for n in cls.body:
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and not n.name.startswith(
                    "test"
                ):
                    helpers.setdefault(n.name, n)
    analysis = FileAnalysis(path, "python")
    _collect_units(path, tree.body, "", helpers, analysis.units)
    visitor = _Markers(path)
    visitor.visit(tree)
    analysis.markers = visitor.found
    if posixpath.basename(path) == "conftest.py":
        analysis.markers.extend(_conftest_markers(path, tree))
    return analysis


# Display names for messages.
def marker_display(kind: str) -> str:
    if kind.startswith("mark."):
        return f"@pytest.{kind}"
    return kind
