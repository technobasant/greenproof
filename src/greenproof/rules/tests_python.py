"""Python test analysis on the stdlib `ast` (SPEC 5.3 and 5.4)."""

from __future__ import annotations

import ast
import builtins
import copy
import posixpath
import re
from collections.abc import Callable, Sequence

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
    """The same expression on both sides, so comparing it with itself checks nothing. Calls count
    as the same call written twice (`f(1) == f(1)`), except calls that are stateful by nature."""
    return ast.dump(a) == ast.dump(b) and not _stateful(a)


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


def _is_pinned(node: ast.AST) -> bool:
    """A literal an exact check can pin a value to: a constant, a negated number, a display of
    literals, or `approx(<literal>)`."""
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
    if isinstance(node, ast.Call) and dotted(node.func).endswith("approx") and node.args:
        return _is_pinned(node.args[0])
    return False


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
        return _is_pinned(case)
    if isinstance(case, (ast.Tuple, ast.List)) and len(case.elts) == width:
        return _is_pinned(case.elts[index])
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
        self.exact: dict[str, str] = {}  # shape -> source of each exact-check subject
        self.loose: dict[str, str] = {}  # shape -> source of the loose check on it
        self.cur_form = ""  # exact form of the assertion being classified
        self.exact_hit: set[str] = set()  # forms of assertions holding an exact check
        self.loose_hit: set[str] = set()  # forms of assertions holding a loose check
        self.pinned = 0  # exact checks against a literal or an all-literal parametrize argument
        self.literal_params: set[str] = set()
        self.strong_operands: set[str] = set()  # operands of comparisons
        self.weak_operands: set[str] = (
            set()
        )  # targets of a bare truthiness (or `is not None`) check
        self.first_line = 0
        # (subject shape, expected number) -> (tightest band, subject source) over the exact and
        # tolerant numeric comparisons of the body (SPEC 5.4, exact checks loosened).
        self.numeric: dict[tuple[str, float], tuple[float, str]] = {}

    def _same_at(self, line: int) -> _Same:
        """`_same`, also after inlining the locals assigned once before `line`. An expression with
        calls is the same only when one side is the other's call written out again (a local
        assigned from `f(x)` against `f(x)`), not two locals each assigned from a call."""

        def same(a: ast.AST, b: ast.AST) -> bool:
            if _same(a, b):
                return True
            ia, ib = self.consts.inline(a, 0, line), self.consts.inline(b, 0, line)
            if ast.dump(ia) != ast.dump(ib):
                return False
            if not (_has_call(ia)):
                return True
            return (ast.dump(ia) == ast.dump(a)) != (ast.dump(ib) == ast.dump(b))

        return same

    def _opshape(self, node: ast.AST) -> str:
        return digest(ast.dump(_Locals(self.consts.stores).visit(self.consts.inline(node))))

    # -- exact and loose checks (SPEC 5.4) ---------------------------------------------
    def _pinned_side(self, node: ast.expr) -> bool:
        if isinstance(node, ast.Name) and node.id in self.literal_params:
            return True
        return _is_pinned(self.consts.inline(node))

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
        self.exact_hit |= sub.exact_hit
        self.loose_hit |= sub.loose_hit
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
    for stmt in live.body:
        stats.visit(stmt)
    cases, literal, case_forms = _parametrize(fn)
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
        subjects=_subjects(live),
        swallowed=stats.swallowed,
        swallow_line=stats.swallow_line,
        expected=stats.expected,
        struct_hash=_struct_hash(fn),
        shapes=stats.shapes,
        assertion_forms=stats.forms,
        loose_forms=sorted(stats.loose_hit - stats.exact_hit),
        vacuous=stats.vacuous,
        vacuous_line=stats.vacuous_line,
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
    analysis = FileAnalysis(path, "python", tree=tree)
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
