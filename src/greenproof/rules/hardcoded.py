"""code.hardcoded: non-test code gains a table or a branch on a literal from the tests (SPEC 5.8).

Only literals that appear in added lines of Python test files changed in this diff count (strings
of three or more characters, numbers other than 0, 1 and -1). A non-test Python file then fires
when a function (or the module) gains, compared with the same scope at the base, either

1. a literal display with eight or more constant entries holding a counted literal, or
2. a branch comparing a name, attribute or subscript with a counted literal whose body
   returns or assigns a literal.

A table or branch is identified by its counted literal (and, for a branch, the compared
expression), so one that already existed in the scope at the base never fires.
"""

from __future__ import annotations

import ast
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass

from greenproof.changeset import ChangeSet
from greenproof.findings import Finding
from greenproof.rules._common import finding, split_lines

MODULE = "<module>"
_TABLE_ENTRIES = 8
_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef)
_Lit = tuple[str, object]  # ("s", text) or ("n", number)
_Key = tuple[str, str, _Lit]  # kind, comparand, literal


def _literal(node: ast.AST) -> _Lit | None:
    """The counted literal a node is: a string of 3+ characters or a number other than 0/1/-1."""
    sign = 1
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        node, sign = node.operand, -1
    if not isinstance(node, ast.Constant) or isinstance(node.value, bool):
        return None
    value = node.value
    if isinstance(value, str) and sign == 1:
        return ("s", value) if len(value) >= 3 else None
    if isinstance(value, (int, float)):
        number = sign * value
        return None if number in (0, 1, -1) else ("n", number)
    return None


def _constant(node: ast.AST) -> bool:
    try:
        ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return False
    return True


def _show(lit: _Lit) -> str:
    return repr(lit[1])


# ---- literals the tests add ---------------------------------------------------------------


def _added_lines(old: str | None, new: str) -> set[int]:
    """Lines of `new` whose content is not left over in `old` (a changed line counts as added)."""
    pool: Counter[str] = Counter()
    if old is not None:
        pool.update(ln.strip() for ln in split_lines(old))
    added: set[int] = set()
    for number, raw in enumerate(split_lines(new), start=1):
        line = raw.strip()
        if pool[line] > 0:
            pool[line] -= 1
        elif line:
            added.add(number)
    return added


def _test_literals(tree: ast.AST, added: set[int]) -> Iterator[_Lit]:
    skip: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            skip.add(id(node.value))  # a docstring or a bare string statement
        elif isinstance(node, ast.JoinedStr):
            skip.update(id(v) for v in ast.walk(node))
        elif isinstance(node, ast.UnaryOp) and _literal(node) is not None:
            skip.add(id(node.operand))
            if getattr(node, "lineno", 0) in added:
                lit = _literal(node)
                assert lit is not None
                yield lit
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and id(node) not in skip
            and getattr(node, "lineno", 0) in added
        ):
            lit = _literal(node)
            if lit is not None:
                yield lit


# ---- what a scope contains ----------------------------------------------------------------


@dataclass(frozen=True)
class _Item:
    key: _Key
    line: int
    lit: _Lit
    kind: str  # "table" or "branch"


def _literal_body(stmts: list[ast.stmt], counted: dict[_Lit, str]) -> bool:
    """The body returns or assigns a counted test literal: the answer comes from the tests too
    (nested definitions are not part of the body)."""
    todo: list[ast.AST] = list(stmts)
    while todo:
        node = todo.pop()
        if isinstance(node, (*_DEFS, ast.ClassDef)):
            continue
        value = None
        if isinstance(node, (ast.Return, ast.Assign, ast.AnnAssign, ast.AugAssign)):
            value = node.value
        if value is not None and _literal(value) in counted:
            return True
        todo.extend(ast.iter_child_nodes(node))
    return False


def _comparand(node: ast.AST) -> bool:
    return isinstance(node, (ast.Name, ast.Attribute, ast.Subscript))


def _comparisons(test: ast.AST, counted: dict[_Lit, str]) -> Iterator[tuple[str, _Lit]]:
    """(compared expression, counted literal) for each `x == L`, `L == x` and `x in (L, ...)`."""
    if isinstance(test, ast.BoolOp):
        for value in test.values:
            yield from _comparisons(value, counted)
        return
    if not isinstance(test, ast.Compare) or len(test.ops) != 1:
        return
    left, right = test.left, test.comparators[0]
    op = test.ops[0]
    if isinstance(op, ast.Eq):
        for subject, other in ((left, right), (right, left)):
            lit = _literal(other)
            if _comparand(subject) and lit is not None and lit in counted:
                yield ast.dump(subject), lit
    elif (
        isinstance(op, ast.In)
        and _comparand(left)
        and isinstance(right, ast.Tuple | ast.List | ast.Set)
    ):
        for element in right.elts:
            lit = _literal(element)
            if lit is not None and lit in counted:
                yield ast.dump(left), lit


