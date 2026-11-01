"""Helpers shared by the rule families."""

from __future__ import annotations

import hashlib
import re

from greenproof.findings import WARN, Finding
from greenproof.registry import RULES

JS_SUFFIXES = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts")
CODE_SUFFIXES = (*JS_SUFFIXES, ".py", ".go", ".rs", ".java", ".kt", ".rb", ".c", ".cc", ".cpp")


def finding(rule: str, file: str | None, line: int | None, message: str, hint: str) -> Finding:
    """A finding at the rule's registered default severity (policy overrides apply later)."""
    return Finding(rule, RULES[rule].default_severity, file, line, message, hint)


def warn_finding(rule: str, file: str | None, line: int | None, message: str, hint: str) -> Finding:
    """A finding that stays a warning unless the policy overrides the rule explicitly."""
    return Finding(rule, WARN, file, line, message, hint)


def language_of(path: str) -> str | None:
    if path.endswith(".py"):
        return "python"
    if path.endswith(JS_SUFFIXES):
        return "js"
    if path.endswith(".go"):
        return "go"
    if path.endswith(".rs"):
        return "rust"
    return None


def digest(text: str) -> str:
    return hashlib.sha256(re.sub(r"\s+", "", text).encode()).hexdigest()[:16]


def split_lines(text: str) -> list[str]:
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
