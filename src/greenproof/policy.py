"""Load and strictly validate `.greenproof.toml` (SPEC section 4).

The policy that governs a run is the one at the base revision. When the base has none
(first adoption) the candidate's is used and the source is recorded as "candidate".
"""

from __future__ import annotations

import hashlib
import tomllib
from dataclasses import dataclass, field
from typing import Any

from greenproof.errors import PolicyError
from greenproof.findings import BLOCK, SEVERITIES, Finding
from greenproof.registry import RULES

POLICY_FILE = ".greenproof.toml"
CHECK_IDS = ("static", "commands", "revert", "smoke")
MODES = ("hook", "local", "ci")
# Run in this order. `setup` is only used for --isolate and revert worktrees.
COMMAND_NAMES = ("lint", "typecheck", "test", "build")
_COMMAND_KEYS = ("setup", *COMMAND_NAMES, "timeout")
DEFAULT_TIMEOUT = 900
DEFAULT_SMOKE_TIMEOUT = 60
DEFAULT_MODE_CHECKS: dict[str, tuple[str, ...]] = {
    "hook": ("static", "commands"),
    "local": ("static", "commands"),
    "ci": ("static", "commands", "revert", "smoke"),
}


@dataclass(frozen=True)
class Smoke:
    start: str
    ready_url: str
    probes: tuple[str, ...]
    timeout: int = DEFAULT_SMOKE_TIMEOUT


@dataclass(frozen=True)
class Policy:
    commands: dict[str, str] = field(default_factory=dict)
    timeout: int = DEFAULT_TIMEOUT
    test_paths: tuple[str, ...] = ()
    smoke: Smoke | None = None
    rules: dict[str, str] = field(default_factory=dict)
    mode_checks: dict[str, tuple[str, ...]] = field(
        default_factory=lambda: dict(DEFAULT_MODE_CHECKS)
    )

    def runnable_commands(self) -> list[tuple[str, str]]:
        """(name, command) in run order; empty entries are skipped and `setup` is excluded."""
        return [(n, self.commands[n]) for n in COMMAND_NAMES if self.commands.get(n, "").strip()]


@dataclass(frozen=True)
class LoadedPolicy:
    policy: Policy
    source: str  # "base" or "candidate"
    sha256: str
    changed: bool = False
    change_detail: str = ""


def _type_name(value: Any) -> str:
    return type(value).__name__


