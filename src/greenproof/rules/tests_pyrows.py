"""Whether a parametrized Python test asserts a row value on every path: the Python side of the
table-fold rule (tests_flow.py reads JS, Go and Rust). A fold into one parametrized test earns only
a warning when no row leaves early (a `return`, `pytest.skip`) and the row values reach an assertion
on every path; the first path that does not is named in the finding.

Linear in the body: each statement is visited once, and a `with pytest.raises` block is read as the
one assertion it is."""

from __future__ import annotations

import ast
import re
from collections.abc import Sequence

from greenproof.rules.tests_flow import DEAD, MAX_DEPTH, PathState, join_states

MAX_NODES = 20_000
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


class _Walk:
    def __init__(self, fn: ast.FunctionDef | ast.AsyncFunctionDef, names: set[str]) -> None:
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
            self.report(st, "return", guards)
            return DEAD
        if isinstance(node, ast.Raise):
            return DEAD
        if isinstance(node, ast.Assert):
            if isinstance(node.test, ast.Constant) and not node.test.value:
                return DEAD
            return self.assertion(st, node, rescue)
        if isinstance(node, ast.Expr):
            v = node.value
            if isinstance(v, ast.Call):
                tail = _tail(v.func)
                if tail in _FAILS:
                    return DEAD
                if tail in _SKIPS:
                    self.report(st, ast.unparse(v.func), guards)
                    return DEAD
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
            end = self.body(node.body, st, guards, rescue, depth + 1)
            return st if not (end.u or end.a) else end
        if isinstance(node, ast.Try):
            for part in (node.body, *(h.body for h in node.handlers), node.orelse, node.finalbody):
                st = self.body(part, st, guards, rescue, depth + 1)
            return st
        if isinstance(node, (ast.With, ast.AsyncWith)):
            if self.classify(node) is not None:
                return self.assertion(st, node, rescue)
            return self.body(node.body, st, guards, rescue, depth + 1)
        if isinstance(node, ast.Match):
            return self.match(node, st, guards, rescue, depth)
        return st

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
        if not dep:
            self.uniform += 1
        for case in node.cases:
            res = join_states(
                res, self.body(case.body, st, inner, rescue, depth + 1), dep, shown, "switch"
            )
        if not dep:
            self.uniform -= 1
        return join_states(res, st, dep, shown, "switch")


def row_gap(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[str, bool]:
    """(why a row of the parametrized test can finish without asserting a row value else "",
    whether the body was too large to read)."""
    names = param_names(fn)
    if not names:
        return "", False
    w = _Walk(fn, names)
    w.strong = w.strong_exists(fn)
    try:
        end = w.body(fn.body, PathState(True, False, "", ""), [], False, 0)
    except _TooBig:
        return "", True
    w.finish(end)
    return w.bad, False
