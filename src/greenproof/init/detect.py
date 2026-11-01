"""Detect the stacks in a repository and propose `[commands]` from evidence only (SPEC 7.1-7.2).

Nothing is proposed for a tool the repository does not show evidence of using. Every proposed
command carries a human-readable source, written next to it in the policy.
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

COMMAND_SLOTS = ("setup", "lint", "typecheck", "test", "build")
_STACK_ORDER = ("python", "node", "go", "rust")
_TASK_FILES = ("Makefile", "GNUmakefile", "justfile")
_NPM_PLACEHOLDER = 'echo "Error: no test specified" && exit 1'
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


@dataclass(frozen=True)
class ProposedCommand:
    command: str
    source: str


@dataclass(frozen=True)
class Detection:
    stacks: tuple[str, ...] = ()
    python_runner: str = ""  # "uv", "poetry" or "" (plain python)
    node_pm: str = ""  # "npm", "pnpm", "yarn" or "bun" when Node was detected
    targets: dict[str, ProposedCommand] = field(default_factory=dict)

    @property
    def python_prefix(self) -> str:
        return {"uv": "uv run ", "poetry": "poetry run "}.get(self.python_runner, "")

    def describe(self) -> str:
        parts = []
        for stack in self.stacks:
            if stack == "python":
                parts.append(f"python ({self.python_runner or 'pip'})")
            elif stack == "node":
                parts.append(f"node ({self.node_pm})")
            else:
                parts.append(stack)
        return ", ".join(parts)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(_read_text(path))
    except tomllib.TOMLDecodeError:
        return {}


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(_read_text(path))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _requirement_name(spec: str) -> str | None:
    match = _NAME.match(spec.strip())
    return match.group(0).lower().replace("_", "-") if match else None


def _requirement_files(root: Path) -> list[Path]:
    return sorted(p for p in root.glob("requirements*.txt") if p.is_file())


def _pyproject_dependency_names(pyproject: dict[str, Any]) -> set[str]:
    """Names in project dependencies, optional-dependencies, dependency-groups and the
    uv/poetry dependency tables, which are all dependencies named in pyproject."""
    specs: list[Any] = []
    project = pyproject.get("project", {})
    if isinstance(project, dict):
        specs += list(project.get("dependencies") or [])
        optional = project.get("optional-dependencies") or {}
        if isinstance(optional, dict):
            for group in optional.values():
                specs += list(group) if isinstance(group, list) else []
    groups = pyproject.get("dependency-groups") or {}
    if isinstance(groups, dict):
        for group in groups.values():
            specs += list(group) if isinstance(group, list) else []
    tool = pyproject.get("tool", {})
    names: set[str] = set()
    if isinstance(tool, dict):
        uv = tool.get("uv", {})
        if isinstance(uv, dict):
            specs += list(uv.get("dev-dependencies") or [])
        poetry = tool.get("poetry", {})
        if isinstance(poetry, dict):
            tables = [poetry.get("dependencies"), poetry.get("dev-dependencies")]
            poetry_groups = poetry.get("group")
            if isinstance(poetry_groups, dict):
                tables += [
                    g.get("dependencies") for g in poetry_groups.values() if isinstance(g, dict)
                ]
            for table in tables:
                if isinstance(table, dict):
                    names.update(str(k).lower().replace("_", "-") for k in table)
    for spec in specs:
        if isinstance(spec, str) and (name := _requirement_name(spec)):
            names.add(name)
    return names


def _requirements_names(root: Path) -> dict[str, str]:
    """Package name -> the requirements file that names it."""
    found: dict[str, str] = {}
    for path in _requirement_files(root):
        for line in _read_text(path).splitlines():
            line = line.strip()
            if not line or line.startswith(("#", "-")):
                continue
            if name := _requirement_name(line):
                found.setdefault(name, path.name)
    return found


class _Python:
    """Evidence lookups for the Python tools, over one read of the project files."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.pyproject = _read_toml(root / "pyproject.toml")
        self.deps = _pyproject_dependency_names(self.pyproject)
        self.requirements = _requirements_names(root)

    def _tool_table(self, name: str) -> bool:
        tool = self.pyproject.get("tool", {})
        return isinstance(tool, dict) and name in tool

    def _named(self, name: str) -> str | None:
        if name in self.deps:
            return f"{name} is a dependency in pyproject.toml"
        if name in self.requirements:
            return f"{name} is listed in {self.requirements[name]}"
        return None

    def pytest(self) -> str | None:
        tool = self.pyproject.get("tool", {})
        pytest_table = tool.get("pytest") if isinstance(tool, dict) else None
        if isinstance(pytest_table, dict) and "ini_options" in pytest_table:
            return "[tool.pytest.ini_options] in pyproject.toml"
        if (self.root / "pytest.ini").is_file():
            return "pytest.ini"
        for rel in ("conftest.py", "tests/conftest.py", "test/conftest.py"):
            if (self.root / rel).is_file():
                return rel
        return self._named("pytest")

    def ruff(self) -> str | None:
        if self._tool_table("ruff"):
            return "[tool.ruff] in pyproject.toml"
        for name in ("ruff.toml", ".ruff.toml"):
            if (self.root / name).is_file():
                return name
        return self._named("ruff")

    def mypy(self) -> str | None:
        if self._tool_table("mypy"):
            return "[tool.mypy] in pyproject.toml"
        for name in ("mypy.ini", ".mypy.ini"):
            if (self.root / name).is_file():
                return name
        return self._named("mypy")

    def pyright(self) -> str | None:
        if self._tool_table("pyright"):
            return "[tool.pyright] in pyproject.toml"
        if (self.root / "pyrightconfig.json").is_file():
            return "pyrightconfig.json"
        return None


