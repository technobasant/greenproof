"""Remember passes so an unchanged candidate is not re-verified (SPEC 3.1 step 7).

Stored in `<git-common-dir>/greenproof/cache.json`, last 50 entries, passes only. A
failure is never cached, and a corrupt file is treated as empty (a miss just re-runs).
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

MAX_ENTRIES = 50


def cache_key(
    tree: str,
    base_sha: str,
    policy_sha256: str,
    version: str,
    mode: str,
    checks: Sequence[str],
    flags: Sequence[str] = (),
) -> str:
    """sha256 over everything that determines the verdict.

    `flags` carries options that change it beyond the spec's list, e.g. allow-policy-change:
    without it a pass obtained with the flag would be replayed for a run without it.
    """
    material = {
        "tree": tree,
        "base": base_sha,
        "policy": policy_sha256,
        "version": version,
        "mode": mode,
        "checks": list(checks),
        "flags": sorted(flags),
    }
    blob = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


class PassCache:
    def __init__(self, directory: Path) -> None:
        self.path = directory / "cache.json"

    def _load(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text())
            entries = data["entries"]
        except (OSError, ValueError, KeyError, TypeError):
            return []
        if not isinstance(entries, list):
            return []
        return [e for e in entries if isinstance(e, dict) and isinstance(e.get("key"), str)]

    def get_pass(self, key: str) -> list[dict[str, Any]] | None:
        """The check statuses of the original passing run, or None on a miss."""
        for entry in self._load():
            checks = entry.get("checks")
            if entry["key"] == key and isinstance(checks, list):
                return [c for c in checks if isinstance(c, dict)]
        return None

    def has_pass(self, key: str) -> bool:
        return self.get_pass(key) is not None

    def record_pass(self, key: str, checks: Sequence[Mapping[str, Any]]) -> None:
        entries = [e for e in self._load() if e["key"] != key]
        entries.append({"key": key, "at": int(time.time()), "checks": [dict(c) for c in checks]})
        entries = entries[-MAX_ENTRIES:]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix="cache-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump({"entries": entries}, handle)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
