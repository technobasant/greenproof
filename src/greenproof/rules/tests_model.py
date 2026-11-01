"""What the test analysers produce: units (tests) and markers (skip/focus) per file."""

from __future__ import annotations

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
    param_cases: int | None = None
    swallowed: int = 0
    swallow_line: int = 0
    expected: list[tuple[str, int]] = field(default_factory=list)  # (literal, line)

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


@dataclass
class FileAnalysis:
    path: str
    engine: str
    units: list[Unit] = field(default_factory=list)
    markers: list[Marker] = field(default_factory=list)
