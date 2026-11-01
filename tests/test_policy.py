from __future__ import annotations

import hashlib

import pytest

from greenproof.errors import PolicyError, UsageError
from greenproof.findings import BLOCK
from greenproof.policy import load_policy, parse_policy, policy_changed_finding
from greenproof.registry import RULES, rule_ids

FULL = """
version = 1

[commands]
setup = "uv sync --frozen"
lint = "uv run ruff check ."
typecheck = "uv run mypy src"
test = "uv run pytest -q"
build = ""
timeout = 120

[tests]
paths = ["spec/**"]

[smoke]
start = "serve"
ready_url = "http://127.0.0.1:8765/health"
probes = ["curl -fsS http://127.0.0.1:8765/health"]
timeout = 30

[rules]
"tests.expected_changed" = "off"
"suppress.added" = "block"

[modes.hook]
checks = ["static"]
[modes.ci]
checks = ["static", "commands", "revert", "smoke"]
"""


def test_full_policy_parses() -> None:
    policy = parse_policy(FULL)
    assert policy.runnable_commands() == [
        ("lint", "uv run ruff check ."),
        ("typecheck", "uv run mypy src"),
        ("test", "uv run pytest -q"),
    ]
    assert policy.commands["setup"] == "uv sync --frozen"
    assert policy.timeout == 120
    assert policy.test_paths == ("spec/**",)
    assert policy.smoke is not None
    assert policy.smoke.timeout == 30
    assert policy.rules == {"tests.expected_changed": "off", "suppress.added": "block"}
    assert policy.mode_checks["hook"] == ("static",)
    assert policy.mode_checks["local"] == ("static", "commands")


def test_minimal_policy_uses_defaults() -> None:
    policy = parse_policy("version = 1\n")
    assert policy.runnable_commands() == []
    assert policy.timeout == 900
    assert policy.smoke is None
    assert policy.mode_checks["ci"] == ("static", "commands", "revert", "smoke")


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("", "version"),
        ("version = 2\n", "version"),
        ("version = true\n", "version"),
        ("version = 1\nbogus = 1\n", "bogus"),
        ('version = 1\n[commands]\ntset = "x"\n', "commands.tset"),
        ("version = 1\n[commands]\ntest = 5\n", "commands.test"),
        ('version = 1\n[commands]\ntimeout = "9"\n', "commands.timeout"),
        ("version = 1\n[commands]\ntimeout = 0\n", "commands.timeout"),
        ("version = 1\n[commands]\ntimeout = true\n", "commands.timeout"),
        ("version = 1\ncommands = 3\n", "commands"),
        ("version = 1\n[tests]\npaths = 'x'\n", "tests.paths"),
        ("version = 1\n[tests]\npaths = [1]\n", "tests.paths[0]"),
        ("version = 1\n[tests]\nglobs = []\n", "tests.globs"),
        ('version = 1\n[smoke]\nstart = "x"\n', "smoke.ready_url"),
        ('version = 1\n[smoke]\nstart = 1\nready_url = "u"\nprobes = []\n', "smoke.start"),
        (
            'version = 1\n[smoke]\nstart = "x"\nready_url = "u"\nprobes = []\ntimeout = -1\n',
            "smoke.timeout",
        ),
        ("version = 1\n[smoke]\nwait = 1\n", "smoke.wait"),
        ('version = 1\n[rules]\n"nope.rule" = "warn"\n', "rules.nope.rule"),
        ('version = 1\n[rules]\n"tests.deleted" = "loud"\n', "rules.tests.deleted"),
        ('version = 1\n[rules]\n"tests.deleted" = 1\n', "rules.tests.deleted"),
        ("version = 1\n[modes.nightly]\nchecks = []\n", "modes.nightly"),
        ("version = 1\n[modes.ci]\nchecks = ['lint']\n", "modes.ci.checks"),
        ("version = 1\n[modes.ci]\nextra = 1\n", "modes.ci.extra"),
        ("version = 1\n[modes]\nci = 1\n", "modes.ci"),
        ("version = [\n", "invalid TOML"),
    ],
)
def test_validation_names_the_key(text: str, key: str) -> None:
    with pytest.raises(PolicyError) as info:
        parse_policy(text)
    assert key in str(info.value)
    assert isinstance(info.value, UsageError)  # exit 4


def test_registry_has_every_spec_rule_id() -> None:
    expected = {
        "policy.changed",
        "tests.deleted",
        "tests.removed",
        "tests.replaced",
        "tests.skipped",
        "tests.focused",
        "tests.weakened",
        "tests.expected_changed",
        "suppress.added",
        "runner.tampered",
        "ci.tampered",
        "code.hardcoded",
        "revert.passes_without_change",
        "smoke.failed",
    }
    assert set(rule_ids()) == expected
    assert all(r.description and r.default_severity in ("block", "warn") for r in RULES.values())


# ---- base versus candidate -------------------------------------------------------------

BASE = b'version = 1\n[commands]\ntest = "pytest"\n'


def test_policy_comes_from_base_and_edit_is_detected() -> None:
    candidate = b'version = 1\n[commands]\ntest = "true"\n'
    loaded = load_policy(BASE, candidate)
    assert loaded.source == "base"
    assert loaded.policy.commands["test"] == "pytest"
    assert loaded.changed
    assert loaded.sha256 == hashlib.sha256(BASE).hexdigest()
    finding = policy_changed_finding(loaded)
    assert (finding.rule, finding.severity, finding.file) == (
        "policy.changed",
        BLOCK,
        ".greenproof.toml",
    )


def test_comment_and_formatting_changes_are_not_a_change() -> None:
    candidate = b'# a comment\nversion = 1\n\n[commands]\ntest   =   "pytest"\n'
    assert not load_policy(BASE, candidate).changed


def test_deleted_candidate_policy_is_a_change() -> None:
    loaded = load_policy(BASE, None)
    assert loaded.changed
    assert "deleted" in loaded.change_detail


def test_unparseable_candidate_policy_is_a_change_not_an_error() -> None:
    loaded = load_policy(BASE, b"version = [")
    assert loaded.changed
    assert loaded.policy.commands["test"] == "pytest"
    assert "no longer parses" in loaded.change_detail


def test_first_adoption_uses_the_candidate() -> None:
    loaded = load_policy(None, BASE)
    assert loaded.source == "candidate"
    assert not loaded.changed
    assert loaded.policy.commands["test"] == "pytest"


def test_no_policy_anywhere_is_an_error() -> None:
    with pytest.raises(PolicyError, match="not found"):
        load_policy(None, None)


def test_invalid_base_policy_is_an_error_naming_the_key() -> None:
    with pytest.raises(PolicyError, match=r"commands\.test"):
        load_policy(b"version = 1\n[commands]\ntest = 1\n", BASE)


def test_non_utf8_policy_is_an_error() -> None:
    with pytest.raises(PolicyError, match="UTF-8"):
        load_policy(b"\xff\xfe", None)
