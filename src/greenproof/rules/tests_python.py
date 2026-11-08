"""Python test analysis on the stdlib `ast` (SPEC 5.3 and 5.4)."""

from __future__ import annotations

import ast
import builtins
import copy
import math
import posixpath
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from typing import Protocol

from greenproof.rules._common import digest
from greenproof.rules.tests_model import FileAnalysis, Fixture, Marker, Unit
from greenproof.rules.tests_pyrows import param_names, row_gap

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
_EXACT_METHODS = _EQUAL_METHODS | {"assertIs", "assertIsNone", "assertMultiLineEqual"}
_LOOSE_PAIR_METHODS = {
    "assertNotEqual",
    "assertNotEquals",
    "assertNotAlmostEqual",
    "assertGreater",
    "assertGreaterEqual",
    "assertLess",
    "assertLessEqual",
    "assertNotIn",
}
_EXACT_OPS = (ast.Eq, ast.Is)
_INEXACT_OPS = (ast.NotEq, ast.IsNot)
_ORDER_OPS = (ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.NotIn)
_WRAPPERS = frozenset({"type", "len", "bool"})
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


class _Locals(ast.NodeTransformer):
    """Alpha-renames only the names bound inside the test; globals, attributes and builtins keep
    their spelling, so `f(x)` and `g(x)` or `resp.status` and `resp.ok` stay different."""

    def __init__(self, bound: set[str]) -> None:
        self.bound = bound
        self.names: dict[str, str] = {}

    def visit_Name(self, node: ast.Name) -> ast.AST:
        if node.id in self.bound and node.id not in _KEEP_NAMES:
            node.id = self.names.setdefault(node.id, f"_{len(self.names)}")
        return node


def _has_call(node: ast.AST) -> bool:
    return any(isinstance(n, ast.Call) for n in ast.walk(node))


def _call_keys(node: ast.AST, stores: set[str]) -> set[str]:
    """A key for each call inside `node`, locals alpha-renamed, so the same call in two tests
    (or two revisions of one) gets the same key."""
    return {
        digest(ast.dump(_Locals(stores).visit(copy.deepcopy(n))))
        for n in ast.walk(node)
        if isinstance(n, ast.Call)
    }


_Same = Callable[[ast.AST, ast.AST], bool]


def _empty_display(node: ast.AST) -> bool:
    """A `[]`, `()`, `{}` display with nothing in it: falsy."""
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return not node.elts
    return isinstance(node, ast.Dict) and not node.keys


def _always_false(node: ast.AST, same: _Same | None = None) -> bool:
    if isinstance(node, ast.Constant):
        return not node.value
    if _empty_display(node):
        return True
    return (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, ast.Not)
        and _always_true(node.operand, same)
    )


def _truthy_display(node: ast.AST) -> bool:
    """A display with at least one element that is not a `*unpack` (which may be empty)."""
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return any(not isinstance(e, ast.Starred) for e in node.elts)
    return isinstance(node, ast.Dict) and any(k is not None for k in node.keys)


def _always_true(node: ast.AST, same: _Same | None = None) -> bool:
    """A condition that cannot fail: a truthy constant, `x or True`, `x == x`, a non-empty
    display (the classic `assert (x, "msg")`), a non-empty f-string, `not []`.
    `same` decides whether two sides are the same expression (default: `_same`)."""
    same = same or _same
    if isinstance(node, ast.Constant):
        return bool(node.value) and node.value is not Ellipsis
    if _truthy_display(node):
        return True
    if isinstance(node, ast.JoinedStr):
        return any(
            isinstance(v, ast.Constant) and isinstance(v.value, str) and v.value
            for v in node.values
        )
    if isinstance(node, ast.BoolOp):
        parts = [_always_true(v, same) for v in node.values]
        return any(parts) if isinstance(node.op, ast.Or) else all(parts)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return _always_false(node.operand, same)
    if isinstance(node, ast.Compare) and len(node.ops) == 1:
        return isinstance(node.ops[0], (ast.Eq, ast.LtE, ast.GtE, ast.Is)) and same(
            node.left, node.comparators[0]
        )
    return False


# Calls that give a different answer each time: `next(it) == next(it)` is not a tautology.
_IMPURE_TAILS = frozenset(
    {
        "next",
        "pop",
        "popleft",
        "popitem",
        "readline",
        "recv",
        "random",
        "randint",
        "randrange",
        "choice",
        "getrandbits",
        "uuid1",
        "uuid4",
        "time",
        "time_ns",
        "now",
        "monotonic",
        "perf_counter",
        "get_nowait",
        "utcnow",
        "today",
        "urandom",
        "token_hex",
        "token_bytes",
        "rand",
        "randn",
        "uniform",
        "sample",
        "shuffle",
    }
)


def _stateful(node: ast.AST) -> bool:
    for n in ast.walk(node):
        if isinstance(n, (ast.Await, ast.Yield, ast.YieldFrom, ast.NamedExpr)):
            return True
        if isinstance(n, ast.Call):
            func = n.func
            tail = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if tail in _IMPURE_TAILS:
                return True
    return False


def _same(a: ast.AST, b: ast.AST) -> bool:
    """The same expression on both sides, so comparing it with itself checks nothing. An operand
    with a call is never the same: `get_settings() is get_settings()` pins a singleton and
    `Money(5) == Money(5)` runs `__eq__`. Names, attributes, subscripts and literals are."""
    return not _has_call(a) and not _has_call(b) and ast.dump(a) == ast.dump(b) and not _stateful(a)


def _vacuous_call(tail: str, node: ast.Call, same: _Same | None = None) -> bool:
    if tail == "assertTrue":
        return bool(node.args) and _always_true(node.args[0], same)
    if (tail in _EQUAL_METHODS or tail == "assertIs") and len(node.args) >= 2:
        return (same or _same)(node.args[0], node.args[1])
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
        self.assigned: dict[str, ast.expr] = {}
        self.assigned_line: dict[str, int] = {}
        self.other_reads: dict[str, list[int]] = {}  # reads outside any assertion, by line
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
                    self.assigned[name] = stmt.value
                    self.assigned_line[name] = stmt.lineno
        in_assert: set[int] = set()
        for node in ast.walk(fn):
            if isinstance(node, ast.Assert) or (
                isinstance(node, ast.Call)
                and _ASSERT_METHOD.fullmatch(dotted(node.func).rsplit(".", 1)[-1])
            ):
                in_assert.update(id(n) for n in ast.walk(node))
        for node in ast.walk(fn):
            if (
                isinstance(node, ast.Name)
                and isinstance(node.ctx, ast.Load)
                and node.id in self.assigned
                and id(node) not in in_assert
            ):
                self.other_reads.setdefault(node.id, []).append(node.lineno)
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

    def _usable(self, name: str, before: int | None) -> bool:
        """With `before` set, only a local assigned earlier than that line and not read before it
        outside an assertion (SPEC 5.4, vacuous comparisons)."""
        if before is None:
            return True
        return self.assigned_line[name] < before and not any(
            line < before for line in self.other_reads.get(name, ())
        )

    def inline(self, node: ast.AST, depth: int = 0, before: int | None = None) -> ast.AST:
        """A copy of `node` with single-assignment locals replaced by what they were assigned,
        so `got = parse("4"); assert got == 4` and `assert parse("4") == 4` share operands."""
        outer = self

        class _Inline(ast.NodeTransformer):
            def visit_Name(self, n: ast.Name) -> ast.AST:
                if (
                    isinstance(n.ctx, ast.Load)
                    and n.id in outer.assigned
                    and depth < 3
                    and outer._usable(n.id, before)
                ):
                    return outer.inline(outer.assigned[n.id], depth + 1, before)
                return n

        result: ast.AST = _Inline().visit(copy.deepcopy(node))
        return result

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
        try:
            value = float(node.value)
        except OverflowError:  # an int too large for a float (`0x` and 512 hex digits)
            return None
        return value if math.isfinite(value) else None
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


