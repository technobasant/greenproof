"""Whether a parametrized Python test asserts a row value on every path: the Python side of the
table-fold rule (tests_flow.py reads JS, Go and Rust). A fold into one parametrized test earns only
a warning when no row leaves early (a `return`, `pytest.skip`) and the row values reach an assertion
on every path; the first path that does not is named in the finding.

Linear in the body: each statement is visited once, and a `with pytest.raises` block is read as the
one assertion it is."""

from __future__ import annotations

import ast
import re
from collections.abc import Mapping, Sequence

from greenproof.rules.tests_flow import DEAD, MAX_DEPTH, PathState, join_states

MAX_NODES = 20_000
MAX_HELPER_DEPTH = 3  # how many helper calls deep a row value is followed into
_MARKER = re.compile(r"request\s*\.\s*node|PYTEST_CURRENT_TEST|\bnodeid\b|callspec")
_FAILS = frozenset({"fail"})
_SKIPS = frozenset({"skip", "skipTest", "xfail"})
_RAISES = frozenset({"raises", "warns", "assertRaises", "assertWarns", "assertRaisesRegex"})
_WEAK_CALLS = frozenset(
    {"assertTrue", "assertFalse", "assertIsNone", "assertIsNotNone", "assertIsInstance", "assertIs"}
)
_HELPER = re.compile(r"assert|check|verify|expect|compare", re.IGNORECASE)


class _TooBig(Exception):
    pass


