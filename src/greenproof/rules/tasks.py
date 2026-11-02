"""Task runners read into the commands they run: `concurrently "a" "b"`, `turbo run test`,
`nx run-many -t test`, `nx run app:test`, `nx affected -t test` and `lerna run test`.

A step of the base that became one of these has not lost its checks when the commands behind it
are the same checks, and has lost them when they are `echo skip`. `concurrently` runs each of
its quoted arguments (`npm:<script>` is that package script). The others run script (target) X in
every workspace package: the packages are those of `package.json` `workspaces`,
`pnpm-workspace.yaml`, `lerna.json` or the nx `project.json` files of the tree, and a tool that
runs without any of them runs the root package. A selection by name (`--filter`, `--scope`,
`-p`), a package that cannot be listed or a tree past the budget is `Opaque`: the caller warns
and does not block.
"""

from __future__ import annotations

import json
import posixpath
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from greenproof.changeset import _glob_to_regex
from greenproof.rules.shell import command_tokens, split_segments

_MAX_PACKAGES = 300
_MAX_LINES = 600
_PM = frozenset({"npm", "pnpm", "yarn", "bun"})
_NODE_NAMES = {"npm", "pnpm", "yarn", "bun"}
_CONCURRENTLY_VALUES = frozenset(
    {"-n", "--names", "-c", "--prefix-colors", "-p", "--prefix", "-m", "--max-processes"}
    | {"--restart-tries", "--restart-after", "-s", "--success", "-t", "--timestamp-format"}
    | {"-l", "--prefix-length", "--kill-signal", "--handle-input", "--default-input-target"}
    | {"--teardown", "--hide", "--passthrough-arguments"}
)
_TURBO_VALUES = frozenset(
    {"--filter", "-F", "--cache-dir", "--concurrency", "--env-mode", "--output-logs", "--log-order"}
    | {"--cwd", "--root-turbo-json", "--summarize", "--token", "--team", "--api", "--login"}
)
_TURBO_SELECT = frozenset({"--filter", "-F", "--affected", "--only"})
_LERNA_VALUES = frozenset(
    {"--scope", "--ignore", "--since", "--concurrency", "--log-level", "--loglevel"}
    | {"--npm-client", "--profile-location", "--ci", "--include-dependencies"}
)
_LERNA_SELECT = ("--scope", "--ignore", "--since", "--include-", "--exclude-", "--no-private")
_NX_SELECT = frozenset({"-p", "--projects", "--exclude", "--exclude-task-dependencies", "--base"})
_NX_VALUES = _NX_SELECT | {"-t", "--target", "--targets", "-c", "--configuration", "--parallel"}
_NX_VALUES = _NX_VALUES | {
    "--head",
    "--base",
    "--output-style",
    "--skip-nx-cache",
    "--max-parallel",
}
_NX_COMMANDS = frozenset({"run-many", "affected", "run", "exec", "graph", "show", "reset"})
# nx executors and the command each one runs, by what they are.
_NX_EXECUTORS = {
    "jest": "jest",
    "vite:test": "vitest",
    "vitest": "vitest",
    "eslint:lint": "eslint .",
    "linter": "eslint .",
    "cypress": "cypress run",
    "playwright": "playwright test",
    "mocha": "mocha",
    "karma": "karma start",
    "angular:test": "ng test",
}


class Opaque(str):
    """A task runner statement whose commands could not be read, with the reason."""


Reader = Callable[[str], "list[str] | Opaque | None"]


def _script_lines(scripts: Mapping[str, Any], name: str) -> list[str]:
    return [
        b for key in (f"pre{name}", name, f"post{name}") if isinstance(b := scripts.get(key), str)
    ]


def _dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _json(text: str | None) -> dict[str, Any]:
    try:
        data = json.loads(text) if text else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _yaml_packages(text: str) -> list[str]:
    """The `packages:` list of a `pnpm-workspace.yaml`."""
    out: list[str] = []
    inside = False
    for raw in text.splitlines():
        line = re.sub(r"\s+#.*$", "", raw).rstrip()
        if re.match(r"^packages\s*:", line):
            inside = True
            rest = line.partition(":")[2].strip()
            if rest.startswith("["):
                return [w.strip("'\" ") for w in rest.strip("[]").split(",") if w.strip("'\" ")]
        elif inside and re.match(r"^\s*-\s*", line):
            out.append(re.sub(r"^\s*-\s*", "", line).strip("'\" "))
        elif inside and line and not line.startswith((" ", "\t")):
            break
    return out