_APPROX_TIGHT = 1e-6  # the loosest approx tolerance still credited as pinning a value


def _approx_arg(node: ast.AST) -> ast.expr | None:
    """The expected value of `approx(<value>)` when its tolerance is the default or no looser
    than `_APPROX_TIGHT` (literal `rel=` and `abs=` only); None for anything else."""
    if not (
        isinstance(node, ast.Call)
        and dotted(node.func).rsplit(".", 1)[-1] == "approx"
        and len(node.args) == 1
    ):
        return None
    for kw in node.keywords:
        if kw.arg not in {"rel", "abs"}:
            return None
        tol = _number(kw.value)
        if tol is None or not 0 <= tol <= _APPROX_TIGHT:
            return None
    return node.args[0]


def _is_pinned(node: ast.AST) -> bool:
    """A literal an exact check can pin a value to: a constant, a negated number, a display of
    literals, or `approx(<literal>)` with a tight tolerance."""
    if isinstance(node, ast.Constant):
        return node.value is not Ellipsis
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        return _number(node) is not None or _is_pinned(node.operand)
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return all(_is_pinned(e) for e in node.elts)
    if isinstance(node, ast.Dict):
        return all(k is not None and _is_pinned(k) for k in node.keys) and all(
            _is_pinned(v) for v in node.values
        )
    arg = _approx_arg(node)
    return arg is not None and _is_pinned(arg)


def _pinned_expr(node: ast.AST) -> bool:
    """`_is_pinned`, or a value built from literals (`_constructed`)."""
    return _is_pinned(node) or _constructed(node)


def _constructed(node: ast.AST) -> bool:
    """`QueryParams("a=1")`, `Decimal("1.5")`: a capitalised callable built from literals alone, a
    value the test spells out. A call of a lower-case function is computed, not pinned."""
    if not (isinstance(node, ast.Call) and (node.args or node.keywords)):
        return False
    name = dotted(node.func).rsplit(".", 1)[-1]
    return (
        name[:1].isupper()
        and not name.isupper()
        and all(not isinstance(a, ast.Starred) and _pinned_expr(a) for a in node.args)
        and all(k.arg is not None and _pinned_expr(k.value) for k in node.keywords)
    )


def _literal_params(fn: _FuncDef) -> set[str]:
    """Parametrize arguments whose cases are all literals."""
    names: dict[str, bool] = {}
    for dec in fn.decorator_list:
        if not (isinstance(dec, ast.Call) and dotted(dec.func).endswith("mark.parametrize")):
            continue
        argnames = dec.args[0] if dec.args else None
        values: ast.AST | None = dec.args[1] if len(dec.args) > 1 else None
        for kw in dec.keywords:
            if kw.arg == "argnames":
                argnames = kw.value
            elif kw.arg == "argvalues":
                values = kw.value
        listed: list[str] = []
        if isinstance(argnames, ast.Constant) and isinstance(argnames.value, str):
            listed = [n.strip() for n in argnames.value.split(",") if n.strip()]
        elif isinstance(argnames, (ast.List, ast.Tuple)):
            listed = [
                e.value
                for e in argnames.elts
                if isinstance(e, ast.Constant) and isinstance(e.value, str)
            ]
        cases = values.elts if isinstance(values, (ast.List, ast.Tuple)) else None
        for i, name in enumerate(listed):
            ok = cases is not None and all(_case_literal(c, i, len(listed)) for c in cases)
            names[name] = names.get(name, True) and ok
    return {n for n, ok in names.items() if ok}


def _case_literal(case: ast.expr, index: int, width: int) -> bool:
    if isinstance(case, ast.Call) and dotted(case.func).rsplit(".", 1)[-1] == "param":
        case = (
            ast.Tuple(elts=list(case.args), ctx=ast.Load())
            if width > 1
            else (case.args[0] if case.args else case)
        )
    if width == 1:
        return _pinned_expr(case)
    if isinstance(case, (ast.Tuple, ast.List)) and len(case.elts) == width:
        return _pinned_expr(case.elts[index])
    return False


def _expected_of(node: ast.AST) -> ast.AST | None:
    """The literal on the expected side of `x == LIT`, `x == approx(LIT)` or None."""
    if _is_literal(node):
        return node
    if isinstance(node, ast.Call) and dotted(node.func).endswith("approx") and node.args:
        return node.args[0] if _is_literal(node.args[0]) else None
    return None


class _Helpers(dict[str, _FuncDef]):
    """The functions a test may call and get credit for, by the name it calls them with: the
    file's own top-level helpers and the functions of the test-path modules it imports. `scopes`
    gives, for an imported function, the helpers of the module it lives in; `swallowers` names
    those whose body swallows assertion failures (a decorator that does is a tamper)."""

    def __init__(self) -> None:
        super().__init__()
        self.scopes: dict[str, dict[str, _FuncDef]] = {}
        self.swallowers: set[str] = set()
        # The file's own plain functions defined exactly once at module level, whose calls are
        # read as if inlined (their checks pin values, their calls are subjects), and the
        # module-level names bound once to a literal list or tuple (a table of rows).
        self.unique: set[str] = set()
        self.tables: dict[str, ast.expr] = {}


def _handler_swallows(handler: ast.ExceptHandler) -> bool:
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


def _own_nodes(fn: _FuncDef) -> Iterator[ast.AST]:
    """The nodes of a function body, not descending into nested defs, lambdas or classes."""
    todo: list[ast.AST] = list(fn.body)
    while todo:
        node = todo.pop()
        yield node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            todo.extend(ast.iter_child_nodes(node))


def _param_names(fn: _FuncDef) -> set[str]:
    a = fn.args
    names = {p.arg for p in (*a.posonlyargs, *a.args, *a.kwonlyargs)}
    names.update(p.arg for p in (a.vararg, a.kwarg) if p is not None)
    return names


def _calls_any(nodes: Sequence[ast.stmt], names: set[str]) -> bool:
    """A statement of `nodes` calls one of `names` (`f(...)`, `f.__wrapped__(...)`)."""
    for stmt in nodes:
        for n in ast.walk(stmt):
            if isinstance(n, ast.Call):
                root = n.func
                while isinstance(root, (ast.Attribute, ast.Call, ast.Subscript)):
                    root = root.func if isinstance(root, ast.Call) else root.value
                if isinstance(root, ast.Name) and root.id in names:
                    return True
    return False


