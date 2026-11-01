"""Python test analysis on the stdlib `ast` (SPEC 5.3 and 5.4)."""

from __future__ import annotations

import ast
import builtins
import copy
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
_CONDITIONAL = {"mark.skipif", "pytest.importorskip", "unittest.skipIf", "unittest.skipUnless"}
_KEEP_NAMES = set(dir(builtins)) | {"self", "cls", "pytest", "mock", "unittest"}
_KEEP_ATTRS = {"raises", "warns", "approx", "mark", "parametrize", "skip", "skipif", "xfail"}
_KEEP_ATTRS |= {"fail", "importorskip", "TestCase"}


def dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = dotted(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""


def _is_test_name(name: str, in_class: bool) -> bool:
    # Superset of what the runner collects; `collect.Collection` applies the configured prefixes.
    return name.startswith("test")


class _Renamer(ast.NodeTransformer):
    """Alpha-renames identifiers to `_0`, `_1`, ... by first occurrence; keeps builtins, the
    pytest vocabulary, `assert*` method names, constants, operators and keyword names."""

    def __init__(self) -> None:
        self.names: dict[str, str] = {}

    def _new(self, ident: str) -> str:
        return self.names.setdefault(ident, f"_{len(self.names)}")

    def visit_Name(self, node: ast.Name) -> ast.AST:
        if node.id not in _KEEP_NAMES:
            node.id = self._new(node.id)
        return node

    def visit_Attribute(self, node: ast.Attribute) -> ast.AST:
        self.generic_visit(node)
        if node.attr not in _KEEP_ATTRS and not node.attr.startswith("assert"):
            node.attr = self._new(node.attr)
        return node

    def visit_arg(self, node: ast.arg) -> ast.AST:
        node.arg = self._new(node.arg)
        node.annotation = None
        return node


def _shape(node: ast.AST) -> str:
    """Structural form of an expression or statement: identifiers renamed, rest kept."""
    return digest(ast.dump(_Renamer().visit(copy.deepcopy(node))))


def _has_call(node: ast.AST) -> bool:
    return any(isinstance(n, ast.Call) for n in ast.walk(node))


def _always_false(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return not node.value
    return (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, ast.Not)
        and _always_true(node.operand)
    )


def _always_true(node: ast.AST) -> bool:
    """A condition that cannot fail: a truthy constant, `x or True`, `x == x`, a non-empty tuple."""
    if isinstance(node, ast.Constant):
        return bool(node.value) and node.value is not Ellipsis
    if isinstance(node, ast.Tuple):
        return bool(node.elts)
    if isinstance(node, ast.BoolOp):
        parts = [_always_true(v) for v in node.values]
        return any(parts) if isinstance(node.op, ast.Or) else all(parts)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return _always_false(node.operand)
    if isinstance(node, ast.Compare) and len(node.ops) == 1:
        return isinstance(node.ops[0], (ast.Eq, ast.LtE, ast.GtE, ast.Is)) and _same(
            node.left, node.comparators[0]
        )
    return False


def _same(a: ast.AST, b: ast.AST) -> bool:
    """The same side-effect-free expression on both sides (calls are not assumed pure)."""
    return not _has_call(a) and ast.dump(a) == ast.dump(b)


def _vacuous_call(tail: str, node: ast.Call) -> bool:
    if tail == "assertTrue":
        return bool(node.args) and _always_true(node.args[0])
    if tail in _EQUAL_METHODS and len(node.args) >= 2:
        return _same(node.args[0], node.args[1])
    return False


_PURE_BUILTINS = frozenset(
    {
        "len",
        "abs",
        "min",
        "max",
        "sum",
        "sorted",
        "reversed",
        "list",
        "tuple",
        "set",
        "frozenset",
        "dict",
        "str",
        "int",
        "float",
        "bool",
        "round",
        "repr",
    }
)
_ASSERT_METHOD = re.compile(r"assert[A-Z]\w*")


class _Consts:
    """Which locals of one test body are constants, and whether an expression is constant.

    Deliberately conservative (SPEC 5.4): a local counts only when it is assigned exactly once,
    by a plain `name = <constant expression>` at the top level of the body, is not a parameter,
    loop/with/except/import/match target or nested definition, and is only ever read as an
    operand of an operator, comparison, container, f-string, pure builtin, assertion or
    `approx`. Calls, attribute access and subscripts on it make it non-constant.
    """

    def __init__(self, fn: _FuncDef | None = None) -> None:
        self.names: set[str] = set()
        self.stores: set[str] = set()
        if fn is not None:
            self._analyse(fn)

    def _analyse(self, fn: _FuncDef) -> None:
        counts: dict[str, int] = {}
        bad: set[str] = set()
        parents: dict[int, ast.AST] = {}

        def store(name: str | None) -> None:
            if name:
                counts[name] = counts.get(name, 0) + 1

        for node in ast.walk(fn):
            for child in ast.iter_child_nodes(node):
                parents[id(child)] = node
            if isinstance(node, ast.arg):
                store(node.arg)
            elif isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
                store(node.id)
                if isinstance(node.ctx, ast.Del):
                    bad.add(node.id)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node is not fn:
                    store(node.name)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    store(alias.asname or alias.name.split(".")[0])
            elif isinstance(node, ast.ExceptHandler):
                store(node.name)
            elif isinstance(node, (ast.Global, ast.Nonlocal)):
                bad.update(node.names)
            elif isinstance(node, (ast.MatchAs, ast.MatchStar)):
                store(node.name)
            elif isinstance(node, ast.MatchMapping):
                store(node.rest)
        self.stores = set(counts)

        candidates: dict[str, ast.Assign] = {}
        for stmt in fn.body:
            if (
                isinstance(stmt, ast.Assign)
                and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name)
            ):
                name = stmt.targets[0].id
                if counts.get(name) == 1 and name not in bad:
                    candidates[name] = stmt
        for node in ast.walk(fn):
            if (
                isinstance(node, ast.Name)
                and isinstance(node.ctx, ast.Load)
                and node.id in candidates
                and not self._benign_read(node, parents)
            ):
                del candidates[node.id]
        # Source order: a constant may only be built from constants defined before it.
        for stmt in fn.body:
            if (
                isinstance(stmt, ast.Assign)
                and isinstance(stmt.targets[0], ast.Name)
                and candidates.get(stmt.targets[0].id) is stmt
                and self.is_const(stmt.value)
            ):
                self.names.add(stmt.targets[0].id)

    def _benign_read(self, node: ast.Name, parents: dict[int, ast.AST]) -> bool:
        parent = parents.get(id(node))
        if isinstance(parent, ast.keyword):
            parent = parents.get(id(parent))
            return isinstance(parent, ast.Call) and self._neutral_call(parent)
        if isinstance(parent, ast.Call):
            return node is not parent.func and self._neutral_call(parent)
        return isinstance(
            parent,
            (
                ast.BinOp,
                ast.UnaryOp,
                ast.BoolOp,
                ast.Compare,
                ast.Tuple,
                ast.List,
                ast.Set,
                ast.Dict,
                ast.IfExp,
                ast.JoinedStr,
                ast.FormattedValue,
                ast.Assert,
            ),
        ) or (isinstance(parent, ast.Assign) and node is parent.value)

    def _pure_call(self, call: ast.Call) -> bool:
        return (
            isinstance(call.func, ast.Name)
            and call.func.id in _PURE_BUILTINS
            and call.func.id not in self.stores
        )

    def _neutral_call(self, call: ast.Call) -> bool:
        """A call that only reads its arguments: a pure builtin, an assertion, or approx."""
        tail = dotted(call.func).rsplit(".", 1)[-1]
        return self._pure_call(call) or tail == "approx" or bool(_ASSERT_METHOD.fullmatch(tail))

    def is_const(self, node: ast.AST | None) -> bool:
        if node is None or isinstance(node, ast.Constant):
            return True
        if isinstance(node, ast.Name):
            return node.id in self.names
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            return all(self.is_const(e) for e in node.elts)
        if isinstance(node, ast.Dict):
            return all(k is not None and self.is_const(k) for k in node.keys) and all(
                self.is_const(v) for v in node.values
            )
        if isinstance(node, ast.BinOp):
            return self.is_const(node.left) and self.is_const(node.right)
        if isinstance(node, ast.UnaryOp):
            return self.is_const(node.operand)
        if isinstance(node, (ast.BoolOp, ast.JoinedStr)):
            return all(self.is_const(v) for v in node.values)
        if isinstance(node, ast.FormattedValue):
            return self.is_const(node.value) and self.is_const(node.format_spec)
        if isinstance(node, ast.Compare):
            return self.is_const(node.left) and all(self.is_const(c) for c in node.comparators)
        if isinstance(node, ast.IfExp):
            return all(self.is_const(n) for n in (node.test, node.body, node.orelse))
        if isinstance(node, ast.Call):
            tail = dotted(node.func).rsplit(".", 1)[-1]
            if not (self._pure_call(node) or tail == "approx"):
                return False
            return all(self.is_const(a) for a in node.args) and all(
                self.is_const(k.value) for k in node.keywords
            )
        return False