def param_names(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """The argument names the parametrize decorators of `fn` bind."""
    out: set[str] = set()
    for dec in fn.decorator_list:
        if not (isinstance(dec, ast.Call) and _tail(dec.func) == "parametrize") or not dec.args:
            continue
        first = dec.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            out.update(n.strip() for n in first.value.split(",") if n.strip())
        elif isinstance(first, (ast.List, ast.Tuple)):
            out.update(
                e.value
                for e in first.elts
                if isinstance(e, ast.Constant) and isinstance(e.value, str)
            )
    return out


def _tail(node: ast.AST) -> str:
    if isinstance(node, ast.Attribute):
        return node.attr
    return node.id if isinstance(node, ast.Name) else ""


def _has(node: ast.AST, names: set[str]) -> bool:
    return any(isinstance(n, ast.Name) and n.id in names for n in ast.walk(node))


def _flag(test: ast.expr) -> tuple[bool, bool]:
    """(a bare name or attribute, negated)"""
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        return _flag(test.operand)[0], True
    return isinstance(test, (ast.Name, ast.Attribute)), False


def _compares(test: ast.expr) -> bool:
    """A comparison with something other than None."""
    return any(
        isinstance(n, ast.Compare)
        and not all(isinstance(c, ast.Constant) and c.value is None for c in n.comparators)
        for n in ast.walk(test)
    )


def _swallowing(handler: ast.ExceptHandler) -> bool:
    """The handler catches assertion failures and neither raises, fails nor asserts: the swallow
    test the `tests.weakened` rule uses (tests_python), less a handler that checks something."""
    from greenproof.rules.tests_python import _handler_swallows

    if not _handler_swallows(handler):
        return False
    for stmt in handler.body:
        for n in ast.walk(stmt):
            if isinstance(n, (ast.Assert, ast.With)):
                return False
            if isinstance(n, ast.Call) and (_tail(n.func) in _FAILS or _tail(n.func) in _RAISES):
                return False
    return True


class _Walk:
    def __init__(
        self,
        fn: ast.AST,
        names: set[str],
        helpers: Mapping[str, ast.FunctionDef | ast.AsyncFunctionDef] | None = None,
        stack: tuple[str, ...] = (),
    ) -> None:
        self.helpers = helpers
        self.unique: set[str] = set(getattr(helpers, "unique", ())) if helpers else set()
        self.stack = stack
        # In a helper: a `return` ends the call, not the test; its state is kept here.
        self.sink: list[PathState] | None = None
        self.fold = False  # walking the body of a fold loop: `continue` and `break` leave the row
        self.inner = 0  # loops entered inside it
        self.names = set(names)
        for _ in range(2):
            for n in ast.walk(fn):
                if (
                    isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign))
                    and n.value
                    and (_has(n.value, self.names) or _MARKER.search(ast.unparse(n.value)))
                ):
                    targets = n.targets if isinstance(n, ast.Assign) else [n.target]
                    for t in targets:
                        self.names.update(x.id for x in ast.walk(t) if isinstance(x, ast.Name))
        self.strong = True
        self.bad = ""
        self.uniform = 0
        self.count = 0

    # -- assertions
    def classify(self, node: ast.AST) -> tuple[bool, str] | None:
        """(uses a row value, weak matcher or "") when `node` is an assertion, else None."""
        if isinstance(node, ast.Assert):
            test = node.test
            if isinstance(test, ast.Constant):
                return None
            weak = "" if _compares(test) else ast.unparse(test)[:60]
            return _has(test, self.names), weak
        call = node.value if isinstance(node, ast.Expr) else None
        if isinstance(call, ast.Call):
            tail = _tail(call.func)
            if tail in _SKIPS or tail in _FAILS:
                return None
            if _HELPER.search(tail):
                uses = _has(call, self.names)
                weak = (
                    tail if tail in _WEAK_CALLS and not any(_compares(a) for a in call.args) else ""
                )
                return uses, weak
        if isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                c = item.context_expr
                if isinstance(c, ast.Call) and _tail(c.func) in _RAISES:
                    return _has(node, self.names), ""
        return None

    def strong_exists(self, fn: ast.AST) -> bool:
        for n in ast.walk(fn):
            got = self.classify(n)
            if got and got[0] and not got[1]:
                return True
        return False

    def assertion(self, st: PathState, node: ast.AST, rescue: bool) -> PathState:
        got = self.classify(node)
        if not got or not got[0]:
            return st
        if got[1] and self.strong and not rescue:
            return st._replace(weak=got[1]) if st.u else st
        return PathState(False, st.a or st.u, "", "")

    # -- findings
    def report(self, st: PathState, word: str, guards: list[tuple[str, bool]]) -> None:
        if self.bad or not st.u or self.uniform:
            return
        if st.weak:
            self.bad = f"a path returns after `{st.weak}` without asserting a row value"
        elif guards:
            cond, marker = guards[0]
            self.bad = (
                f"rows matching `{cond}` are skipped"
                if marker
                else f"the body leaves with `{word}` when `{cond}` before asserting"
            )
        else:
            self.bad = f"the body leaves unconditionally with `{word}` before asserting"

    def finish(self, end: PathState) -> None:
        if not self.bad and end.u and (end.why or end.weak):
            self.bad = end.why or f"a path ends after `{end.weak}` without asserting a row value"

    # -- walking
    def body(
        self,
        stmts: Sequence[ast.stmt],
        st: PathState,
        guards: list[tuple[str, bool]],
        rescue: bool,
        depth: int,
    ) -> PathState:
        for stmt in stmts:
            if self.bad:
                return st
            self.count += 1
            if self.count > MAX_NODES or depth > MAX_DEPTH:
                raise _TooBig
            st = self.stmt(stmt, st, guards, rescue, depth)
        return st

    def stmt(
        self,
        node: ast.stmt,
        st: PathState,
        guards: list[tuple[str, bool]],
        rescue: bool,
        depth: int,
    ) -> PathState:
        if isinstance(node, ast.Return):
            if self.sink is not None:
                self.sink.append(st)
                return DEAD
            self.report(st, "return", guards)
            return DEAD
        if isinstance(node, (ast.Continue, ast.Break)) and self.fold and not self.inner:
            self.report(st, "continue" if isinstance(node, ast.Continue) else "break", guards)
            return DEAD
        if isinstance(node, ast.Raise):
            return DEAD
        if isinstance(node, ast.Assert):
            if isinstance(node.test, ast.Constant) and not node.test.value:
                return DEAD
            return self.assert_cond(node, st, rescue)
        if isinstance(node, ast.Expr):
            v = node.value
            if isinstance(v, ast.Call):
                tail = _tail(v.func)
                if tail in _FAILS:
                    return DEAD
                if tail in _SKIPS:
                    self.report(st, ast.unparse(v.func), guards)
                    return DEAD
                inlined = self.helper_call(v, st, guards)
                if inlined is not None:
                    return inlined
            if isinstance(v, ast.BoolOp) and not isinstance(v.values[0], ast.Call):
                after = st
                for rest in v.values[1:]:
                    after = self.assertion(after, ast.Expr(rest), rescue)
                if after != st:
                    left = v.values[0]
                    if not (_has(left, self.names) or _MARKER.search(ast.unparse(left))):
                        return after
                    cond = ast.unparse(left)
                    return join_states(after, st, True, cond, "if")
            return self.assertion(st, node, rescue)
        if isinstance(node, ast.If):
            return self.if_(node, st, guards, rescue, depth)
        if isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
            self.inner += 1
            end = self.body(node.body, st, guards, rescue, depth + 1)
            self.inner -= 1
            return st if not (end.u or end.a) else end
        if isinstance(node, (ast.Try, ast.TryStar)):
            return self.try_(node, st, guards, rescue, depth)
        if isinstance(node, (ast.With, ast.AsyncWith)):
            if self.classify(node) is not None:
                return self.assertion(st, node, rescue)
            return self.body(node.body, st, guards, rescue, depth + 1)
        if isinstance(node, ast.Match):
            return self.match(node, st, guards, rescue, depth)
        return st

    def helper_call(
        self, call: ast.Call, st: PathState, guards: list[tuple[str, bool]]
    ) -> PathState | None:
        """Read a call of one of the file's own plain helpers, defined once at module level, as if
        its body stood here: the parameters that receive a row value carry it. None when the call
        is not such a call, or no argument uses a row value (the name test then decides)."""
        func = call.func
        if (
            self.helpers is None
            or not isinstance(func, ast.Name)
            or func.id not in self.unique
            or func.id in self.stack
            or len(self.stack) >= MAX_HELPER_DEPTH
            or any(isinstance(a, ast.Starred) for a in call.args)
            or any(k.arg is None for k in call.keywords)
        ):
            return None
        fn = self.helpers[func.id]
        a = fn.args
        positional = [p.arg for p in (*a.posonlyargs, *a.args)]
        known = {*positional, *(p.arg for p in a.kwonlyargs)}
        bound = {n for n, arg in zip(positional, call.args, strict=False) if _has(arg, self.names)}
        bound |= {k.arg for k in call.keywords if k.arg in known and _has(k.value, self.names)}
        if not bound:
            return None
        sub = _Walk(fn, bound, self.helpers, (*self.stack, func.id))
        sub.sink = []
        sub.count = self.count
        sub.uniform = self.uniform
        sub.strong = sub.strong_exists(fn)
        end = sub.body(fn.body, st, guards, False, 0)
        self.count = sub.count
        if sub.bad and not self.bad:
            self.bad = sub.bad
        for early in sub.sink:
            end = join_states(end, early, True, func.id, "if")
        if any(early.u for early in sub.sink):
            end = end._replace(
                u=True, why=end.why or f"`{func.id}` can return before asserting a row value"
            )
        if end.u and not end.why and not end.weak:
            end = end._replace(why=f"`{func.id}` does not assert a row value on every path")
        return end

    def assert_cond(self, node: ast.Assert, st: PathState, rescue: bool) -> PathState:
        """`assert a or b` and `assert x if c else True` check the row value only when the left
        side (the condition) leaves it to the other side: conditional on a row-dependent test."""
        test = node.test
        if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.Or):
            left = test.values[0]
            rest = ast.Assert(test=ast.BoolOp(op=ast.Or(), values=test.values[1:]))
            if len(test.values) > 1 and self.dep(left)[0] and _has(rest, self.names):
                after = self.assertion(st, rest, rescue)
                if after != st:
                    return join_states(st, after, True, " ".join(ast.unparse(left).split()), "if")
        if isinstance(test, ast.IfExp) and self.dep(test.test)[0]:
            cond = " ".join(ast.unparse(test.test).split())
            body_trivial = isinstance(test.body, ast.Constant) and bool(test.body.value)
            else_trivial = isinstance(test.orelse, ast.Constant) and bool(test.orelse.value)
            if body_trivial != else_trivial:
                real = test.orelse if body_trivial else test.body
                after = self.assertion(st, ast.Assert(test=real), rescue)
                if after != st:
                    x, y = (st, after) if body_trivial else (after, st)
                    return join_states(x, y, True, cond, "if")
        return self.assertion(st, node, rescue)

    def try_(
        self,
        node: ast.Try | ast.TryStar,
        st: PathState,
        guards: list[tuple[str, bool]],
        rescue: bool,
        depth: int,
    ) -> PathState:
        """An assertion in a `try` whose handler swallows the failure asserts nothing: the body is
        read for the exits in it, and the path goes on as if it had not asserted."""
        end = self.body(node.body, st, guards, rescue, depth + 1)
        if st.u and any(_swallowing(h) for h in node.handlers):
            end = PathState(
                st.u,
                st.a,
                st.weak,
                "an assertion sits inside a try/except that swallows the failure",
            )
        for part in (*(h.body for h in node.handlers), node.orelse, node.finalbody):
            end = self.body(part, end, guards, rescue, depth + 1)
        return end

    def dep(self, node: ast.AST) -> tuple[bool, bool]:
        uses = _has(node, self.names)
        return uses or bool(_MARKER.search(ast.unparse(node))), uses

    def if_(
        self, node: ast.If, st: PathState, guards: list[tuple[str, bool]], rescue: bool, depth: int
    ) -> PathState:
        dep, uses = self.dep(node.test)
        shown = " ".join(ast.unparse(node.test).split())
        flag, neg = _flag(node.test)
        flag = flag and uses
        inner = [*guards, (shown, not uses)] if dep else guards
        if not dep:
            self.uniform += 1
        x = self.body(node.body, st, inner, rescue or (flag and not neg), depth + 1)
        y = self.body(node.orelse, st, inner, rescue or (flag and neg), depth + 1)
        if not dep:
            self.uniform -= 1
        return join_states(x, y, dep, shown, "if")

    def match(
        self,
        node: ast.Match,
        st: PathState,
        guards: list[tuple[str, bool]],
        rescue: bool,
        depth: int,
    ) -> PathState:
        dep, uses = self.dep(node.subject)
        shown = " ".join(ast.unparse(node.subject).split())
        inner = [*guards, (shown, not uses)] if dep else guards
        res = DEAD
        catch_all = False
        if not dep:
            self.uniform += 1
        for case in node.cases:
            arm = f"{shown} matches {' '.join(ast.unparse(case.pattern).split())}"
            catch_all = catch_all or (
                case.guard is None
                and isinstance(case.pattern, ast.MatchAs)
                and case.pattern.pattern is None
            )
            res = join_states(
                res, self.body(case.body, st, inner, rescue, depth + 1), dep, arm, "switch"
            )
        if not dep:
            self.uniform -= 1
        if catch_all:
            return res
        return join_states(res, st, dep, f"{shown} matches a case", "switch")