def _fn_swallows(fn: _FuncDef) -> bool:
    """The decorator `fn` swallows assertion failures (SPEC 5.4): one of its wrappers has a `try`
    whose body calls the wrapped callable (a parameter of the decorator) and a handler that
    catches AssertionError, Exception, BaseException or everything without re-raising. A wrapper
    with any `raise` outside its nested defs is not swallowing (a retry decorator ends with
    `raise last`), and a `try` that does not call the wrapped callable (best-effort cleanup) is
    ignored."""

    def scan(w: _FuncDef, wrapped: set[str]) -> bool:
        own = list(_own_nodes(w))
        if not any(isinstance(n, ast.Raise) for n in own):
            for n in own:
                if (
                    isinstance(n, (ast.Try, ast.TryStar))
                    and _calls_any(n.body, wrapped)
                    and any(_handler_swallows(h) for h in n.handlers)
                ):
                    return True
        inner = wrapped | _param_names(w)
        return any(scan(d, inner) for d in _direct_defs(w))

    return scan(fn, _param_names(fn))


def _direct_defs(fn: _FuncDef) -> list[_FuncDef]:
    """The defs nested in `fn` that are not inside another nested def."""
    out: list[_FuncDef] = []
    todo: list[ast.AST] = list(fn.body)
    while todo:
        node = todo.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append(node)
        elif not isinstance(node, (ast.Lambda, ast.ClassDef)):
            todo.extend(ast.iter_child_nodes(node))
    return out


