"""Local acceptances (SPEC 6.6): `greenproof accept`, `--list`, `--revoke`.

Advisory only: the file is writable by anything running as the user, an agent included.
Local and hook modes honour it; ci mode ignores it (CI reads PR comments instead, see
`github.py`).
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from greenproof import gitops
from greenproof.errors import UsageError
from greenproof.findings import FINGERPRINT_LENGTH

ACCEPTED_FILE = "accepted.json"
_FINGERPRINT = re.compile(rf"[0-9a-f]{{{FINGERPRINT_LENGTH}}}")


@dataclass(frozen=True)
class Acceptance:
    by: str
    at: str
    source: str  # "local" or "github"
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"by": self.by, "at": self.at, "source": self.source, "reason": self.reason}


def is_fingerprint(text: str) -> bool:
    return _FINGERPRINT.fullmatch(text) is not None


def accepted_path(repo: Path) -> Path:
    return gitops.greenproof_dir(repo) / ACCEPTED_FILE


def load_local(repo: Path) -> dict[str, Acceptance]:
    """Fingerprint to acceptance. A missing or malformed file counts as empty."""
    try:
        data = json.loads(accepted_path(repo).read_text())
        table = data["accepted"]
    except (OSError, ValueError, KeyError, TypeError):
        return {}
    if not isinstance(table, dict):
        return {}
    out: dict[str, Acceptance] = {}
    for fp, entry in table.items():
        if not (isinstance(fp, str) and is_fingerprint(fp) and isinstance(entry, dict)):
            continue
        by, at, reason = entry.get("by"), entry.get("at"), entry.get("reason")
        if (
            isinstance(by, str)
            and isinstance(at, str)
            and (reason is None or isinstance(reason, str))
        ):
            out[fp] = Acceptance(by, at, "local", reason)
    return out


def _save(repo: Path, table: dict[str, Acceptance]) -> None:
    path = accepted_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "accepted": {
            fp: {"by": a.by, "at": a.at, "reason": a.reason} for fp, a in sorted(table.items())
        }
    }
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix="accepted-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def identity(repo: Path) -> str:
    """`Name <email>` from git config; whichever part is missing is left out."""

    def config(key: str) -> str:
        proc = gitops.git(repo, "config", key, check=False)
        return proc.stdout.decode(errors="replace").strip() if proc.returncode == 0 else ""

    name, email = config("user.name"), config("user.email")
    if name and email:
        return f"{name} <{email}>"
    return name or email or "unknown"


def _check_fingerprints(fingerprints: Iterable[str]) -> list[str]:
    out = list(fingerprints)
    for fp in out:
        if not is_fingerprint(fp):
            raise UsageError(
                f"{fp!r} is not a fingerprint: expected {FINGERPRINT_LENGTH} lowercase hex digits"
                " (shown as [fp ...] in the report)"
            )
    return out


def accept(repo: Path, fingerprints: Iterable[str], reason: str | None) -> list[str]:
    fps = _check_fingerprints(fingerprints)
    if not fps:
        raise UsageError("accept: give at least one fingerprint (or --list, or --revoke FP)")
    table = load_local(repo)
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    who = identity(repo)
    for fp in fps:
        table[fp] = Acceptance(who, now, "local", reason or None)
    _save(repo, table)
    return fps


def revoke(repo: Path, fingerprint: str) -> None:
    (fp,) = _check_fingerprints([fingerprint])
    table = load_local(repo)
    if fp not in table:
        raise UsageError(f"{fp} is not accepted")
    del table[fp]
    _save(repo, table)


def render_list(table: dict[str, Acceptance]) -> str:
    if not table:
        return "greenproof: no accepted findings\n"
    lines = []
    for fp, a in sorted(table.items()):
        reason = f"  {a.reason}" if a.reason else ""
        lines.append(f"{fp}  {a.at}  {a.by}{reason}")
    return "\n".join(lines) + "\n"
