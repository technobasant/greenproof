"""What the test analysers produce: units (tests) and markers (skip/focus) per file."""

from __future__ import annotations

import ast
from collections.abc import Iterable
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Table:
    """The rows a table-driven JS, Go or Rust test runs: `test.each`/`it.each`/`describe.each`,
    a `[]struct` or map ranged over, rstest `#[case]` or `#[test_case]`.

    `rows` holds the literal tokens of each row (numbers, strings, true/false/null) and `forms`
    its whitespace-free text. `runs` is True only when every row reaches the test body and is
    asserted there: the wrappers on the way are live (not skipped, not inverted by `.failing` or
    `#[should_panic]`), the body reads a parameter, no row leaves before asserting, and some
    parameter reaches an assertion. `why` says which of these failed when `runs` is False.

    `cells` holds each row's values by column (`(column, tokens)`), `reach` whether the
    parameter of each column is read by an assertion, and `reach_all` whether every parameter
    the body reads is: a fold is credited only when the columns that carry the removed test's
    values reach an assertion. `note` is a limit of the reading (a body too large to check).
    """

    rows: tuple[tuple[str, ...], ...]
    forms: tuple[str, ...]
    runs: bool
    implicit: bool = False  # Rust `#[test_case(.. => expected)]`: each row is its own check
    why: str = ""
    note: str = ""
    cells: tuple[tuple[tuple[str, tuple[str, ...]], ...], ...] = ()
    reach: tuple[tuple[str, bool], ...] = ()
    reach_all: bool = True

    def unasserted(self, index: int, tokens: Iterable[str]) -> str:
        """Why row `index` does not put these values (a removed test's literals) under an
        assertion, else "". The column of each value must reach an assertion; when the columns
        cannot be told apart, every parameter must."""
        if self.reach_all:
            return ""
        reach = dict(self.reach)
        cells = self.cells[index] if index < len(self.cells) else ()
        for tok in tokens:
            keys = [k for k, toks in cells if tok in toks]
            if keys and all(k in reach for k in keys):
                if not any(reach[k] for k in keys):
                    shown = ", ".join(f"`{k}`" for k in keys)
                    value = tok.split(":", 1)[-1]
                    return f"the row's value {value} is bound to {shown}, which no assertion reads"
            else:
                return (
                    "a row parameter that no assertion reads, and the columns cannot be told apart"
                )
        return ""


@dataclass(frozen=True)
class RowTab:
    """A literal table of rows that drives a Python test's checks: a `for` loop (a subTest fold
    or a plain loop) or a `parametrize` decorator. `rows` is the number of distinct rows, None
    when the table cannot be read (`why` is "altered" for a module-level name re-bound or mutated
    after its literal, "opaque" otherwise). `reads` is whether an assertion reads the row's
    variables; `sig` the assertion forms the loop body checks; `keys` the distinct rows."""

    kind: str  # "loop" or "param"
    names: tuple[str, ...]
    sig: str
    rows: int | None
    reads: bool
    why: str = ""
    name: str = ""  # the module-level name the table is read through, else ""
    keys: tuple[str, ...] = ()
    literal: bool = True  # a parametrize whose argvalues are a literal (the case count covers it)