# How many helpers deep a test's call chain is followed (`check` -> `_check_fields` -> `_check_id`).
# The stack in `_credit_helper` already stops cycles.
_HELPER_DEPTH = 8
_INLINE_DEPTH = 3  # how deep a helper call is read as inlined (pinned values, subjects, rows)


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
        self.exact: dict[str, str] = {}  # shape -> source of each exact-check subject
        self.loose: dict[str, str] = {}  # shape -> source of the loose check on it
        self.cur_form = ""  # exact form of the assertion being classified
        self.exact_hit: set[str] = set()  # forms of assertions holding an exact check
        self.loose_hit: set[str] = set()  # forms of assertions holding a loose check
        self.pinned = 0  # exact checks against a literal or an all-literal parametrize argument
        self.literal_params: set[str] = set()
        self.call_self: dict[str, int] = {}  # call -> times compared with itself
        self.call_self_line = 0
        self.call_compared: set[str] = set()  # calls that were an operand of a real comparison
        self.cur_line = 0
        self.strong_operands: set[str] = set()  # operands of comparisons
        self.weak_operands: set[str] = (
            set()
        )  # targets of a bare truthiness (or `is not None`) check
        self.first_line = 0
        # (subject shape, expected number) -> (tightest band, subject source) over the exact and
        # tolerant numeric comparisons of the body (SPEC 5.4, exact checks loosened).
        self.numeric: dict[tuple[str, float], tuple[float, str]] = {}

    def _same_at(self, line: int) -> _Same:
        """`_same`, also after inlining the locals assigned once before `line`. An operand that
        has a call once the locals are inlined is never the same."""

        def same(a: ast.AST, b: ast.AST) -> bool:
            if _same(a, b):
                return True
            ia, ib = self.consts.inline(a, 0, line), self.consts.inline(b, 0, line)
            if ast.dump(ia) != ast.dump(ib):
                return False
            return not _has_call(ia)

        return same

    def _opshape(self, node: ast.AST) -> str:
        return digest(ast.dump(_Locals(self.consts.stores).visit(self.consts.inline(node))))

    def _note_pair(self, a: ast.AST, b: ast.AST, selfop: bool) -> None:
        """Record a comparison of two operands that have a call: a call compared with itself
        (when the operator holds for equal operands), or the calls a real comparison used."""
        ia, ib = (self.consts.inline(x, 0, self.cur_line) for x in (a, b))
        if not (_has_call(ia) or _has_call(ib)):
            return
        stores = self.consts.stores
        if ast.dump(ia) == ast.dump(ib):
            if not selfop or _stateful(ia):
                return
            keys = _call_keys(ia, stores)
            for key in keys:
                self.call_self[key] = self.call_self.get(key, 0) + 1
            self.call_self_line = self.call_self_line or self.cur_line
        else:
            self.call_compared |= _call_keys(ia, stores) | _call_keys(ib, stores)

    # -- exact and loose checks (SPEC 5.4) ---------------------------------------------
    def _pinned_side(self, node: ast.expr) -> bool:
        if isinstance(node, ast.Name) and node.id in self.literal_params:
            return True
        if _constructed(node):  # spelled out here, not a local that was built earlier
            return True
        inlined = self.consts.inline(node)
        if isinstance(inlined, ast.Name) and inlined.id in self.literal_params:
            return True
        arg = _approx_arg(inlined)
        if arg is not None:
            return self._pinned_side(arg)
        return _is_pinned(inlined)

    def _pinned_check(self, sides: Sequence[ast.expr]) -> None:
        """Count an exact check one of whose sides is pinned to a literal."""
        if any(self._pinned_side(s) for s in sides):
            self.pinned += 1

    def _exact(self, node: ast.expr, src: str) -> None:
        """`node` is a subject of an exact check; a `type(X)`, `len(X)`, `bool(X)` wrapper also
        makes X the target of a loose one."""
        if self.consts.is_const(node):
            return
        self.exact_hit.add(self.cur_form)
        self.exact.setdefault(self._opshape(node), ast.unparse(self.consts.inline(node)))
        self._unwrap(node, src)

    def _loosen(self, node: ast.expr, src: str) -> None:
        if self.consts.is_const(node):
            return
        self.loose_hit.add(self.cur_form)
        self.loose.setdefault(self._opshape(node), src)
        self._unwrap(node, src)

    def _unwrap(self, node: ast.expr, src: str) -> None:
        while (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in _WRAPPERS
            and len(node.args) == 1
            and not node.keywords
        ):
            node = node.args[0]
            if not self.consts.is_const(node):
                self.loose.setdefault(self._opshape(node), src)

    def _test_checks(self, test: ast.expr, src: str, negated: bool = False) -> None:
        """Classify the condition of an assert (or the argument of assertTrue/assertFalse)."""
        if isinstance(test, ast.BoolOp):
            for value in test.values:
                self._test_checks(value, src, False)
        elif isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
            self._test_checks(test.operand, src, not negated)
        elif isinstance(test, ast.Compare):
            left = test.left
            for op, right in zip(test.ops, test.comparators, strict=True):
                self._note_pair(
                    left, right, not negated and isinstance(op, (ast.Eq, ast.LtE, ast.GtE, ast.Is))
                )
                self._compare(left, op, right, src, negated)
                left = right
        elif isinstance(test, ast.Call) and dotted(test.func) in {"isinstance", "issubclass"}:
            if test.args:
                self._loosen(test.args[0], src)
        else:
            if (
                isinstance(test, ast.Call)
                and not negated
                and dotted(test.func).rsplit(".", 1)[-1] == "isclose"
            ):
                self._isclose(test)
            self._loosen(test, src)

    def _compare(self, left: ast.expr, op: ast.cmpop, right: ast.expr, src: str, neg: bool) -> None:
        if isinstance(op, (*_EXACT_OPS, *_INEXACT_OPS)):
            exact = isinstance(op, _EXACT_OPS) != neg
        elif isinstance(op, _ORDER_OPS) or neg:
            exact = False
        else:  # `in`: neither an exact nor a loose check
            return
        if exact:
            self._pinned_check((left, right))
            if isinstance(op, ast.Eq) and not neg:
                self._numeric_pair(left, right)
        for side in (left, right):
            if exact:
                self._exact(side, src)
            else:
                self._loosen(side, src)

    # -- numeric tolerance bands ---------------------------------------------------------
    def _band(self, node: ast.expr) -> tuple[float, float] | None:
        """(expected, band) of a numeric literal (band 0) or `approx(<literal>, rel=, abs=)`;
        None for anything else, or when a tolerance is not a literal."""
        node = self.consts.inline(node)  # type: ignore[assignment]
        expected = _number(node)
        if expected is not None:
            return expected, 0.0
        if not (
            isinstance(node, ast.Call)
            and dotted(node.func).rsplit(".", 1)[-1] == "approx"
            and len(node.args) == 1
        ):
            return None
        expected = _number(node.args[0])
        if expected is None:
            return None
        tol: dict[str, float] = {}
        for kw in node.keywords:
            if kw.arg not in {"rel", "abs"}:
                continue
            value = _number(self.consts.inline(kw.value))
            if value is None:
                return None
            tol[kw.arg] = value
        rel = tol.get("rel", 0.0 if "abs" in tol else 1e-6)
        return expected, max(rel * abs(expected), tol.get("abs", 1e-12))

    def _note_numeric(self, subject: ast.expr, expected: float, band: float) -> None:
        if self.consts.is_const(subject):
            return
        key = (self._opshape(subject), expected)
        have = self.numeric.get(key)
        if have is None or band < have[0]:
            self.numeric[key] = (band, ast.unparse(self.consts.inline(subject)))

    def _numeric_pair(self, left: ast.expr, right: ast.expr, band: float | None = None) -> None:
        """`left == right` where one side is a numeric literal or approx of one. `band` overrides
        the tolerance for a call that carries its own (assertAlmostEqual, math.isclose)."""
        a, b = self._band(left), self._band(right)
        if (a is None) == (b is None):
            return
        expected, own = a or b or (0.0, 0.0)
        self._note_numeric(
            right if a is not None else left, expected, own if band is None else band
        )

    def _almost_band(self, node: ast.Call) -> float | None:
        """The band an assertAlmostEqual call allows: `delta`, else half of 10**-places."""
        places: float | None = 7.0
        if len(node.args) >= 3:
            places = _number(node.args[2])
        for kw in node.keywords:
            if kw.arg == "delta":
                return _number(kw.value)
            if kw.arg == "places":
                places = _number(kw.value)
        return None if places is None else 0.5 * 10.0 ** (-places)

    def _isclose(self, node: ast.Call) -> None:
        tol: dict[str, float] = {}
        for kw in node.keywords:
            if kw.arg in {"rel_tol", "abs_tol"}:
                value = _number(kw.value)
                if value is None:
                    return
                tol[kw.arg] = value
        if len(node.args) < 2:
            return
        for lit, other in ((node.args[1], node.args[0]), (node.args[0], node.args[1])):
            if (n := _number(self.consts.inline(lit))) is not None:
                band = max(tol.get("rel_tol", 1e-9) * abs(n), tol.get("abs_tol", 0.0))
                self._note_numeric(other, n, band)
                return

    # -- assertions ---------------------------------------------------------------------
    def _counted(self, node: ast.AST) -> None:
        self.asserts += 1
        self.first_line = self.first_line or getattr(node, "lineno", 0)
        self.shapes.append(_shape(node))
        self.cur_form = digest(ast.dump(node))
        self.cur_line = getattr(node, "lineno", 0)
        self.forms.append(self.cur_form)

    def visit_Assert(self, node: ast.Assert) -> None:
        test = node.test
        if _always_true(test, self._same_at(node.lineno)) or self.consts.is_const(
            test
        ):  # vacuous: it can never fail, so it checks nothing
            self.vacuous += 1
            self.vacuous_line = self.vacuous_line or node.lineno
            self.first_line = self.first_line or node.lineno
            return
        self._counted(node.test)
        self._test_checks(test, ast.unparse(test))
        if isinstance(test, ast.Compare):
            weak = (
                len(test.ops) == 1
                and isinstance(test.ops[0], ast.IsNot)
                and isinstance(test.comparators[0], ast.Constant)
                and test.comparators[0].value is None
            )
            if weak:
                self.weak += 1
                self.weak_operands.add(self._opshape(test.left))
            else:
                self.strong += 1
                self.strong_operands.update(
                    self._opshape(o) for o in (test.left, *test.comparators)
                )
            if len(test.ops) == 1 and isinstance(test.ops[0], ast.Eq):
                for side in (test.left, test.comparators[0]):
                    lit = _expected_of(side)
                    if lit is not None:
                        self.expected.append((ast.dump(lit), node.lineno))
        elif isinstance(test, (ast.Name, ast.Attribute, ast.Subscript)):
            self.weak += 1
            self.weak_operands.add(self._opshape(test))
        elif isinstance(test, ast.Call):
            # A bare truthiness check on a call is weak; type checks pin a concrete fact.
            if dotted(test.func) in {"isinstance", "issubclass"}:
                self.strong += 1
            elif dotted(test.func).rsplit(".", 1)[-1] == "isclose" and len(test.args) >= 2:
                self.strong += 1  # a comparison with a tolerance, like `== approx(...)`
                self.strong_operands.update(self._opshape(a) for a in test.args[:2])
            else:
                self.weak += 1
                self.weak_operands.add(self._opshape(test))
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
            if _vacuous_call(tail, node, self._same_at(node.lineno)) or self._const_call(node):
                self.vacuous += 1
                self.vacuous_line = self.vacuous_line or node.lineno
                self.first_line = self.first_line or node.lineno
                return
            self._counted(node)
            self._method_checks(tail, node)
            if tail in _WEAK_METHODS:
                self.weak += 1
                if node.args:
                    self.weak_operands.add(self._opshape(node.args[0]))
            else:
                self.strong += 1
                self.strong_operands.update(self._opshape(a) for a in node.args[:2])
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

    def _method_checks(self, tail: str, node: ast.Call) -> None:
        src = ast.unparse(node)
        args = node.args
        if (tail in _EQUAL_METHODS or tail == "assertIs") and len(args) >= 2:
            self._note_pair(args[0], args[1], True)
        if tail in _EQUAL_METHODS and len(args) >= 2:
            band = self._almost_band(node) if tail in _ALMOST else None
            if band is not None or tail not in _ALMOST:
                self._numeric_pair(args[0], args[1], band)
        if tail in _EXACT_METHODS:
            if tail == "assertIsNone":
                self.pinned += 1  # `is None`: pinned to the constant None
            else:
                self._pinned_check(args[:2])
            for arg in args[: 1 if tail == "assertIsNone" else 2]:
                self._exact(arg, src)
        elif tail in _LOOSE_PAIR_METHODS:
            for arg in args[:2]:
                self._loosen(arg, src)
        elif tail in {"assertTrue", "assertFalse"} and args:
            self._test_checks(args[0], src, negated=tail == "assertFalse")
        elif tail in {"assertIsNotNone", "assertIsInstance"} and args:
            self._loosen(args[0], src)

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
        elif isinstance(func, ast.Attribute):
            if dotted(func.value) in {"self", "cls"}:
                target = func.attr
            elif dotted(func) in self.helpers:  # `helpers.check(...)` on an imported module
                target = dotted(func)
        if target is None or target not in self.helpers or self.depth >= _HELPER_DEPTH:
            return
        if target in self.stack:
            return
        scopes = getattr(self.helpers, "scopes", {})
        sub = _Stats(
            scopes.get(target, self.helpers),
            self.depth + 1,
            (*self.stack, target),
            _Consts(self.helpers[target]),
        )
        inlined = target in getattr(self.helpers, "unique", ()) and self.depth < _INLINE_DEPTH
        if inlined:
            sub.literal_params = self._literal_args(self.helpers[target], node)
        for stmt in self.helpers[target].body:
            sub.visit(stmt)
        if inlined:
            self.pinned += sub.pinned
            for key, text in sub.exact.items():
                self.exact.setdefault(key, text)
            for key, text in sub.loose.items():
                self.loose.setdefault(key, text)
        self.asserts += sub.asserts
        self.strong += sub.strong
        self.weak += sub.weak
        self.broad += sub.broad
        self.specific += sub.specific
        self.shapes.extend(sub.shapes)
        self.forms.extend(sub.forms)
        self.exact_hit |= sub.exact_hit
        self.loose_hit |= sub.loose_hit
        self.vacuous += sub.vacuous
        for key, count in sub.call_self.items():
            self.call_self[key] = self.call_self.get(key, 0) + count
        self.call_self_line = self.call_self_line or sub.call_self_line
        self.call_compared |= sub.call_compared

    def _literal_args(self, helper: _FuncDef, call: ast.Call) -> set[str]:
        """The parameters of `helper` bound, by this call, to a literal or a literal row value."""
        args = helper.args
        names = [a.arg for a in (*args.posonlyargs, *args.args)]
        if any(isinstance(a, ast.Starred) for a in call.args) or any(
            k.arg is None for k in call.keywords
        ):
            return set()
        bound = dict(zip(names, call.args, strict=False))
        bound.update({k.arg: k.value for k in call.keywords if k.arg})
        return {name for name, value in bound.items() if self._pinned_side(value)}

    # -- swallowing ---------------------------------------------------------------------
    def _swallows(self, handler: ast.ExceptHandler) -> bool:
        return _handler_swallows(handler)

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