class Workspace:
    """The packages under a root directory of one tree (base or candidate)."""

    def __init__(
        self,
        root: str,
        read: Callable[[str], str | None],
        paths: Callable[[], Sequence[str]] | None,
    ) -> None:
        self.root = root
        self._read = read
        self._paths = paths
        self._cache: dict[str, Any] = {}

    def _file(self, name: str) -> str | None:
        return self._read(posixpath.normpath(posixpath.join(self.root, name)))

    def root_package(self) -> dict[str, Any]:
        return _json(self._file("package.json"))

    def _patterns(self, tool: str) -> list[str] | None:
        """The package globs the tool's own config names, else the package manager's."""
        if tool == "lerna":
            lerna = _json(self._file("lerna.json"))
            if isinstance(lerna.get("packages"), list) and not lerna.get("useWorkspaces"):
                return [str(p) for p in lerna["packages"]]
        yaml = self._file("pnpm-workspace.yaml")
        if yaml is not None and (found := _yaml_packages(yaml)):
            return found
        ws = self.root_package().get("workspaces")
        if isinstance(ws, dict):
            ws = ws.get("packages")
        if isinstance(ws, list) and ws:
            return [str(p) for p in ws]
        if tool == "lerna" and self._file("lerna.json") is not None:
            return ["packages/*"]
        return None

    def packages(self, tool: str) -> dict[str, dict[str, Any]] | None:
        """`{directory: package.json}` of the workspace packages, None when none is declared;
        raises LookupError when they cannot be listed."""
        if tool in self._cache:
            cached: dict[str, dict[str, Any]] | None = self._cache[tool]
            return cached
        patterns = self._patterns(tool)
        found: dict[str, dict[str, Any]] | None = None
        if patterns is not None:
            if self._paths is None:
                raise LookupError("the file tree is not available")
            wanted = [p for p in patterns if not p.startswith("!")]
            unwanted = [p.removeprefix("!") for p in patterns if p.startswith("!")]
            found = {}
            for path in self._paths():
                if posixpath.basename(path) != "package.json" or path == "package.json":
                    continue
                rel = self._rel(posixpath.dirname(path))
                if rel is None or "node_modules" in rel.split("/"):
                    continue
                if any(self._glob(p, rel) for p in wanted) and not any(
                    self._glob(p, rel) for p in unwanted
                ):
                    found[rel] = _json(self._read(path))
                    if len(found) > _MAX_PACKAGES:
                        raise LookupError(f"more than {_MAX_PACKAGES} packages")
        self._cache[tool] = found
        return found

    def _rel(self, directory: str) -> str | None:
        if not self.root:
            return directory
        return (
            directory.removeprefix(self.root + "/")
            if directory.startswith(self.root + "/")
            else None
        )

    @staticmethod
    def _glob(pattern: str, rel: str) -> bool:
        pattern = posixpath.normpath(pattern.removeprefix("./")).rstrip("/")
        return _glob_to_regex(pattern).match(rel) is not None

    def projects(self) -> dict[str, dict[str, Any]]:
        """nx projects: `{name: {"targets": ..., "scripts": ...}}` from project.json files and
        workspace packages."""
        out: dict[str, dict[str, Any]] = {}
        for rel, pkg in (self.packages("nx") or {}).items():
            name = str(pkg.get("name") or posixpath.basename(rel))
            targets = _dict(_dict(pkg.get("nx")).get("targets"))
            out[name] = {"scripts": _dict(pkg.get("scripts")), "targets": targets}
        if self._paths is None:
            raise LookupError("the file tree is not available")
        for path in self._paths():
            if (
                posixpath.basename(path) != "project.json"
                or self._rel(posixpath.dirname(path)) is None
            ):
                continue
            data = _json(self._read(path))
            name = str(data.get("name") or posixpath.basename(posixpath.dirname(path)))
            entry = out.setdefault(name, {"scripts": {}, "targets": {}})
            if isinstance(data.get("targets"), dict):
                entry["targets"] = {**entry["targets"], **data["targets"]}
            if len(out) > _MAX_PACKAGES:
                raise LookupError(f"more than {_MAX_PACKAGES} projects")
        return out


