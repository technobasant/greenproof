"""What the test analysers produce: units (tests) and markers (skip/focus) per file."""

from __future__ import annotations

import ast
from dataclasses import dataclass, field


@dataclass
class Unit:
    """One test. `key` is its qualified name without the path, so a move keeps its identity."""

    path: str
    key: str
    line: int
    body_hash: str
    engine: str  # "python" (AST), "python-lines", "js", "go", "rust"
    asserts: int = 0
    strong: int = 0
    weak: int = 0
    broad_raises: int = 0
    specific_raises: int = 0
    tolerances: dict[str, float] = field(default_factory=dict)  # rel/abs/delta max, places min
    param_cases: int | None = None  # product over literal parametrize decorators, else None
    param_literal: bool = True  # False when some parametrize argvalues is not a literal list
    param_forms: list[str] = field(default_factory=list)  # exact form of each literal case
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
    strong_operands: set[str] = field(default_factory=set)
    weak_operands: set[str] = field(default_factory=set)
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