def _is_raises_with(node: ast.With | ast.AsyncWith) -> bool:
    return any(
        isinstance(i.context_expr, ast.Call)
        and (t := dotted(i.context_expr.func).rsplit(".", 1)[-1])
        and (t in {"raises", "warns"} or t in _RAISES_METHODS)
        for i in node.items
    )


def _prune(fn: _FuncDef) -> list[ast.stmt]:
    """Remove statements that can never run (after an unconditional return/raise outside a
    raises-block, or under `if False:`) from `fn` in place; return what was removed."""
    dropped: list[ast.stmt] = []

    def walk(stmts: list[ast.stmt], in_raises: bool) -> None:
        for i, stmt in enumerate(stmts):
            if isinstance(stmt, ast.If) and _always_false(stmt.test):
                dropped.extend(stmt.body)
                stmt.body = [ast.copy_location(ast.Pass(), stmt)]
            for name in ("body", "orelse", "finalbody"):
                inner = getattr(stmt, name, None)
                if isinstance(inner, list) and inner and isinstance(inner[0], ast.stmt):
                    raises = isinstance(stmt, (ast.With, ast.AsyncWith)) and _is_raises_with(stmt)
                    walk(inner, in_raises or (raises and name == "body"))
            for handler in getattr(stmt, "handlers", []):
                walk(handler.body, in_raises)
            if isinstance(stmt, (ast.Return, ast.Raise)) and not in_raises:
                dropped.extend(stmts[i + 1 :])
                del stmts[i + 1 :]
                return

    walk(fn.body, False)
    return dropped


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

    def __init__(
        self,
        helpers: dict[str, _FuncDef],
        depth: int = 0,
        stack: tuple[str, ...] = (),
        consts: _Consts | None = None,
    ):
        self.helpers = helpers
        self.consts = consts or _Consts()
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
        self.shapes: list[str] = []
        self.forms: list[str] = []
        self.vacuous = 0
        self.vacuous_line = 0
        self.strong_operands: set[str] = set()
        self.weak_operands: set[str] = set()
        self.first_line = 0

    # -- assertions ---------------------------------------------------------------------
    def _counted(self, node: ast.AST) -> None:
        self.asserts += 1
        self.first_line = self.first_line or getattr(node, "lineno", 0)
        self.shapes.append(_shape(node))
        self.forms.append(digest(ast.dump(node)))

    def visit_Assert(self, node: ast.Assert) -> None:
        test = node.test
        if _always_true(test) or self.consts.is_const(
            test
        ):  # vacuous: it can never fail, so it checks nothing
            self.vacuous += 1
            self.vacuous_line = self.vacuous_line or node.lineno
            self.first_line = self.first_line or node.lineno
            return
        self._counted(node.test)
        if isinstance(test, ast.Compare):
            if (
                len(test.ops) == 1
                and isinstance(test.ops[0], ast.IsNot)
                and isinstance(test.comparators[0], ast.Constant)
                and test.comparators[0].value is None
            ):
                self.weak += 1
                self.weak_operands.add(_shape(test.left))
            else:
                self.strong += 1
                self.strong_operands.update(_shape(o) for o in (test.left, *test.comparators))
            if len(test.ops) == 1 and isinstance(test.ops[0], ast.Eq):
                for side in (test.left, test.comparators[0]):
                    lit = _expected_of(side)
                    if lit is not None:
                        self.expected.append((ast.dump(lit), node.lineno))
        elif isinstance(test, (ast.Name, ast.Attribute, ast.Subscript)):
            self.weak += 1
            self.weak_operands.add(_shape(test))
        elif isinstance(test, ast.Call):
            # A bare truthiness check on a call is weak; type checks pin a concrete fact.
            if dotted(test.func) in {"isinstance", "issubclass"}:
                self.strong += 1
            else:
                self.weak += 1
                self.weak_operands.add(_shape(test))
        self.generic_visit(node)

    def _raises(self, call: ast.Call) -> None:
        self._counted(call)
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
            if _vacuous_call(tail, node) or self._const_call(node):
                self.vacuous += 1
                self.vacuous_line = self.vacuous_line or node.lineno
                self.first_line = self.first_line or node.lineno
                return
            self._counted(node)
            if tail in _WEAK_METHODS:
                self.weak += 1
                if node.args:
                    self.weak_operands.add(_shape(node.args[0]))
            else:
                self.strong += 1
                self.strong_operands.update(_shape(a) for a in node.args[:2])
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
            self._counted(node)
        elif tail == "approx":
            for kw in node.keywords:
                if kw.arg in {"rel", "abs"}:
                    self._tolerance(kw.arg, _number(kw.value))
        else:
            self._credit_helper(node)
        self.generic_visit(node)

    def _const_call(self, node: ast.Call) -> bool:
        """`self.assertEqual(4, 4)`: every argument is a constant, so nothing is checked."""
        return (
            bool(node.args)
            and all(self.consts.is_const(a) for a in node.args)
            and all(self.consts.is_const(k.value) for k in node.keywords)
        )

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
        sub = _Stats(
            self.helpers, self.depth + 1, (*self.stack, target), _Consts(self.helpers[target])
        )
        for stmt in self.helpers[target].body:
            sub.visit(stmt)
        self.asserts += sub.asserts
        self.strong += sub.strong
        self.weak += sub.weak
        self.broad += sub.broad
        self.specific += sub.specific
        self.shapes.extend(sub.shapes)
        self.forms.extend(sub.forms)
        self.vacuous += sub.vacuous

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