@dataclass
class Unit:
    """One test. `key` is its qualified name without the path, so a move keeps its identity."""

    path: str
    key: str
    line: int
    body_hash: str
    engine: str  # "python" (AST), "python-lines", "js", "go", "rust"
    asserts: int = 0
    # JS, Go, Rust: the checks with each same-file helper call at what its body checks and each
    # literal-table loop at its distinct rows. It never lowers a block; it only turns a drop of
    # `asserts` into a warning when the candidate's is no lower than the base's (SPEC 5.4).
    credited: int = 0
    # Go: the sorted literals of the test outside messages and names (table rows and the bodies
    # of the helpers it calls included), and those of its check lines and table rows. A credit
    # turns a drop into a warning only when every literal of the base's checks is still among
    # the candidate's (SPEC 5.4).
    credit_lits: tuple[str, ...] = ()
    check_lits: tuple[str, ...] = ()
    strong: int = 0
    weak: int = 0
    broad_raises: int = 0
    specific_raises: int = 0
    tolerances: dict[str, float] = field(default_factory=dict)  # rel/abs/delta max, places min
    param_cases: int | None = None  # product over literal parametrize decorators, else None
    param_literal: bool = True  # False when some parametrize argvalues is not a literal list
    param_forms: list[str] = field(default_factory=list)  # exact form of each literal case
    # Python: why a parametrized row can finish without asserting a row value (tests_pyrows.py),
    # whether that body was too large to read, the (form, mark kinds) of the rows marked skip or
    # xfail, and the skip, skipif and xfail marks on the test itself
    row_gap: str = ""
    row_big: bool = False
    param_marked: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)
    row_tabs: tuple[RowTab, ...] = ()  # Python: the literal row tables that drive its checks
    mark_kinds: set[str] = field(default_factory=set)  # skip, skipif, xfail marks on the test
    subjects: set[str] = field(default_factory=set)  # dotted names of the code under test called
    swallowed: int = 0
    swallow_line: int = 0
    expected: list[tuple[str, int]] = field(default_factory=list)  # (literal, line)
    # Python only: structural identity and per-assertion shapes (identifiers alpha-renamed),
    # used for the 5.4 severity split.
    struct_hash: str = ""
    shapes: list[str] = field(default_factory=list)
    # Exact form of each counted, reachable assertion (all engines); coverage compares these.
    assertion_forms: list[str] = field(default_factory=list)
    vacuous: int = 0
    vacuous_line: int = 0
    # JS/TS, Go, Rust: self-comparisons whose operand has a call, as (normalised operand, line),
    # and the normalised operands of comparisons of two different expressions. A self-comparison
    # is vacuous only when its operand was compared with something else at the base (SPEC 5.4).
    self_calls: list[tuple[str, int]] = field(default_factory=list)
    compared: set[str] = field(default_factory=set)
    unreachable: int = 0
    unreachable_line: int = 0
    # Python only (SPEC 5.4): shape -> source of each subject of an exact check, and of each
    # target of a loose check; exact checks pinned to a literal (SPEC 5.7); operand shapes of
    # comparisons and of bare truthiness checks.
    exact_subjects: dict[str, str] = field(default_factory=dict)
    loose_targets: dict[str, str] = field(default_factory=dict)
    pinned: int = 0
    # Python only: forms of assertions that are loose checks only (no exact check inside).
    loose_forms: list[str] = field(default_factory=list)
    # Python only: (subject shape, expected number) -> (tightest tolerance band, subject source)
    # over the body's comparisons to a numeric literal; exact is band 0 (SPEC 5.4).
    numeric_bands: dict[tuple[str, float], tuple[float, str]] = field(default_factory=dict)
    # Python only (SPEC 5.4): calls compared with themselves (`f(x) == f(x)`) and the calls that
    # were operands of a real comparison; a self-comparison is vacuous only where it replaces one.
    call_self: dict[str, int] = field(default_factory=dict)
    call_self_line: int = 0
    call_compared: set[str] = field(default_factory=set)
    strong_operands: set[str] = field(default_factory=set)
    weak_operands: set[str] = field(default_factory=set)
    # JS, Go, Rust: the literals of the body (numbers and strings, never the test's name nor the
    # table's rows) and the literal table the test runs, if any. They let a table-driven test
    # account for the plain tests it replaced (tests_tables.py).
    literals: tuple[str, ...] = ()
    table: Table | None = None
    # Collection facts: enclosing classes, whether one is a TestCase, `__test__ = False`.
    classes: tuple[str, ...] = ()
    testcase: bool = False
    disabled: bool = False
    # Last line of a line-engine test (1-based); 0 when the engine does not track it.
    end_line: int = 0
    # Python only: the test's function node, for rules that read the body (self_mocked).
    node: ast.AST | None = field(default=None, repr=False, compare=False)

    @property
    def name(self) -> str:
        return self.key.rsplit("::", 1)[-1] if self.engine.startswith("python") else self.key


@dataclass
class Fixture:
    """A `@pytest.fixture` function: where it is, whether every test in scope uses it, and the
    literals it returns or yields (the expected values a test may compare against)."""

    path: str
    name: str  # `Class::name` for a fixture defined in a class
    line: int
    autouse: bool
    literals: list[str]  # ast.dump of each returned or yielded literal
    body_hash: str
    scope: str = ""  # the class prefix (`Class::`) it is defined in, else ""
    node: ast.AST | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class Marker:
    """A skip or focus marker. `scope` is the unit key it sits in, else `<path>::<module>`."""

    scope: str
    kind: str
    text: str
    line: int
    rule: str  # tests.skipped or tests.focused
    conditional: bool = False  # skipif(<condition>), importorskip: depends on the environment


@dataclass
class FileAnalysis:
    path: str
    engine: str
    units: list[Unit] = field(default_factory=list)
    markers: list[Marker] = field(default_factory=list)
    collected: bool = True  # whether the runner's file patterns pick this file up
    tree: ast.Module | None = field(default=None, repr=False, compare=False)  # Python only
    # Go only: assertion helpers this file declares (SPEC 5.4), so callers can be re-analysed.
    helpers: frozenset[str] = frozenset()
    # Python only: paths of the test-path modules whose functions this file imports and may call.
    imports: set[str] = field(default_factory=set)
    fixtures: dict[str, Fixture] = field(default_factory=dict)  # Python only, by name