def _table(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PolicyError(f"{path}: expected a table, got {_type_name(value)}")
    return value


def _check_keys(table: dict[str, Any], allowed: tuple[str, ...], path: str) -> None:
    for key in table:
        if key not in allowed:
            where = f"{path}.{key}" if path else key
            raise PolicyError(
                f"{where}: unknown key (allowed here: {', '.join(allowed) or 'none'})"
            )


def _string(value: Any, path: str) -> str:
    if not isinstance(value, str):
        raise PolicyError(f"{path}: expected a string, got {_type_name(value)}")
    return value


def _positive_int(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PolicyError(f"{path}: expected an integer, got {_type_name(value)}")
    if value <= 0:
        raise PolicyError(f"{path}: must be greater than zero, got {value}")
    return value


def _string_list(value: Any, path: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise PolicyError(f"{path}: expected an array of strings, got {_type_name(value)}")
    for i, item in enumerate(value):
        _string(item, f"{path}[{i}]")
    return tuple(value)


def parse_policy(text: str, label: str = POLICY_FILE) -> Policy:
    """Parse and validate policy text. Raises PolicyError naming the offending key."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise PolicyError(f"{label}: invalid TOML: {exc}") from exc
    return _validate(data)


def _validate(data: dict[str, Any]) -> Policy:
    _check_keys(data, ("version", "commands", "tests", "smoke", "rules", "modes"), "")
    if "version" not in data:
        raise PolicyError("version: required (use version = 1)")
    version = data["version"]
    if isinstance(version, bool) or version != 1:
        raise PolicyError(f"version: unsupported value {version!r} (this greenproof reads 1)")

    commands: dict[str, str] = {}
    timeout = DEFAULT_TIMEOUT
    if "commands" in data:
        table = _table(data["commands"], "commands")
        _check_keys(table, _COMMAND_KEYS, "commands")
        for name in ("setup", *COMMAND_NAMES):
            if name in table:
                commands[name] = _string(table[name], f"commands.{name}")
        if "timeout" in table:
            timeout = _positive_int(table["timeout"], "commands.timeout")

    test_paths: tuple[str, ...] = ()
    if "tests" in data:
        table = _table(data["tests"], "tests")
        _check_keys(table, ("paths",), "tests")
        if "paths" in table:
            test_paths = _string_list(table["paths"], "tests.paths")

    smoke: Smoke | None = None
    if "smoke" in data:
        table = _table(data["smoke"], "smoke")
        _check_keys(table, ("start", "ready_url", "probes", "timeout"), "smoke")
        required = ("start", "ready_url", "probes")
        present = [k for k in required if k in table]
        if present:
            for key in required:
                if key not in table:
                    raise PolicyError(f"smoke.{key}: required when any smoke key is set")
            smoke = Smoke(
                start=_string(table["start"], "smoke.start"),
                ready_url=_string(table["ready_url"], "smoke.ready_url"),
                probes=_string_list(table["probes"], "smoke.probes"),
                timeout=(
                    _positive_int(table["timeout"], "smoke.timeout")
                    if "timeout" in table
                    else DEFAULT_SMOKE_TIMEOUT
                ),
            )

    rules: dict[str, str] = {}
    if "rules" in data:
        table = _table(data["rules"], "rules")
        for rule_id, severity in table.items():
            if rule_id not in RULES:
                raise PolicyError(f"rules.{rule_id}: unknown rule id (see `greenproof explain`)")
            sev = _string(severity, f"rules.{rule_id}")
            if sev not in SEVERITIES:
                raise PolicyError(
                    f"rules.{rule_id}: severity must be one of {', '.join(SEVERITIES)}, got {sev!r}"
                )
            rules[rule_id] = sev

    mode_checks = dict(DEFAULT_MODE_CHECKS)
    if "modes" in data:
        table = _table(data["modes"], "modes")
        _check_keys(table, MODES, "modes")
        for mode, mode_table in table.items():
            body = _table(mode_table, f"modes.{mode}")
            _check_keys(body, ("checks",), f"modes.{mode}")
            if "checks" in body:
                checks = _string_list(body["checks"], f"modes.{mode}.checks")
                for check in checks:
                    if check not in CHECK_IDS:
                        raise PolicyError(
                            f"modes.{mode}.checks: unknown check {check!r} "
                            f"(allowed: {', '.join(CHECK_IDS)})"
                        )
                mode_checks[mode] = checks

    return Policy(
        commands=commands,
        timeout=timeout,
        test_paths=test_paths,
        smoke=smoke,
        rules=rules,
        mode_checks=mode_checks,
    )


def _decode(blob: bytes, label: str) -> str:
    try:
        return blob.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PolicyError(f"{label}: not valid UTF-8") from exc


def _difference(base_text: str, candidate: bytes | None) -> str:
    """Why the candidate's policy differs from the base's, or "" when it does not."""
    if candidate is None:
        return f"{POLICY_FILE} was deleted"
    try:
        same = tomllib.loads(candidate.decode("utf-8")) == tomllib.loads(base_text)
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return f"{POLICY_FILE} no longer parses as valid TOML"
    return "" if same else f"{POLICY_FILE} was edited"


def load_policy(base_blob: bytes | None, candidate_blob: bytes | None) -> LoadedPolicy:
    """Pick the governing policy: the base's, else the candidate's (first adoption).

    A change is detected on the parsed content, so comments and formatting do not count.
    """
    if base_blob is None:
        if candidate_blob is None:
            raise PolicyError(
                f"{POLICY_FILE}: not found at the base revision or in the working tree. "
                "Run `greenproof init`."
            )
        text = _decode(candidate_blob, f"{POLICY_FILE} (working tree)")
        policy = parse_policy(text, f"{POLICY_FILE} (working tree)")
        return LoadedPolicy(policy, "candidate", hashlib.sha256(candidate_blob).hexdigest())

    label = f"{POLICY_FILE} (base revision)"
    text = _decode(base_blob, label)
    policy = parse_policy(text, label)
    detail = _difference(text, candidate_blob)
    return LoadedPolicy(
        policy,
        "base",
        hashlib.sha256(base_blob).hexdigest(),
        changed=bool(detail),
        change_detail=detail,
    )


def policy_changed_finding(loaded: LoadedPolicy) -> Finding:
    return Finding(
        rule="policy.changed",
        severity=BLOCK,
        file=POLICY_FILE,
        line=None,
        message=f"{loaded.change_detail} relative to the base revision; this run used the base's",
        hint="Review the policy change yourself; rerun with --allow-policy-change if intended.",
    )