def _parametrize(fn: _FuncDef) -> tuple[int | None, bool, list[str]]:
    """(cases, literal, case forms): the product of cases over stacked literal parametrize
    decorators, whether every decorator was literal, and the exact form of each case."""
    total: int | None = None
    literal = True
    forms: list[str] = []
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
            forms.extend(digest(ast.dump(e)) for e in values.elts)
        else:
            literal = False
    return (total if literal else None), literal, forms


@dataclass(frozen=True)
class _Fold:
    """A unittest fold: `for <names> in <literal rows>:` around `with self.subTest(...)`, the
    only place the test asserts."""

    loop: ast.For
    names: tuple[str, ...]
    rows: tuple[ast.expr, ...]


def _fold_targets(target: ast.expr) -> tuple[str, ...] | None:
    if isinstance(target, ast.Name):
        return (target.id,)
    if isinstance(target, (ast.Tuple, ast.List)) and all(
        isinstance(e, ast.Name) for e in target.elts
    ):
        names = tuple(e.id for e in target.elts if isinstance(e, ast.Name))
        return names if len(set(names)) == len(names) else None
    return None


def _has_subtest(loop: ast.For) -> bool:
    for stmt in loop.body:
        if isinstance(stmt, (ast.With, ast.AsyncWith)):
            for item in stmt.items:
                ctx = item.context_expr
                if isinstance(ctx, ast.Call) and dotted(ctx.func).rsplit(".", 1)[-1] == "subTest":
                    return True
    return False


def _fold_loop(fn: _FuncDef, helpers: dict[str, _FuncDef]) -> _Fold | None:
    """The `subTest` loop that folds a literal table of rows into `fn`, read like a parametrize
    decorator: a top-level `for` over a literal list or tuple (or a module-level name bound once
    to one) of literal rows, with no `else`, whose body holds a `with self.subTest(...)`, in a test
    with no parametrize decorator, no other loop of this kind, and no assertion outside the loop
    (those would run once, not once per row). The loop variables must not be rebound in the body."""
    if isinstance(fn, ast.AsyncFunctionDef) or param_names(fn):
        return None
    loops = [s for s in fn.body if isinstance(s, ast.For) and _has_subtest(s)]
    if len(loops) != 1 or loops[0].orelse:
        return None
    loop = loops[0]
    names = _fold_targets(loop.target)
    table: ast.expr | None = loop.iter
    if isinstance(table, ast.Name):
        table = getattr(helpers, "tables", {}).get(table.id)
    if names is None or not isinstance(table, (ast.List, ast.Tuple)) or not table.elts:
        return None
    for row in table.elts:
        if isinstance(row, ast.Starred) or not _is_pinned(row):
            return None
        if len(names) > 1 and not (
            isinstance(row, (ast.Tuple, ast.List)) and len(row.elts) == len(names)
        ):
            return None
    targets = {id(n) for n in ast.walk(loop.target)}
    for node in ast.walk(loop):
        if (
            isinstance(node, ast.Name)
            and not isinstance(node.ctx, ast.Load)
            and node.id in names
            and id(node) not in targets
        ):
            return None
    outside = _Stats(helpers, consts=_Consts(fn))
    for stmt in fn.body:
        if stmt is not loop:
            outside.visit(stmt)
    if outside.asserts or outside.vacuous or outside.swallowed:
        return None
    return _Fold(loop, names, tuple(table.elts))


def _marked(fn: _FuncDef) -> list[tuple[str, tuple[str, ...]]]:
    """The exact form of each literal row of a parametrize decorator that carries a skip or xfail
    mark (`pytest.param(..., marks=pytest.mark.skip)`), with the kinds of its marks: such a row
    never runs, or never counts."""
    out: list[tuple[str, tuple[str, ...]]] = []
    for dec in fn.decorator_list:
        if not (isinstance(dec, ast.Call) and dotted(dec.func).endswith("mark.parametrize")):
            continue
        values: ast.AST | None = dec.args[1] if len(dec.args) > 1 else None
        for kw in dec.keywords:
            if kw.arg == "argvalues":
                values = kw.value
        if not isinstance(values, (ast.List, ast.Tuple)):
            continue
        for e in values.elts:
            if isinstance(e, ast.Call) and dotted(e.func).rsplit(".", 1)[-1] == "param":
                marks = next((k.value for k in e.keywords if k.arg == "marks"), None)
                kinds = re.findall(r"\b(skipif|skip|xfail)\b", ast.unparse(marks)) if marks else []
                if kinds:
                    out.append((digest(ast.dump(e)), tuple(sorted(set(kinds)))))
    return out