def _flatten(lines: Sequence[str], scripts: Mapping[str, Any], depth: int = 0) -> list[str]:
    """Package script bodies with `npm run x` of the same package read into script x."""
    out: list[str] = []
    for line in lines:
        for seg in split_segments(line):
            toks = command_tokens(seg.text)
            name = None
            if toks and posixpath.basename(toks[0]) in _PM and depth < 4:
                words = [t for t in toks[1:] if not t.startswith("-")]
                if words[:1] in (["run"], ["run-script"]):
                    words = words[1:]
                name = words[0] if words else None
            if name is not None and name in scripts and not _is_builtin(toks, name):
                out.extend(_flatten(_script_lines(scripts, name), scripts, depth + 1))
            else:
                out.append(seg.text)
    return out


def _is_builtin(toks: list[str], name: str) -> bool:
    """`npm test` is script `test`; `pnpm install` is not a script even when one is so named."""
    return (
        posixpath.basename(toks[0]) == "npm"
        and name not in ("test", "start")
        and toks[1:2] != ["run"]
    )


def _package_script(pkg: Mapping[str, Any], name: str) -> list[str]:
    scripts = pkg.get("scripts")
    if not isinstance(scripts, dict):
        return []
    return _flatten(_script_lines(scripts, name), scripts)


def _words(toks: Sequence[str], values: frozenset[str]) -> tuple[list[str], list[str]]:
    """`(positional words, flag names)` of arguments, a flag's value skipped."""
    words: list[str] = []
    flags: list[str] = []
    i = 0
    while i < len(toks):
        tok = toks[i]
        if tok == "--":
            break
        if tok.startswith("-") and len(tok) > 1:
            name = tok.partition("=")[0]
            flags.append(name)
            i += 1 if "=" in tok or name not in values else 2
        else:
            words.append(tok)
            i += 1
    return words, flags


def _concurrently(toks: Sequence[str], scripts: Mapping[str, Any]) -> list[str]:
    words, _ = _words(toks, _CONCURRENTLY_VALUES)
    out: list[str] = []
    for word in words:
        m = re.match(r"^(npm|pnpm|yarn|bun):(.+)$", word)
        if m is None:
            out.append(word)
        else:
            out.extend(
                _flatten([f"{m[1]} run {m[2]}"], scripts)
                if m[2] in scripts
                else [f"npm run {m[2]}"]
            )
    return out


def _per_package(
    tool: str, targets: Sequence[str], ws: Workspace, root_scripts: Mapping[str, Any], single: bool
) -> list[str] | Opaque:
    pkgs = ws.packages(tool)
    out: list[str] = []
    if pkgs is None:
        if not single:
            return Opaque(f"`{tool}` has no workspace that can be read here")
        pkgs = {"": ws.root_package()}
    for target in targets:
        for rel in sorted(pkgs):
            out.extend(_package_script(pkgs[rel], target))
        if (
            tool == "turbo"
            and pkgs
            and "" not in pkgs
            and f"//#{target}" in (ws._file("turbo.json") or "")
        ):
            out.extend(_package_script(ws.root_package(), target))
    if len(out) > _MAX_LINES:
        return Opaque(f"`{tool}` runs more than {_MAX_LINES} commands")
    return out


def _nx_target(project: Mapping[str, Any], target: str) -> list[str] | None:
    """The command an nx target runs, None when it is one that cannot be read."""
    cfg = _dict(project.get("targets")).get(target)
    if isinstance(cfg, dict):
        executor = str(cfg.get("executor", ""))
        options = _dict(cfg.get("options"))
        if not executor and "command" in cfg:
            return [str(cfg["command"])]
        if executor.endswith("run-commands"):
            cmds = options.get("commands")
            if isinstance(cmds, list):
                return [c if isinstance(c, str) else str(c.get("command", "")) for c in cmds]
            return (
                [str(options["command"])]
                if "command" in options
                else ([str(cfg["command"])] if "command" in cfg else None)
            )
        if executor.endswith("run-script"):
            return _package_script(
                {"scripts": project.get("scripts", {})}, str(options.get("script", target))
            )
        if executor.endswith(":noop"):
            return []
        for key, command in _NX_EXECUTORS.items():
            if executor.endswith(key) or executor.endswith(":" + key):
                return [command]
        return None
    scripts = project.get("scripts")
    if isinstance(scripts, dict) and target in scripts:
        return _package_script({"scripts": scripts}, target)
    return []