def _table(
    node: ast.Dict | ast.List | ast.Tuple | ast.Set, counted: dict[_Lit, str]
) -> _Lit | None:
    """A counted literal inside a display with eight or more constant entries, else None."""
    if isinstance(node, ast.Dict):
        entries = sum(
            1
            for k, v in zip(node.keys, node.values, strict=True)
            if k is not None and _constant(k) and _constant(v)
        )
    else:
        entries = sum(1 for e in node.elts if _constant(e))
    if entries < _TABLE_ENTRIES:
        return None
    for inner in ast.walk(node):
        lit = _literal(inner)
        if lit is not None and lit in counted:
            return lit
    return None


def _match_values(
    node: ast.Match, case: ast.match_case, counted: dict[_Lit, str]
) -> Iterator[_Item]:
    if not _comparand(node.subject):
        return
    pattern = case.pattern
    options = pattern.patterns if isinstance(pattern, ast.MatchOr) else [pattern]
    for option in options:
        if isinstance(option, ast.MatchValue):
            lit = _literal(option.value)
            if lit is not None and lit in counted:
                key: _Key = ("branch", ast.dump(node.subject), lit)
                yield _Item(key, option.lineno, lit, "branch")


def _here(node: ast.AST, counted: dict[_Lit, str]) -> tuple[list[_Item], bool]:
    """Items at `node` itself, and whether it is a table (whose insides are the same table)."""
    out: list[_Item] = []
    if isinstance(node, (ast.Dict, ast.List, ast.Tuple, ast.Set)):
        lit = _table(node, counted)
        if lit is not None:
            return [_Item(("table", "", lit), node.lineno, lit, "table")], True
    elif isinstance(node, ast.If):
        if _literal_body(node.body, counted):
            for subject, lit in _comparisons(node.test, counted):
                out.append(_Item(("branch", subject, lit), node.lineno, lit, "branch"))
    elif isinstance(node, ast.Match):
        for case in node.cases:
            if _literal_body(case.body, counted):
                out.extend(_match_values(node, case, counted))
    return out, False


def _scopes(tree: ast.Module, counted: dict[_Lit, str]) -> dict[str, list[_Item]]:
    """Scope name (qualified function or class, else `<module>`) -> the items found in it."""
    scopes: dict[str, list[_Item]] = {MODULE: []}

    def walk(node: ast.AST, scope: str, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (*_DEFS, ast.ClassDef)):
                name = f"{prefix}{child.name}"
                scopes.setdefault(name, [])
                walk(child, name, f"{name}.")
                continue
            items, is_table = _here(child, counted)
            scopes[scope].extend(items)
            if not is_table:
                walk(child, scope, prefix)

    walk(tree, MODULE, "")
    return scopes


def _parse(text: str | None) -> ast.Module | None:
    if text is None:
        return None
    try:
        return ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return None


def check_hardcoded(changes: ChangeSet) -> list[Finding]:
    counted: dict[_Lit, str] = {}  # literal -> the first test file that adds it
    for f in changes.files:
        if not f.is_test or not f.path.endswith(".py") or f.new_text is None:
            continue
        tree = _parse(f.new_text)
        if tree is None:
            continue
        for lit in _test_literals(tree, _added_lines(f.old_text, f.new_text)):
            counted.setdefault(lit, f.path)
    if not counted:
        return []
    out: list[Finding] = []
    for f in changes.non_tests():
        if not f.path.endswith(".py") or f.new_text is None:
            continue
        tree = _parse(f.new_text)
        base = _parse(f.old_text)
        if tree is None or (f.old_text is not None and base is None):
            continue
        before = _scopes(base, counted) if base is not None else {}
        for scope, items in _scopes(tree, counted).items():
            existing = {item.key for item in before.get(scope, [])}
            for item in items:
                if item.key in existing:
                    continue
                existing.add(item.key)  # one finding per table or branch
                what = "a table" if item.kind == "table" else f"a branch on {_show(item.lit)}"
                test_file = counted[item.lit]
                out.append(
                    finding(
                        "code.hardcoded",
                        f.path,
                        item.line,
                        f"{f.path}: {scope} gained {what} whose value also appears in "
                        f"{test_file}; it may special-case the test input",
                        "Implement the behaviour the tests describe instead of returning the "
                        "expected values; a lookup table of test inputs passes only those inputs.",
                    )
                )
    return out