def _mark_kinds(fn: _FuncDef) -> set[str]:
    """The skip, skipif and xfail marks a test carries as decorators."""
    kinds = set()
    for dec in fn.decorator_list:
        name = dotted(dec.func if isinstance(dec, ast.Call) else dec)
        for suffix in _MARK_SUFFIXES:
            if name.endswith(suffix):
                kinds.add(suffix.rsplit(".", 1)[-1])
    return kinds


_VOCAB_ROOTS = frozenset(
    {"pytest", "unittest", "mock", "mocker", "monkeypatch", "capsys", "capfd", "caplog", "request"}
)
_VOCAB_NAMES = frozenset({"patch", "MagicMock", "Mock", "AsyncMock", "PropertyMock", "call", "ANY"})
_VOCAB_TAILS = frozenset({"fail", "skipTest", "subTest", "approx", "raises", "warns", "param"})


def _subjects(fn: _FuncDef) -> set[str]:
    """Dotted names of the functions and methods the test calls, minus builtins, test-framework
    vocabulary and names bound inside the test body (locals and nested definitions)."""
    bound: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
            bound.add(node.id)
        elif (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and node is not fn
        ):
            bound.add(node.name)
    out: set[str] = set()
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        while isinstance(func, ast.Attribute):
            func = func.value
        if not isinstance(func, ast.Name):
            continue  # a call on an expression result or literal: no stable name
        name = dotted(node.func)
        root, _, rest = name.partition(".")
        tail = name.rsplit(".", 1)[-1]
        if (
            root in bound
            or root in _VOCAB_ROOTS
            or name in _VOCAB_NAMES
            or tail in _VOCAB_TAILS
            or tail.startswith("assert")
            or (not rest and hasattr(builtins, root))
            or (root in {"self", "cls"} and tail.startswith(("assert", "fail")))
        ):
            continue
        out.add(name)
    return out


def _helper_subjects(fn: _FuncDef, helpers: Mapping[str, _FuncDef]) -> set[str]:
    """The subjects of the file's own plain helpers `fn` calls, as if their bodies were inlined
    (`_INLINE_DEPTH` deep): a test that asserts through `check(a, b)` still calls what `check`
    calls. The helper's own name stays a subject of the caller."""
    unique: AbstractSet[str] = getattr(helpers, "unique", set())
    out: set[str] = set()
    seen: set[str] = set()
    level: list[_FuncDef] = [fn]
    for _ in range(_INLINE_DEPTH):
        nxt: list[_FuncDef] = []
        for func in level:
            for node in ast.walk(func):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id in unique
                    and node.func.id not in seen
                ):
                    seen.add(node.func.id)
                    nxt.append(helpers[node.func.id])
        for func in nxt:
            out |= _subjects(func)
        level = nxt
    return out


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
    stats.literal_params = _literal_params(fn)
    fold = _fold_loop(fn, helpers)
    if fold is not None:
        stats.literal_params |= set(fold.names)
    for stmt in live.body:
        stats.visit(stmt)
    swallowers: set[str] = getattr(helpers, "swallowers", set())
    for dec in fn.decorator_list:
        # A decorator that swallows assertion failures wraps every check of the test.
        if dotted(dec.func if isinstance(dec, ast.Call) else dec) in swallowers and stats.asserts:
            stats.swallowed += stats.asserts
            stats.swallow_line = stats.swallow_line or dec.lineno
            break
    cases, literal, case_forms = _parametrize(fn)
    if fold is not None:
        cases, literal, case_forms = len(fold.rows), True, [digest(ast.dump(r)) for r in fold.rows]
    gap, big = row_gap(fn, helpers, (set(fold.names), fold.loop) if fold else None)
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
        param_cases=cases,
        param_literal=literal,
        param_forms=case_forms,
        row_gap=gap,
        row_big=big,
        param_marked=_marked(fn),
        mark_kinds=_mark_kinds(fn),
        subjects=_subjects(live) | _helper_subjects(live, helpers),
        swallowed=stats.swallowed,
        swallow_line=stats.swallow_line,
        expected=stats.expected,
        struct_hash=_struct_hash(fn),
        shapes=stats.shapes,
        assertion_forms=stats.forms,
        loose_forms=sorted(stats.loose_hit - stats.exact_hit),
        vacuous=stats.vacuous,
        vacuous_line=stats.vacuous_line,
        call_self=stats.call_self,
        call_self_line=stats.call_self_line,
        call_compared=stats.call_compared,
        unreachable=lost.asserts,
        unreachable_line=lost.first_line,
        exact_subjects=stats.exact,
        loose_targets=stats.loose,
        pinned=stats.pinned,
        numeric_bands=stats.numeric,
        strong_operands=stats.strong_operands,
        weak_operands=stats.weak_operands,
        classes=classes,
        testcase=testcase,
        disabled=disabled,
        node=fn,
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


def _reads(node: ast.AST, name: str) -> bool:
    return any(isinstance(n, ast.Name) and n.id == name for n in ast.walk(node))


def _last_bindings(body: Sequence[ast.stmt]) -> dict[str, ast.AST | None]:
    """Python keeps only the last binding of a name in a module or class body. Maps each name to
    the definition that is its last binding, or None when a later `del`, assignment or import
    replaced it. An assignment that reads the name (`test_x = decorate(test_x)`) wraps the
    definition rather than replacing it, so it is not a rebinding. `del TestBase` after the
    subclasses were defined is an idiom that keeps pytest from collecting the base class; the
    class is not removed, so a `del` of a class another class of the body subclasses is skipped."""
    last: dict[str, ast.AST | None] = {}
    subclassed = {
        dotted(b).rsplit(".", 1)[-1]
        for node in body
        if isinstance(node, ast.ClassDef)
        for b in node.bases
    }
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            last[node.name] = node
        elif isinstance(node, ast.Delete):
            for target in node.targets:
                if isinstance(target, ast.Name) and not (
                    target.id in subclassed and isinstance(last.get(target.id), ast.ClassDef)
                ):
                    last[target.id] = None
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name != "*":
                    last[alias.asname or alias.name.split(".")[0]] = None
        elif isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                names = [target] if isinstance(target, ast.Name) else []
                if isinstance(target, (ast.Tuple, ast.List)):
                    names = [e for e in target.elts if isinstance(e, ast.Name)]
                for name in names:
                    if not _reads(node.value, name.id):
                        last[name.id] = None
    return last


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
    last = _last_bindings(body)
    for node in body:
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and last.get(node.name) is not node
        ):
            continue  # shadowed by a later definition, or deleted or rebound: not collected
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
                    value = node.value
                    text = target.id
                    if value is not None:  # the paths, for `collect.live_ignores`
                        text = f"{target.id} = {ast.unparse(value)[:200]}"
                    out.append(Marker(scope, target.id, text, node.lineno, "tests.skipped"))
    return out


