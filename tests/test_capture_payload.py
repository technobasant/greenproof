"""scripts/e2e/capture_payload.py: the hook command that records real payloads (SPEC 15.5)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "e2e" / "capture_payload.py"

pytestmark = pytest.mark.skipif(
    not SCRIPT.exists(), reason="scripts/ is not in this source tree (the sdist does not carry it)"
)


def capture(
    stdin: bytes, *argv: str, target: Path | None, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[bytes]:
    env = dict(extra_env or {})  # nothing from the ambient environment, which may hold *_SESSION_ID
    if target is not None:
        env["GREENPROOF_CAPTURE"] = str(target)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *argv],
        input=stdin,
        capture_output=True,
        env=env,
        check=False,
    )


def test_appends_one_json_line_per_call_and_prints_nothing(tmp_path: Path) -> None:
    out = tmp_path / "payloads.jsonl"
    first = capture(b'{"session_id": "s1"}\n', "qwen", "Stop", target=out)
    second = capture(b'{"sessionId": "s2"}', "copilot", target=out)
    assert (first.returncode, first.stdout, first.stderr) == (0, b"", b"")
    assert (second.returncode, second.stdout, second.stderr) == (0, b"", b"")
    lines = out.read_text().splitlines()
    assert [json.loads(line)["argv"] for line in lines] == [["qwen", "Stop"], ["copilot"]]
    assert json.loads(lines[0])["stdin"] == '{"session_id": "s1"}\n'
    assert json.loads(lines[1])["stdin"] == '{"sessionId": "s2"}'


def test_records_only_the_project_dir_and_session_id_variables(tmp_path: Path) -> None:
    out = tmp_path / "payloads.jsonl"
    env = {
        "CLAUDE_PROJECT_DIR": "/repo",
        "FACTORY_PROJECT_DIR": "/repo2",
        "GEMINI_SESSION_ID": "g1",
        "PROJECT_DIR_NOT": "x",
        "SESSION_ID": "bare",
        "SECRET_TOKEN": "hunter2",
    }
    capture(b"{}", target=out, extra_env=env)
    recorded = json.loads(out.read_text())["env"]
    assert recorded == {
        "CLAUDE_PROJECT_DIR": "/repo",
        "FACTORY_PROJECT_DIR": "/repo2",
        "GEMINI_SESSION_ID": "g1",
    }


def test_keeps_the_payload_as_read_even_when_it_is_not_json_or_not_utf8(tmp_path: Path) -> None:
    out = tmp_path / "payloads.jsonl"
    capture(b"not json \xff end", target=out)
    capture(b"", target=out)
    first, second = (json.loads(line) for line in out.read_text().splitlines())
    assert first["stdin"] == "not json � end" and second["stdin"] == ""


def test_still_exits_0_when_it_cannot_record(tmp_path: Path) -> None:
    unset = capture(b"{}", target=None)
    assert (unset.returncode, unset.stdout) == (0, b"")
    assert b"GREENPROOF_CAPTURE" in unset.stderr
    bad = capture(b"{}", target=tmp_path / "missing-dir" / "x.jsonl")
    assert (bad.returncode, bad.stdout) == (0, b"")
    assert b"cannot write" in bad.stderr


def test_it_is_not_packaged() -> None:
    """The wheel is `src/greenproof` only and the sdist lists what it carries: not `scripts/`."""
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert 'packages = ["src/greenproof"]' in pyproject
    sdist = pyproject.split("[tool.hatch.build.targets.sdist]")[1].split("\n]\n")[0]
    assert "scripts" not in sdist
