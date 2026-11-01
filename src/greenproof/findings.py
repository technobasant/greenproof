"""The finding object every rule produces."""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from typing import Any

BLOCK = "block"
WARN = "warn"
OFF = "off"
SEVERITIES = (BLOCK, WARN, OFF)

FINGERPRINT_LENGTH = 12
# Line references move when unrelated code above them changes; the fingerprint ignores them.
_LINE_REFERENCE = re.compile(r"\bline \d+|:\d+\b")


def fingerprint_of(rule: str, path: str | None, message: str) -> str:
    """First 12 hex digits of sha256 over rule, path and message (SPEC 6.6), with `line N`
    and `:N` references removed from the message."""
    stable = _LINE_REFERENCE.sub("", message)
    material = "\0".join((rule, path or "", stable))
    return hashlib.sha256(material.encode()).hexdigest()[:FINGERPRINT_LENGTH]


@dataclass(frozen=True)
class Finding:
    rule: str
    severity: str
    file: str | None
    line: int | None
    message: str
    hint: str

    @property
    def fingerprint(self) -> str:
        return fingerprint_of(self.rule, self.file, self.message)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
