"""The GitHub Actions workflow written by `init --ci github` (SPEC 6.5 and 7.7).

It does not use greenproof's own action, so it needs no release SHA of greenproof. Every
third-party action comes from `pins.py`.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from greenproof import pins
from greenproof.init.detect import Detection
from greenproof.policy import Policy

WORKFLOW_PATH = ".github/workflows/greenproof.yml"
_TEST_EXTRAS = ("test", "tests", "testing", "dev")
_REQUIREMENT_FILES = (
    "requirements.txt",
    "requirements-dev.txt",
    "requirements-test.txt",
    "requirements-tests.txt",
    "dev-requirements.txt",
    "test-requirements.txt",
)
_PYTHON_TOOL = re.compile(r"\bpython -m (pytest|ruff|mypy|pyright)\b")


def _step(pin: pins.Pin, with_lines: list[str] | None = None) -> list[str]:
    lines = [f"      - uses: {pin.uses()}"]
    if with_lines:
        lines.append("        with:")
        lines += [f"          {line}" for line in with_lines]
    return lines


def _toolchain_steps(root: Path, det: Detection) -> list[str]:
    steps: list[str] = []
    if "python" in det.stacks and det.python_runner != "uv":
        if (root / ".python-version").is_file():
            with_lines = ["python-version-file: .python-version"]
        else:
            with_lines = ['python-version: "3.x"']
        steps += _step(pins.SETUP_PYTHON, with_lines)
    if "node" in det.stacks:
        if det.node_pm == "bun":
            steps += _step(pins.SETUP_BUN)
        else:
            for name in (".nvmrc", ".node-version"):
                if (root / name).is_file():
                    with_lines = [f"node-version-file: {name}"]
                    break
            else:
                with_lines = ["node-version: lts/*"]
            steps += _step(pins.SETUP_NODE, with_lines)
            if det.node_pm in ("pnpm", "yarn"):
                steps += ["      - run: corepack enable"]
    if "go" in det.stacks:
        steps += _step(pins.SETUP_GO, ["go-version-file: go.mod"])
    return steps


def _pip_install_parts(root: Path, policy: Policy) -> list[str]:
    """What a plain-pip project needs installed to run the policy's commands.

    Only the workflow installs these. `setup` also runs in local `--isolate` worktrees, where
    `pip install -e .` would re-point the user's own environment at a temporary directory.
    """
    parts: list[str] = []
    try:
        pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        pyproject = {}
    project = pyproject.get("project")
    extras = project.get("optional-dependencies") if isinstance(project, dict) else None
    extra = next((e for e in _TEST_EXTRAS if isinstance(extras, dict) and e in extras), None)
    if extra is not None:
        parts.append(f'-e ".[{extra}]"')
    elif isinstance(project, dict) or any(
        (root / name).is_file() for name in ("setup.py", "setup.cfg")
    ):
        parts.append("-e .")
    parts += [f"-r {name}" for name in _REQUIREMENT_FILES if (root / name).is_file()]
    for _name, command in policy.runnable_commands():
        for tool in _PYTHON_TOOL.findall(command):
            if tool not in parts:
                parts.append(tool)
    return parts


def _install_steps(root: Path, det: Detection, policy: Policy) -> tuple[list[str], bool]:
    """Install steps for the stacks whose `setup` init could not propose (see _pip_install_parts),
    and whether the run step needs `PYTHONPATH: src`. No steps when the policy has a `setup`
    command, which greenproof runs itself."""
    if policy.commands.get("setup", "").strip():
        return [], False
    steps: list[str] = []
    src_path = False
    if "python" in det.stacks and det.python_runner == "":
        parts = _pip_install_parts(root, policy)
        # An editable install of a src-layout project points at the head checkout's src/, which
        # the revert check's base worktree would import instead of its own.
        src_path = (root / "src").is_dir() and any(p.startswith("-e ") for p in parts)
        if parts:
            steps += [
                "      - name: Install Python dependencies",
                "        run: >-",
                f"          python -m pip install {' '.join(parts)}",
            ]
    if (
        "node" in det.stacks
        and det.node_pm == "npm"
        and not any((root / n).is_file() for n in ("package-lock.json", "npm-shrinkwrap.json"))
    ):
        steps += ["      - name: Install npm dependencies", "        run: npm install"]
    return steps, src_path


def render_workflow(root: Path, det: Detection, version: str, policy: Policy) -> str:
    install, src_path = _install_steps(root, det, policy)
    lines = [
        "name: greenproof",
        "",
        "# Written by `greenproof init --ci github`. Runs on pull requests only: a workflow",
        "# triggered by comments would run with base-branch privileges, a known injection path.",
        "on:",
        "  pull_request:",
        "",
        "permissions:",
        "  contents: read",
        "  pull-requests: read",
        "",
        "jobs:",
        "  greenproof:",
        "    name: greenproof",
        "    runs-on: ubuntu-latest",
        "    steps:",
        *_step(pins.CHECKOUT, ["fetch-depth: 0", "persist-credentials: false"]),
        *_toolchain_steps(root, det),
        *install,
        *_step(pins.SETUP_UV),
        "      - name: Run greenproof",
        "        env:",
        "          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}",
        *(
            [
                "          # The revert check's base worktree must import its own src/, not the",
                "          # editable install of this checkout.",
                "          PYTHONPATH: src",
            ]
            if src_path
            else []
        ),
        "        run: >-",
        f"          uvx greenproof@{version} check --mode ci --github-annotations",
        '          --github-accept --markdown "$GITHUB_STEP_SUMMARY" --json greenproof.json',
        f"      - uses: {pins.UPLOAD_ARTIFACT.uses()}",
        "        if: always()",
        "        with:",
        "          name: greenproof-report",
        "          path: greenproof.json",
    ]
    return "\n".join(lines) + "\n"