def _nx(toks: Sequence[str], ws: Workspace) -> list[str] | Opaque | None:
    words, flags = _words(toks, _NX_VALUES)
    sub = words[0] if words else ""
    targets: list[str] = []
    for i, tok in enumerate(toks):
        name, eq, value = tok.partition("=")
        if name in ("-t", "--target", "--targets"):
            raw = value if eq else (toks[i + 1] if i + 1 < len(toks) else "")
            targets.extend(t for t in raw.split(",") if t)
    only: str | None = None
    if sub == "run" and len(words) > 1:
        parts = words[1].split(":")
        if len(parts) < 2:
            return Opaque("`nx run` without a `project:target`")
        only, targets = parts[0], [parts[1]]
    elif sub in ("run-many", "affected"):
        if any(f in _NX_SELECT for f in flags):
            return Opaque("`nx` selects projects by name")
    elif sub.startswith("affected:") and not targets:
        targets = [sub.partition(":")[2]]
    elif sub and sub not in _NX_COMMANDS and len(words) > 1:
        only, targets = words[1], [sub]  # `nx test app`
    elif sub and sub not in _NX_COMMANDS and len(words) == 1:
        return Opaque("`nx` target without a project")
    else:
        return None
    if not targets:
        return Opaque("`nx` without a target")
    projects = ws.projects()
    if not projects:
        return Opaque("`nx` has no project that can be read here")
    if only is not None:
        if only not in projects:
            return Opaque(f"nx project `{only}` was not found")
        projects = {only: projects[only]}
    out: list[str] = []
    for target in targets:
        for name in sorted(projects):
            got = _nx_target(projects[name], target)
            if got is None:
                return Opaque(f"nx target `{name}:{target}` runs an executor that is not read")
            out.extend(got)
    return out


def task_reader(
    root: str, read: Callable[[str], str | None], paths: Callable[[], Sequence[str]] | None
) -> Reader:
    """A reader of task runner statements for the package.json in `root` of one tree."""
    ws = Workspace(root, read, paths)

    def reader(statement: str) -> list[str] | Opaque | None:
        toks = command_tokens(statement)
        if toks and posixpath.basename(toks[0]) in _PM:  # `pnpm turbo run x`, `yarn lerna run x`
            toks = next(
                (
                    toks[i:]
                    for i in range(1, min(len(toks), 4))
                    if posixpath.basename(toks[i]) in _TOOLS
                ),
                toks,
            )
        if not toks:
            return None
        head = posixpath.basename(toks[0])
        if head not in _TOOLS:
            return None
        rest = toks[1:]
        try:
            if head in ("concurrently", "conc"):
                return _concurrently(rest, ws.root_package().get("scripts") or {})
            if head == "turbo":
                words, flags = _words(rest, _TURBO_VALUES)
                if words[:1] == ["run"]:
                    words = words[1:]
                if not words or any(f in _TURBO_SELECT for f in flags):
                    return Opaque("`turbo` selects packages by name")
                if any("#" in w for w in words):
                    return Opaque("`turbo` runs a task of one package")
                return _per_package("turbo", words, ws, {}, True)
            if head == "lerna":
                words, flags = _words(rest, _LERNA_VALUES)
                if words[:1] != ["run"]:
                    return None
                if any(f.startswith(_LERNA_SELECT) for f in flags) or len(words) < 2:
                    return Opaque("`lerna run` selects packages by name")
                return _per_package("lerna", words[1:2], ws, {}, False)
            if head == "nx":
                return _nx(rest, ws)
        except LookupError as why:
            return Opaque(str(why))
        return None

    return reader


_TOOLS = frozenset({"concurrently", "conc", "turbo", "lerna", "nx"})
