"""init --ci github, pins.py and action.yml."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from greenproof import __version__, pins
from tests.init_helpers import fixture_repo

pytestmark = pytest.mark.usefixtures("controlled_path")

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ".github/workflows/greenproof.yml"
SHA = re.compile(r"^[0-9a-f]{40}$")
USES_LINE = re.compile(r"^\s*-?\s*uses:\s*(\S+)@(\S+)\s+#\s*(\S+)\s*$", re.MULTILINE)
PIN_BY_REPO = {p.repo: p for p in pins.ALL_PINS}

UV = {"pyproject.toml": "[tool.pytest.ini_options]\n", "uv.lock": ""}


def generate(
    tmp_path: Path, run, files: dict[str, str], name: str = "repo"
) -> tuple[str, dict[str, Any]]:
    repo = fixture_repo(tmp_path, files, name=name)
    result = run(repo.path, "init", "--no-verify", "--agents", "none", "--ci", "github")
    assert result.code == 0, result.err
    text = (repo.path / WORKFLOW).read_text()
    return text, yaml.safe_load(text)


def steps(doc: dict[str, Any]) -> list[dict[str, Any]]:
    return doc["jobs"]["greenproof"]["steps"]


def used(doc: dict[str, Any]) -> list[str]:
    return [s["uses"].split("@")[0] for s in steps(doc) if "uses" in s]


def test_uv_workflow_shape(tmp_path: Path, run) -> None:
    text, doc = generate(tmp_path, run, UV)
    assert doc[True] == {"pull_request": None}  # PyYAML reads the key `on` as True
    assert doc["permissions"] == {"contents": "read", "pull-requests": "read"}
    assert list(doc["jobs"]) == ["greenproof"]
    assert used(doc) == [
        "actions/checkout",
        "astral-sh/setup-uv",
        "actions/upload-artifact",
    ]
    checkout = steps(doc)[0]
    assert checkout["with"] == {"fetch-depth": 0, "persist-credentials": False}
    run_step = steps(doc)[2]
    assert run_step["env"] == {"GITHUB_TOKEN": "${{ secrets.GITHUB_TOKEN }}"}
    assert run_step["run"] == (
        f"uvx greenproof@{__version__} check --mode ci --github-annotations "
        '--github-accept --markdown "$GITHUB_STEP_SUMMARY" --json greenproof.json'
    )
    upload = steps(doc)[3]
    assert upload["if"] == "always()"
    assert upload["with"] == {"name": "greenproof-report", "path": "greenproof.json"}
    # No `${{ }}` interpolation inside any run: script.
    assert all("${{" not in s.get("run", "") for s in steps(doc))
    assert "issue_comment" not in text


def test_every_action_in_the_workflow_is_a_full_sha_matching_pins(tmp_path: Path, run) -> None:
    text, doc = generate(
        tmp_path,
        run,
        {"pyproject.toml": "", "package.json": "{}", "go.mod": "module x\n", "bun.lockb": ""},
    )
    found = USES_LINE.findall(text)
    assert len(found) == len(used(doc))
    for repo_name, sha, tag in found:
        assert SHA.match(sha)
        assert PIN_BY_REPO[repo_name].sha == sha
        assert PIN_BY_REPO[repo_name].tag == tag


@pytest.mark.parametrize(
    ("files", "toolchain", "with_"),
    [
        ({"requirements.txt": "pytest\n"}, ["actions/setup-python"], {"python-version": "3.x"}),
        (
            {"requirements.txt": "pytest\n", ".python-version": "3.12\n"},
            ["actions/setup-python"],
            {"python-version-file": ".python-version"},
        ),
        (
            {"pyproject.toml": "", "poetry.lock": ""},
            ["actions/setup-python"],
            {"python-version": "3.x"},
        ),
        ({"package.json": "{}"}, ["actions/setup-node"], {"node-version": "lts/*"}),
        (
            {"package.json": "{}", ".nvmrc": "20\n"},
            ["actions/setup-node"],
            {"node-version-file": ".nvmrc"},
        ),
        (
            {"package.json": "{}", ".node-version": "20\n", "package-lock.json": "{}"},
            ["actions/setup-node"],
            {"node-version-file": ".node-version"},
        ),
        ({"go.mod": "module x\n"}, ["actions/setup-go"], {"go-version-file": "go.mod"}),
        ({"package.json": "{}", "bun.lock": ""}, ["oven-sh/setup-bun"], None),
        ({"Cargo.toml": "[package]\n"}, [], None),
        ({"notes.txt": "x"}, [], None),
    ],
)
def test_toolchain_steps_by_stack(files, toolchain, with_, tmp_path: Path, run) -> None:
    _, doc = generate(tmp_path, run, files)
    assert used(doc) == [
        "actions/checkout",
        *toolchain,
        "astral-sh/setup-uv",
        "actions/upload-artifact",
    ]
    if with_ is not None:
        assert steps(doc)[1]["with"] == with_


@pytest.mark.parametrize("pm_file", ["pnpm-lock.yaml", "yarn.lock"])
def test_corepack_is_enabled_for_pnpm_and_yarn(pm_file: str, tmp_path: Path, run) -> None:
    _, doc = generate(tmp_path, run, {"package.json": "{}", pm_file: ""})
    assert steps(doc)[2] == {"run": "corepack enable"}


def test_mixed_workflow_has_every_toolchain_in_order(tmp_path: Path, run) -> None:
    _, doc = generate(
        tmp_path,
        run,
        {
            "pyproject.toml": "",
            "requirements.txt": "x\n",
            "package.json": "{}",
            "pnpm-lock.yaml": "",
            "go.mod": "module x\n",
            "Cargo.toml": "[package]\n",
        },
    )
    assert used(doc) == [
        "actions/checkout",
        "actions/setup-python",
        "actions/setup-node",
        "actions/setup-go",
        "astral-sh/setup-uv",
        "actions/upload-artifact",
    ]
    assert {"run": "corepack enable"} in steps(doc)


def test_existing_workflow_is_kept_unless_yes(tmp_path: Path, run) -> None:
    repo = fixture_repo(tmp_path, {**UV, WORKFLOW: "name: mine\n"})
    result = run(repo.path, "init", "--no-verify", "--agents", "none", "--ci", "github")
    assert (repo.path / WORKFLOW).read_text() == "name: mine\n"
    assert re.search(rf"kept\s+{re.escape(WORKFLOW)}", result.out)
    run(repo.path, "init", "--no-verify", "--agents", "none", "--ci", "github", "--yes")
    assert (repo.path / WORKFLOW).read_text().startswith("name: greenproof")


def test_no_workflow_without_ci_flag(tmp_path: Path, run) -> None:
    repo = fixture_repo(tmp_path, UV)
    run(repo.path, "init", "--no-verify", "--agents", "none")
    assert not (repo.path / ".github").exists()


# ---- pins.py and action.yml ---------------------------------------------------------------


def test_pins_are_full_commit_shas_with_release_tags() -> None:
    assert {p.repo for p in pins.ALL_PINS} == {
        "actions/checkout",
        "astral-sh/setup-uv",
        "actions/upload-artifact",
        "actions/setup-python",
        "actions/setup-node",
        "actions/setup-go",
        "oven-sh/setup-bun",
    }
    for pin in pins.ALL_PINS:
        assert SHA.match(pin.sha), pin
        assert re.match(r"^v\d+\.\d+\.\d+$", pin.tag), pin
        assert pin.uses() == f"{pin.repo}@{pin.sha} # {pin.tag}"


def test_checkout_and_setup_uv_pins_match_this_repositorys_own_ci() -> None:
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    for repo_name, sha, tag in USES_LINE.findall(ci):
        assert (PIN_BY_REPO[repo_name].sha, PIN_BY_REPO[repo_name].tag) == (sha, tag)


def load_action() -> tuple[str, dict[str, Any]]:
    text = (ROOT / "action.yml").read_text()
    return text, yaml.safe_load(text)


def test_every_uses_sha_in_action_yml_equals_the_pin() -> None:
    text, doc = load_action()
    found = USES_LINE.findall(text)
    declared = [s["uses"] for s in doc["runs"]["steps"] if "uses" in s]
    assert len(found) == len(declared) == 2
    for repo_name, sha, tag in found:
        assert SHA.match(sha)
        assert PIN_BY_REPO[repo_name].sha == sha
        assert PIN_BY_REPO[repo_name].tag == tag


def test_action_yml_inputs_outputs_and_script_hygiene() -> None:
    text, doc = load_action()
    assert doc["runs"]["using"] == "composite"
    inputs = doc["inputs"]
    assert set(inputs) == {"base", "checks", "allow-policy-change", "accept-comments", "token"}
    assert inputs["allow-policy-change"]["default"] == "false"
    assert inputs["accept-comments"]["default"] == "true"
    assert inputs["token"]["default"] == "${{ github.token }}"
    assert set(doc["outputs"]) == {"exit-code"}
    for step in doc["runs"]["steps"]:
        if "run" in step:
            assert "${{" not in step["run"], "inputs must reach run: only through env:"
            assert step["shell"] == "bash"
    run_step = next(s for s in doc["runs"]["steps"] if s.get("id") == "greenproof")
    assert run_step["env"]["INPUT_BASE"] == "${{ inputs.base }}"
    script = run_step["run"]
    assert 'uvx --from "$GITHUB_ACTION_PATH" greenproof' in script
    for flag in (
        "--mode ci",
        "--github-annotations",
        '--markdown "$GITHUB_STEP_SUMMARY"',
        "--json greenproof.json",
    ):
        assert flag in script
    assert "--github-accept" in script
    upload = next(s for s in doc["runs"]["steps"] if "upload-artifact" in s.get("uses", ""))
    assert upload["if"] == "always()"
    assert "git fetch --no-tags --unshallow origin" in text


# ---- linters, when installed ----------------------------------------------------------------


def _lint(tool: str, args: list[str]) -> None:
    exe = shutil.which(tool)
    if exe is None:
        pytest.skip(f"{tool} is not installed")
    proc = subprocess.run([exe, *args], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def _all_stacks_workflow(tmp_path: Path, run) -> Path:
    generate(
        tmp_path,
        run,
        {"pyproject.toml": "", "package.json": "{}", "pnpm-lock.yaml": "", "go.mod": "module x\n"},
    )
    return tmp_path / "repo" / WORKFLOW


def test_generated_workflow_passes_actionlint(tmp_path: Path, run, monkeypatch) -> None:
    actionlint = shutil.which("actionlint")  # looked up before PATH is narrowed
    if actionlint is None:
        pytest.skip("actionlint is not installed")
    workflow = _all_stacks_workflow(tmp_path, run)
    proc = subprocess.run([actionlint, str(workflow)], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_generated_workflow_and_action_pass_zizmor(tmp_path: Path, run) -> None:
    zizmor = shutil.which("zizmor")
    if zizmor is None:
        pytest.skip("zizmor is not installed")
    workflow = _all_stacks_workflow(tmp_path, run)
    for target in (workflow, ROOT / "action.yml"):
        proc = subprocess.run(
            [zizmor, "--offline", "--persona=regular", str(target)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