def _make_targets(text: str) -> set[str]:
    names: set[str] = set()
    for line in text.splitlines():
        if not line or line[0] in " \t#.":
            continue
        head, sep, rest = line.partition(":")
        if not sep or rest.startswith("="):
            continue
        names.update(head.split())
    return names


def _just_targets(text: str) -> set[str]:
    names: set[str] = set()
    for line in text.splitlines():
        if not line or line[0] in " \t#":
            continue
        head, sep, rest = line.partition(":")
        tokens = head.split()
        if sep and not rest.startswith("=") and tokens:
            names.add(tokens[0])
    return names


def _task_targets(root: Path) -> dict[str, ProposedCommand]:
    """First make/just file that defines each of test, lint, check and typecheck."""
    found: dict[str, ProposedCommand] = {}
    for filename in _TASK_FILES:
        path = root / filename
        if not path.is_file():
            continue
        text = _read_text(path)
        runner = "just" if filename == "justfile" else "make"
        names = _just_targets(text) if runner == "just" else _make_targets(text)
        for target in ("test", "lint", "check", "typecheck"):
            if target in names and target not in found:
                found[target] = ProposedCommand(
                    f"{runner} {target}", f'the {filename} target "{target}"'
                )
    return found


def detect(root: Path) -> Detection:
    stacks: list[str] = []
    python_runner = ""
    node_pm = ""
    if any(
        (root / name).is_file() for name in ("pyproject.toml", "setup.py", "setup.cfg")
    ) or _requirement_files(root):
        stacks.append("python")
        if (root / "uv.lock").is_file():
            python_runner = "uv"
        elif (root / "poetry.lock").is_file():
            python_runner = "poetry"
    if (root / "package.json").is_file():
        stacks.append("node")
        if (root / "pnpm-lock.yaml").is_file():
            node_pm = "pnpm"
        elif (root / "yarn.lock").is_file():
            node_pm = "yarn"
        elif (root / "bun.lock").is_file() or (root / "bun.lockb").is_file():
            node_pm = "bun"
        else:
            node_pm = "npm"
    if (root / "go.mod").is_file():
        stacks.append("go")
    if (root / "Cargo.toml").is_file():
        stacks.append("rust")
    return Detection(tuple(stacks), python_runner, node_pm, _task_targets(root))


def _node_run(pm: str, script: str) -> str:
    return f"{pm} run {script}"


def _python_commands(root: Path, det: Detection) -> dict[str, ProposedCommand]:
    py = _Python(root)
    prefix = det.python_prefix or "python -m "
    out: dict[str, ProposedCommand] = {}
    if evidence := py.pytest():
        out["test"] = ProposedCommand(f"{prefix}pytest -q", evidence)
    if evidence := py.ruff():
        out["lint"] = ProposedCommand(f"{prefix}ruff check .", evidence)
    if evidence := py.mypy():
        out["typecheck"] = ProposedCommand(f"{prefix}mypy .", evidence)
    elif evidence := py.pyright():
        out["typecheck"] = ProposedCommand(f"{prefix}pyright", evidence)
    if det.python_runner == "uv":
        out["setup"] = ProposedCommand("uv sync --frozen", "uv.lock")
    elif det.python_runner == "poetry":
        out["setup"] = ProposedCommand("poetry install", "poetry.lock")
    elif (root / "requirements.txt").is_file():
        out["setup"] = ProposedCommand(
            "python -m pip install -r requirements.txt", "requirements.txt"
        )
    return out