class Sources:
    """Reads Python modules at one revision, so a test's imported helpers are analysed as they
    were at the same revision as the test (base text for the base, the candidate tree for the
    candidate). `is_test` says whether a path is inside the test paths: only those resolve."""

    def __init__(
        self,
        read: Callable[[str], str | None],
        is_test: Callable[[str], bool],
        listing: Callable[[], set[str]] | None = None,
    ) -> None:
        self.read = read
        self.is_test = is_test
        self._listing = listing
        self._files: set[str] | None = None
        self._parsed: dict[str, ast.Module | None] = {}

    def module(self, path: str) -> ast.Module | None:
        if path not in self._parsed:
            if self._listing is not None and self._files is None:
                self._files = self._listing()  # one `git ls-tree` instead of a read per guess
            text = None if self._files is not None and path not in self._files else self.read(path)
            tree: ast.Module | None = None
            if text is not None and (
                self.is_test(path) or _SUPPORT_TEXT.search(text) or _support_path(path)
            ):
                try:
                    parsed = ast.parse(text)
                except (SyntaxError, ValueError, RecursionError):
                    parsed = None
                if parsed is not None and (self.is_test(path) or is_test_support(path, parsed)):
                    tree = parsed
            self._parsed[path] = tree
        return self._parsed[path]


_SUPPORT_SEGMENT = re.compile(r"(?:testing|test_utils|testutils|pytest_plugins?)")
_SUPPORT_IMPORTS = frozenset({"pytest", "unittest", "hypothesis"})
_SUPPORT_TEXT = re.compile(r"\b(?:pytest|unittest|hypothesis)\b")


def _support_path(path: str) -> bool:
    return any(_SUPPORT_SEGMENT.fullmatch(seg.removesuffix(".py")) for seg in path.split("/"))


def is_test_support(path: str, tree: ast.Module) -> bool:
    """A module outside the test paths that exists for tests (SPEC 5.4): it imports pytest,
    unittest or hypothesis, or a segment of its path is `testing`, `test_utils`, `testutils` or
    `pytest_plugin(s)`. Ordinary application modules are not: a defensive `assert` in production
    code is not a test check."""
    if _support_path(path):
        return True
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(a.name.split(".")[0] in _SUPPORT_IMPORTS for a in node.names):
                return True
        elif (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and node.module
            and node.module.split(".")[0] in _SUPPORT_IMPORTS
        ):
            return True
    return False


def is_test_support_text(path: str, text: str | None) -> bool:
    """`is_test_support` for source text, parsing only a module that could qualify."""
    if text is None or not (_support_path(path) or _SUPPORT_TEXT.search(text)):
        return False
    try:
        return is_test_support(path, ast.parse(text))
    except (SyntaxError, ValueError, RecursionError):
        return False


def _roots(path: str) -> list[str]:
    """Directories an absolute import in the test file may resolve against: its own directory and
    every parent up to the repository root, then the conventional `tests` and `test` roots."""
    out: list[str] = []
    here = posixpath.dirname(path)
    while True:
        out.append(here)
        if not here:
            break
        here = posixpath.dirname(here)
    out.extend(r for r in ("tests", "test") if r not in out)
    return out


def _resolve_module(
    path: str, module: str, level: int, sources: Sources
) -> tuple[str, ast.Module] | None:
    """The test-path module `module` (dotted, `level` leading dots) names, seen from `path`."""
    rel = module.replace(".", "/") if module else ""
    if level:
        base = posixpath.dirname(path)
        for _ in range(level - 1):
            base = posixpath.dirname(base)
        roots = [base]
    else:
        roots = _roots(path)
    for root in roots:
        stem = posixpath.join(root, rel) if rel else root
        names = (f"{stem}.py", posixpath.join(stem, "__init__.py")) if rel else ()
        for candidate in names or (posixpath.join(stem, "__init__.py"),):
            tree = sources.module(candidate)
            if tree is not None:
                return candidate, tree
    return None


def _top_functions(tree: ast.Module) -> dict[str, _FuncDef]:
    """The module's functions by name; the last definition of a name is the one in force."""
    return {
        n.name: n
        for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and not n.name.startswith("test")
    }


def _import_helpers(path: str, tree: ast.Module, sources: Sources, helpers: _Helpers) -> set[str]:
    """Add the functions of the test-path modules `tree` imports to `helpers`, under the name the
    test calls them by (`check`, `h.check`, `helpers.check`). Returns the modules' paths."""
    used: set[str] = set()

    def add_module(local: str, found: tuple[str, ast.Module]) -> None:
        funcs = _top_functions(found[1])
        used.add(found[0])
        for name, fn in funcs.items():
            key = f"{local}.{name}"
            helpers.setdefault(key, fn)
            helpers.scopes.setdefault(key, funcs)

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            found = _resolve_module(path, module, node.level, sources)
            for alias in node.names:
                if alias.name == "*":
                    continue
                local = alias.asname or alias.name
                if found is not None:
                    funcs = _top_functions(found[1])
                    if alias.name in funcs:
                        used.add(found[0])
                        helpers.setdefault(local, funcs[alias.name])
                        helpers.scopes.setdefault(local, funcs)
                        continue
                sub = _resolve_module(
                    path, f"{module}.{alias.name}" if module else alias.name, node.level, sources
                )
                if sub is not None:  # `from tests import helpers`
                    add_module(local, sub)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                found = _resolve_module(path, alias.name, 0, sources)
                if found is not None:
                    add_module(alias.asname or alias.name, found)
    return used


class _Returned(ast.NodeVisitor):
    """The values a function returns or yields, not looking into nested functions."""

    def __init__(self) -> None:
        self.values: list[ast.expr] = []

    def visit_Return(self, node: ast.Return) -> None:
        if node.value is not None:
            self.values.append(node.value)

    def visit_Yield(self, node: ast.Yield) -> None:
        if node.value is not None:
            self.values.append(node.value)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        return

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        return

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        return


def _fixture_decorator(fn: _FuncDef) -> ast.expr | None:
    for dec in fn.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        if dotted(target).rsplit(".", 1)[-1] == "fixture":
            return dec
    return None


def _fixtures(path: str, body: Sequence[ast.stmt], scope: str, out: dict[str, Fixture]) -> None:
    """The `@pytest.fixture` functions of a module or class body, and of the classes in it."""
    last = _last_bindings(body)
    for node in body:
        if isinstance(node, ast.ClassDef):
            _fixtures(path, node.body, f"{scope}{node.name}::", out)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            dec = _fixture_decorator(node)
            if dec is None or last.get(node.name) is not node:
                continue
            autouse = isinstance(dec, ast.Call) and any(
                k.arg == "autouse" and isinstance(k.value, ast.Constant) and k.value.value is True
                for k in dec.keywords
            )
            returned = _Returned()
            for stmt in node.body:
                returned.visit(stmt)
            out[f"{scope}{node.name}"] = Fixture(
                path,
                f"{scope}{node.name}",
                node.lineno,
                autouse,
                [ast.dump(v) for v in returned.values if _is_literal(v)],
                _body_hash(node),
                scope,
                node,
            )


