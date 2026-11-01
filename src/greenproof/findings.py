"""The finding object every rule produces."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

BLOCK = "block"
WARN = "warn"
OFF = "off"
SEVERITIES = (BLOCK, WARN, OFF)


@dataclass(frozen=True)
class Finding:
    rule: str
    severity: str
    file: str | None
    line: int | None
    message: str
    hint: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