def _node_commands(root: Path, det: Detection) -> dict[str, ProposedCommand]:
    pm = det.node_pm
    scripts = _read_json(root / "package.json").get("scripts", {})
    scripts = scripts if isinstance(scripts, dict) else {}
    out: dict[str, ProposedCommand] = {}

    def script(name: str) -> str | None:
        value = scripts.get(name)
        return value if isinstance(value, str) and value.strip() else None

    test = script("test")
    if test is not None and test.strip() != _NPM_PLACEHOLDER:
        command = "bun run test" if pm == "bun" else f"{pm} test"
        out["test"] = ProposedCommand(command, 'package.json scripts "test"')
    if script("lint") is not None:
        out["lint"] = ProposedCommand(_node_run(pm, "lint"), 'package.json scripts "lint"')
    for name in ("typecheck", "type-check"):
        if script(name) is not None:
            out["typecheck"] = ProposedCommand(
                _node_run(pm, name), f'package.json scripts "{name}"'
            )
            break
    if script("build") is not None:
        out["build"] = ProposedCommand(_node_run(pm, "build"), 'package.json scripts "build"')
    if pm == "pnpm":
        out["setup"] = ProposedCommand("pnpm install --frozen-lockfile", "pnpm-lock.yaml")
    elif pm == "yarn":
        modern = (root / ".yarnrc.yml").is_file()
        out["setup"] = ProposedCommand(
            "yarn install --immutable" if modern else "yarn install --frozen-lockfile",
            "yarn.lock",
        )
    elif pm == "bun":
        lock = "bun.lock" if (root / "bun.lock").is_file() else "bun.lockb"
        out["setup"] = ProposedCommand("bun install --frozen-lockfile", lock)
    else:
        for lock in ("package-lock.json", "npm-shrinkwrap.json"):
            if (root / lock).is_file():
                out["setup"] = ProposedCommand("npm ci", lock)
                break
    return out


def _go_commands(root: Path) -> dict[str, ProposedCommand]:
    out = {
        "test": ProposedCommand("go test ./...", "go.mod"),
        "setup": ProposedCommand("go mod download", "go.mod"),
    }
    for name in (".golangci.yml", ".golangci.yaml", ".golangci.toml"):
        if (root / name).is_file():
            out["lint"] = ProposedCommand("golangci-lint run", name)
            break
    else:
        out["lint"] = ProposedCommand("go vet ./...", "go.mod")
    return out


def _rust_commands(root: Path) -> dict[str, ProposedCommand]:
    out = {
        "test": ProposedCommand("cargo test", "Cargo.toml"),
        "setup": ProposedCommand("cargo fetch", "Cargo.toml"),
    }
    for name in ("clippy.toml", ".clippy.toml"):
        if (root / name).is_file():
            out["lint"] = ProposedCommand("cargo clippy --all-targets", name)
            break
    return out


def propose(root: Path, det: Detection) -> dict[str, ProposedCommand]:
    """Commands per slot, in policy order. A make/just target replaces the language-derived
    command of its slot; several stacks filling one slot are joined with ` && `."""
    per_stack: dict[str, dict[str, ProposedCommand]] = {}
    if "python" in det.stacks:
        per_stack["python"] = _python_commands(root, det)
    if "node" in det.stacks:
        per_stack["node"] = _node_commands(root, det)
    if "go" in det.stacks:
        per_stack["go"] = _go_commands(root)
    if "rust" in det.stacks:
        per_stack["rust"] = _rust_commands(root)

    result: dict[str, ProposedCommand] = {}
    for slot in COMMAND_SLOTS:
        target = det.targets.get(slot)
        if slot == "lint" and target is None:
            target = det.targets.get("check")
        if target is not None and slot != "setup":
            result[slot] = target
            continue
        parts = [
            per_stack[s][slot] for s in _STACK_ORDER if s in per_stack and slot in per_stack[s]
        ]
        if parts:
            result[slot] = ProposedCommand(
                " && ".join(p.command for p in parts), "; ".join(p.source for p in parts)
            )
    return result