def _struct_hash(fn: _FuncDef) -> str:
    clone = copy.deepcopy(fn)
    body = list(clone.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    renamer = _Renamer()
    args = renamer.visit(clone.args)
    return digest(ast.dump(args) + "".join(ast.dump(renamer.visit(s)) for s in body))


def _unit(
    path: str,
    key: str,
    fn: _FuncDef,
    helpers: dict[str, _FuncDef],
    classes: tuple[str, ...],
    testcase: bool,
    disabled: bool,
) -> Unit:
    live = copy.deepcopy(fn)
    dropped = _prune(live)
    stats = _Stats(helpers, consts=_Consts(live))
    for stmt in live.body:
        stats.visit(stmt)
    lost = _Stats(helpers)
    for stmt in dropped:
        lost.visit(stmt)
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
        struct_hash=_struct_hash(fn),
        shapes=stats.shapes,
        assertion_forms=stats.forms,
        vacuous=stats.vacuous,
        vacuous_line=stats.vacuous_line,
        unreachable=lost.asserts,
        unreachable_line=lost.first_line,
        strong_operands=stats.strong_operands,
        weak_operands=stats.weak_operands,
        classes=classes,
        testcase=testcase,
        disabled=disabled,
    )


def _test_disabled(body: Sequence[ast.stmt]) -> ast.stmt | None:
    """The `__test__ = False` assignment in a module or class body, if any."""
    for node in body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            if (
                any(isinstance(t, ast.Name) and t.id == "__test__" for t in targets)
                and isinstance(value, ast.Constant)
                and value.value is False
            ):
                return node
    return None


