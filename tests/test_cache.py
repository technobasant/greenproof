from __future__ import annotations

import json
from pathlib import Path

from greenproof.cache import MAX_ENTRIES, PassCache, cache_key

CHECKS = [{"id": "static", "status": "pass", "duration_ms": 3}]
ARGS = ("tree", "base", "policy", "0.1.0", "local", ["static", "commands"])


def test_key_changes_with_every_input() -> None:
    base = cache_key(*ARGS)
    assert cache_key(*ARGS) == base
    variants = [
        ("tree2", "base", "policy", "0.1.0", "local", ["static", "commands"]),
        ("tree", "base2", "policy", "0.1.0", "local", ["static", "commands"]),
        ("tree", "base", "policy2", "0.1.0", "local", ["static", "commands"]),
        ("tree", "base", "policy", "0.2.0", "local", ["static", "commands"]),
        ("tree", "base", "policy", "0.1.0", "ci", ["static", "commands"]),
        ("tree", "base", "policy", "0.1.0", "local", ["static"]),
    ]
    keys = {cache_key(*v) for v in variants}
    assert base not in keys
    assert len(keys) == len(variants)
    assert cache_key(*ARGS, flags=["allow-policy-change"]) != base


def test_hit_after_record_and_miss_before(tmp_path: Path) -> None:
    cache = PassCache(tmp_path / "greenproof")
    assert not cache.has_pass("k")
    cache.record_pass("k", CHECKS)
    assert cache.has_pass("k")
    assert not cache.has_pass("other")


def test_keeps_only_the_last_entries(tmp_path: Path) -> None:
    cache = PassCache(tmp_path)
    for i in range(MAX_ENTRIES + 10):
        cache.record_pass(f"key{i}", CHECKS)
    entries = json.loads((tmp_path / "cache.json").read_text())["entries"]
    assert len(entries) == MAX_ENTRIES
    assert not cache.has_pass("key0")
    assert cache.has_pass(f"key{MAX_ENTRIES + 9}")


def test_recording_twice_does_not_duplicate(tmp_path: Path) -> None:
    cache = PassCache(tmp_path)
    cache.record_pass("k", CHECKS)
    cache.record_pass("k", CHECKS)
    assert len(json.loads((tmp_path / "cache.json").read_text())["entries"]) == 1


def test_corrupt_cache_is_a_miss_and_is_replaced(tmp_path: Path) -> None:
    (tmp_path / "cache.json").write_text("not json{")
    cache = PassCache(tmp_path)
    assert not cache.has_pass("k")
    cache.record_pass("k", CHECKS)
    assert cache.has_pass("k")


def test_wrong_shape_is_a_miss(tmp_path: Path) -> None:
    for body in ('{"entries": 5}', "[]", '{"entries": [1, {"key": 2}]}'):
        (tmp_path / "cache.json").write_text(body)
        assert not PassCache(tmp_path).has_pass("k")


def test_statuses_are_stored_and_returned(tmp_path: Path) -> None:
    cache = PassCache(tmp_path)
    stored = [
        {"id": "static", "status": "pass", "duration_ms": 4},
        {"id": "revert", "status": "skipped", "duration_ms": 0},
    ]
    cache.record_pass("k", stored)
    assert cache.get_pass("k") == stored
    assert cache.get_pass("other") is None


def test_entry_without_statuses_is_a_miss(tmp_path: Path) -> None:
    (tmp_path / "cache.json").write_text('{"entries": [{"key": "k", "at": 1}]}')
    assert PassCache(tmp_path).get_pass("k") is None