def analyse_python(path: str, text: str, sources: Sources | None = None) -> FileAnalysis | None:
    """The file's tests and markers, or None when it does not parse or nests too deeply to walk
    (callers fall back to the line engine). With `sources`, functions imported from modules
    inside the test paths are credited like the file's own helpers."""
    try:
        return _analyse_python(path, text, sources)
    except RecursionError:
        return None


def _bound_once(tree: ast.Module) -> set[str]:
    """The module-level names bound exactly once, by a plain `def` or assignment at the top of the
    module: not rebound in an `if` or `try`, by a `del`, an import, a loop, `global` or `:=`."""
    count: dict[str, int] = {}
    stack: list[ast.AST] = list(tree.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            count[node.name] = count.get(node.name, 0) + 1
            for sub in ast.walk(node):
                if isinstance(sub, (ast.Global, ast.Nonlocal)):
                    for name in sub.names:
                        count[name] = count.get(name, 0) + 2
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                name = alias.asname or alias.name.split(".")[0]
                count[name] = count.get(name, 0) + 1
        elif isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
            count[node.id] = count.get(node.id, 0) + 1
        elif isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)) and node.name:
            count[node.name] = count.get(node.name, 0) + 1
        stack.extend(ast.iter_child_nodes(node))
    return {name for name, n in count.items() if n == 1}


def _plain_helper(fn: _FuncDef) -> bool:
    """A function whose call runs its body: not async, not a generator, not decorated."""
    if isinstance(fn, ast.AsyncFunctionDef) or fn.decorator_list:
        return False
    stack: list[ast.AST] = list(fn.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.Yield, ast.YieldFrom)):
            return False
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            stack.extend(ast.iter_child_nodes(node))
    return True


def _module_tables(helpers: _Helpers, tree: ast.Module) -> None:
    """Fill `helpers.unique` and `helpers.tables` from the module's top level."""
    once = _bound_once(tree)
    handled: set[str] = set()  # names the module touches in a way that could change a table
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)) or (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and not isinstance(node.ctx, ast.Load)
        ):
            handled.add(node.value.id)
        elif isinstance(node, ast.Call):
            handled.update(a.id for a in node.args if isinstance(a, ast.Name))
            handled.update(
                a.value.id
                for a in node.args
                if isinstance(a, ast.Starred) and isinstance(a.value, ast.Name)
            )
            handled.update(k.value.id for k in node.keywords if isinstance(k.value, ast.Name))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            if node.name in once and not node.name.startswith("test") and _plain_helper(node):
                helpers.unique.add(node.name)
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if (
                isinstance(target, ast.Name)
                and target.id in once - handled
                and isinstance(node.value, (ast.List, ast.Tuple))
            ):
                helpers.tables[target.id] = node.value
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id in once - handled
            and isinstance(node.value, (ast.List, ast.Tuple))
        ):
            helpers.tables[node.target.id] = node.value


def _module_helpers(
    path: str, tree: ast.Module, sources: Sources | None
) -> tuple[_Helpers, set[str]]:
    """The functions a test of this module may call and get credit for (its own top-level
    functions and methods, and those of the test-path modules it imports), and the paths of the
    imported modules."""
    helpers = _Helpers()
    helpers.update(_top_functions(tree))
    for cls in tree.body:
        if isinstance(cls, ast.ClassDef):
            for n in cls.body:
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and not n.name.startswith(
                    "test"
                ):
                    helpers.setdefault(n.name, n)
    imports: set[str] = set()
    if sources is not None:
        imports = _import_helpers(path, tree, sources, helpers)
    helpers.swallowers = {k for k, fn in helpers.items() if _fn_swallows(fn)}
    _module_tables(helpers, tree)
    return helpers, imports


def _analyse_python(path: str, text: str, sources: Sources | None) -> FileAnalysis | None:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return None
    helpers, imports = _module_helpers(path, tree, sources)
    analysis = FileAnalysis(path, "python", tree=tree)
    analysis.imports = imports
    _fixtures(path, tree.body, "", analysis.fixtures)
    _collect_units(
        path, tree.body, "", helpers, analysis.units, disabled=_test_disabled(tree.body) is not None
    )
    visitor = _Markers(path)
    visitor.visit(tree)
    analysis.markers = visitor.found
    if posixpath.basename(path) == "conftest.py":
        analysis.markers.extend(_conftest_markers(path, tree))
    return analysis


def call_self_replaced(base: _CallCompares, cand: _CallCompares) -> bool:
    """A call compared with itself is vacuous only where it replaced a real comparison: the
    candidate has more self-comparisons of a call than the base, the base compared that call
    with something else, and the candidate no longer does (SPEC 5.4)."""
    return any(
        count > base.call_self.get(key, 0)
        and key in base.call_compared
        and key not in cand.call_compared
        for key, count in cand.call_self.items()
    )


class _CallCompares(Protocol):
    @property
    def call_self(self) -> Mapping[str, int]: ...

    @property
    def call_compared(self) -> AbstractSet[str]: ...


@dataclass(frozen=True)
class HelperEffect:
    """What one helper function gives the tests that call it (SPEC 5.4)."""

    asserts: int
    strong: int
    weak: int
    broad: int
    vacuous: int
    swallowed: int
    swallows: bool  # used as a decorator, it swallows assertion failures
    exact: frozenset[str]
    call_self: Mapping[str, int]
    call_compared: frozenset[str]


def helper_effects(
    path: str, tree: ast.Module, sources: Sources | None
) -> dict[str, HelperEffect] | None:
    """The effect of each top-level function of a helper module or conftest, or None when the
    module nests too deeply to walk."""
    try:
        helpers, _ = _module_helpers(path, tree, sources)
        out: dict[str, HelperEffect] = {}
        for name, fn in _top_functions(tree).items():
            stats = _Stats(helpers, 1, (name,), _Consts(fn))
            for stmt in fn.body:
                stats.visit(stmt)
            out[name] = HelperEffect(
                stats.asserts,
                stats.strong,
                stats.weak,
                stats.broad,
                stats.vacuous,
                stats.swallowed,
                name in helpers.swallowers,
                frozenset(stats.exact),
                stats.call_self,
                frozenset(stats.call_compared),
            )
        return out
    except RecursionError:
        return None


def lost_helpers(
    base: dict[str, HelperEffect] | None, cand: dict[str, HelperEffect] | None
) -> set[str]:
    """Names of the helpers whose credited checks dropped or weakened, that now swallow, or that
    are gone: the only ones whose callers need another look. Everything in `base` when the
    candidate module cannot be read; nothing when the base could not."""
    if base is None:
        return set()
    if cand is None:
        return set(base)
    lost: set[str] = set()
    for name, b in base.items():
        c = cand.get(name)
        if c is None or (
            c.asserts < b.asserts
            or c.strong < b.strong
            or c.weak > b.weak
            or c.broad > b.broad
            or c.vacuous > b.vacuous
            or call_self_replaced(b, c)
            or c.swallowed > b.swallowed
            or (c.swallows and not b.swallows)
            or not b.exact <= c.exact
        ):
            lost.add(name)
    return lost


# Display names for messages.
def marker_display(kind: str) -> str:
    if kind.startswith("mark."):
        return f"@pytest.{kind}"
    return kind