def _collect_units(
    path: str,
    body: Sequence[ast.stmt],
    prefix: str,
    helpers: dict[str, _FuncDef],
    out: list[Unit],
    classes: tuple[str, ...] = (),
    testcase: bool = False,
    disabled: bool = False,
) -> None:
    in_class = bool(prefix)
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if _is_test_name(node.name, in_class):
                unit = _unit(
                    path, f"{prefix}{node.name}", node, helpers, classes, testcase, disabled
                )
                out.append(unit)
        elif isinstance(node, ast.ClassDef):
            case = any(dotted(b).rsplit(".", 1)[-1].endswith("TestCase") for b in node.bases)
            _collect_units(
                path,
                node.body,
                f"{prefix}{node.name}::",
                helpers,
                out,
                (*classes, node.name),
                testcase or case,
                disabled or _test_disabled(node.body) is not None,
            )


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
            Marker(
                self.scope,
                kind,
                kind,
                getattr(node, "lineno", 0),
                "tests.skipped",
                kind in _CONDITIONAL,
            )
        )

    def visit_Assign(self, node: ast.Assign) -> None:
        if not self.in_unit and _test_disabled([node]) is not None:
            self.found.append(
                Marker(self.scope, "__test__", "__test__ = False", node.lineno, "tests.skipped")
            )
        self.generic_visit(node)

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
    _collect_units(
        path, tree.body, "", helpers, analysis.units, disabled=_test_disabled(tree.body) is not None
    )
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
