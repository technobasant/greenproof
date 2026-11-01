"""The GitHub Actions workflow written by `init --ci github` (SPEC 6.5 and 7.7).

It does not use greenproof's own action, so it needs no release SHA of greenproof. Every
third-party action comes from `pins.py`.
"""

from __future__ import annotations

from pathlib import Path

from greenproof import pins
from greenproof.init.detect import Detection

WORKFLOW_PATH = ".github/workflows/greenproof.yml"


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


def render_workflow(root: Path, det: Detection, version: str) -> str:
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
        *_step(pins.SETUP_UV),
        "      - name: Run greenproof",
        "        env:",
        "          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}",
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