def row_gap(
    fn: ast.FunctionDef | ast.AsyncFunctionDef,
    helpers: Mapping[str, ast.FunctionDef | ast.AsyncFunctionDef] | None = None,
    fold: tuple[set[str], ast.For] | None = None,
) -> tuple[str, bool]:
    """(why a row of the parametrized test can finish without asserting a row value else "",
    whether the body was too large to read). `helpers` are the functions of the file a call may be
    read through; `fold` is a `subTest` loop that stands for the rows: its variables and the loop,
    whose body is then the walked body."""
    if fold is not None:
        names, loop = fold
        w = _Walk(loop, names, helpers)
        w.fold = True
        stmts: Sequence[ast.stmt] = loop.body
        w.strong = w.strong_exists(loop)
    else:
        names = param_names(fn)
        if not names:
            return "", False
        w = _Walk(fn, names, helpers)
        stmts = fn.body
        w.strong = w.strong_exists(fn)
    try:
        end = w.body(stmts, PathState(True, False, "", ""), [], False, 0)
    except _TooBig:
        return "", True
    w.finish(end)
    if fold is not None and not w.bad and end.u:
        # A new fold is read strictly: a loop body that never asserts a row value is no fold.
        w.bad = "the loop body does not assert a row value on every path"
    return w.bad, False
