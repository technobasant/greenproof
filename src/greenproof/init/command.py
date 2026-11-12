"""`greenproof init` (SPEC section 7): plan every file first, run the verification, then write.

Planning is pure, so an unusable existing file (exit 4) leaves nothing written. Nothing here
prompts, commits, or touches git configuration.
"""

from __future__ import annotations

import os
import shutil
import sys
import sysconfig
from dataclasses import dataclass
from pathlib import Path

from greenproof import __version__, gitops
from greenproof.errors import UsageError
from greenproof.init import agents_md, ci_github, write_policy
from greenproof.init.detect import Detection, ProposedCommand, detect, propose
from greenproof.init.hooks import ADAPTERS, hook_timeout
from greenproof.policy import POLICY_FILE, Policy, parse_policy
from greenproof.runner import HOOK_TAIL_LINES, run_command

AUTO_AGENTS = "auto"
NO_AGENTS = "none"
RED_MESSAGE = "the suite is red before greenproof; fix it or remove it before relying on the gate"


@dataclass(frozen=True)
class InitOptions:
    cwd: Path
    yes: bool = False
    agents: str = AUTO_AGENTS
    ci: str = "none"
    no_verify: bool = False
    hook_command: str | None = None


@dataclass(frozen=True)
class Planned:
    path: str
    status: str  # created, updated, unchanged or kept
    text: str | None  # what to write; None when nothing is written


@dataclass(frozen=True)
class Verification:
    name: str
    command: str
    status: str  # pass, fail or timeout
    detail: str
    seconds: float


def _read(root: Path, rel: str) -> str | None:
    path = root / rel
    if not path.is_file():
        return None
    try:
        return path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UsageError(f"{rel}: not valid UTF-8. Nothing was written.") from exc


def _plan(root: Path, rel: str, new_text: str, keep_existing: bool = False) -> Planned:
    existing = _read(root, rel)
    if existing is None:
        return Planned(rel, "created", new_text)
    if existing == new_text:
        return Planned(rel, "unchanged", None)
    if keep_existing:
        return Planned(rel, "kept", None)
    return Planned(rel, "updated", new_text)


def _auto_agents(root: Path) -> list[str]:
    """What the repository shows. Only adapters that are not experimental are ever chosen."""
    github = root / ".github"
    shown = {
        "claude": (root / ".claude").is_dir() or (root / "CLAUDE.md").is_file(),
        "cursor": (root / ".cursor").is_dir(),
        "codex": (root / ".codex").is_dir(),
        "opencode": (root / ".opencode").is_dir()
        or any((root / name).is_file() for name in ("opencode.json", "opencode.jsonc")),
        "copilot": (github / "hooks").is_dir() or (github / "copilot-instructions.md").is_file(),
        "qwen": (root / ".qwen").is_dir(),
        "droid": (root / ".factory").is_dir(),
    }
    return [name for name, found in shown.items() if found and ADAPTERS[name].EXPERIMENTAL is False]


def parse_agents(spec: str, root: Path) -> list[str]:
    """The agents to install hooks for: `auto` (what the repository shows), `none`, or a list."""
    value = spec.strip().lower()
    if value == NO_AGENTS:
        return []
    if value == AUTO_AGENTS:
        return _auto_agents(root)
    names = [n.strip() for n in value.split(",") if n.strip()]
    if not names:
        raise UsageError("--agents: expected auto, none or a comma-separated list")
    for name in names:
        if name not in ADAPTERS:
            raise UsageError(
                f"--agents: unknown agent {name!r} (allowed: auto, none, {', '.join(ADAPTERS)})"
            )
    return list(dict.fromkeys(names))


def _same_dir(a: str, b: str) -> bool:
    try:
        return os.path.samefile(a, b)
    except (OSError, ValueError):
        return False


def _installed_on_path(path_env: str, scripts_dir: str | None, in_venv: bool) -> bool:
    """Is there a `greenproof` on PATH that an agent's hook would also find?

    Inside a virtual environment (what `uvx` and `pipx run` run in, or a project venv that is only
    active in this shell) the environment's own scripts directory is not counted: a hook runs
    with the agent's PATH, which does not have it. The entry itself is compared, not the
    executable it points at, so the symlink that `pipx install` and `uv tool install` put in
    ~/.local/bin still counts.
    """
    for entry in path_env.split(os.pathsep):
        if not entry:
            continue
        if in_venv and scripts_dir and _same_dir(entry, scripts_dir):
            continue
        if shutil.which("greenproof", path=entry):
            return True
    return False


def _in_virtualenv() -> bool:
    return sys.prefix != sys.base_prefix


def _resolve_hook_command(override: str | None) -> tuple[str, bool]:
    """The hook command, and whether it is the pinned `uvx` fallback."""
    if override is not None:
        if not override.strip():
            raise UsageError("--hook-command: expected a command")
        return override.strip(), False
    if _installed_on_path(
        os.environ.get("PATH", ""),
        sysconfig.get_path("scripts"),
        _in_virtualenv(),
    ):
        return "greenproof", False
    return f"uvx greenproof@{__version__}", True


def resolve_hook_command(override: str | None) -> str:
    return _resolve_hook_command(override)[0]


def _plan_policy(
    root: Path, commands: dict[str, ProposedCommand], yes: bool
) -> tuple[Planned, Policy]:
    proposed_text = write_policy.render_policy(commands)
    existing = _read(root, POLICY_FILE)
    if existing is not None and not yes:
        policy = parse_policy(existing, POLICY_FILE)  # exit 4 when it is not usable
        return _plan(root, POLICY_FILE, proposed_text, keep_existing=True), policy
    return _plan(root, POLICY_FILE, proposed_text), parse_policy(proposed_text)


def _verify(root: Path, policy: Policy) -> list[Verification]:
    results = []
    for name, command in policy.runnable_commands():
        outcome = run_command(name, command, root, policy.timeout, tail_lines=HOOK_TAIL_LINES)
        if outcome.timed_out:
            status, detail = "timeout", f"timed out after {policy.timeout}s"
        elif outcome.exit_code != 0:
            status, detail = "fail", f"exit {outcome.exit_code}"
        else:
            status, detail = "pass", ""
        results.append(Verification(name, command, status, detail, outcome.duration_ms / 1000))
    return results


def _summary(
    root: Path,
    det: Detection,
    options: InitOptions,
    planned: list[Planned],
    verification: list[Verification] | None,
    notes: list[str],
    ci_hint: bool,
) -> str:
    lines = [f"greenproof init: {root}", ""]
    if det.stacks or det.targets:
        lines.append(f"detected: {det.describe()}")
    else:
        lines.append("detected: nothing; the policy has no commands, the static checks still work")
    lines.append("")
    width = max((len(p.status) for p in planned), default=0)
    lines += [f"  {p.status.ljust(width)}  {p.path}" for p in planned]
    lines.append("")
    if verification is None:
        lines.append("verification: skipped (--no-verify)")
    elif not verification:
        lines.append("verification: no commands to run")
    else:
        lines.append("verification:")
        for v in verification:
            detail = f" {v.detail}" if v.detail else ""
            lines.append(f"  {v.status.ljust(7)}  {v.name}: {v.command} ({v.seconds:.1f}s){detail}")
            if v.status != "pass":
                lines.append(f"           {RED_MESSAGE}")
    next_steps = list(notes)
    if not gitops.has_commits(root):
        next_steps.append("this repository has no commits yet; `greenproof check` needs one")
    if ci_hint:
        next_steps.append(
            "`.github/workflows/` exists: add the CI gate with `greenproof init --ci github`"
        )
    next_steps.append("review the files above and commit them; `greenproof check` runs the gate")
    lines += ["", "next steps:"] + [f"  - {s}" for s in next_steps]
    return "\n".join(lines) + "\n"


def run_init(options: InitOptions) -> str:
    """Perform init and return the summary text. Raises UsageError (exit 4) when an existing
    config file is unusable; any other exception is an internal error (exit 5)."""
    root = gitops.find_repo(options.cwd)
    det = detect(root)
    commands = propose(root, det)
    agents = parse_agents(options.agents, root)
    hook_command, uvx_fallback = _resolve_hook_command(options.hook_command)

    policy_plan, policy = _plan_policy(root, commands, options.yes)
    planned = [policy_plan]
    runnable = policy.runnable_commands()

    section = agents_md.render_section(runnable, hook_command)
    agents_existing = _read(root, "AGENTS.md")
    planned.append(
        _plan(root, "AGENTS.md", agents_md.upsert_section(agents_existing, section, "AGENTS.md"))
    )
    claude_md = _read(root, "CLAUDE.md")
    if claude_md is not None:
        planned.append(_plan(root, "CLAUDE.md", agents_md.claude_md_update(claude_md)))

    timeout = hook_timeout(policy.timeout, len(runnable))
    notes: list[str] = []
    if uvx_fallback:
        callers = "hooks and AGENTS.md call" if agents else "AGENTS.md calls"
        notes.append(
            f"{callers} `{hook_command}`: no installed greenproof outside this "
            "environment was found on PATH; install it with `uv tool install greenproof` or "
            "`pipx install greenproof` and re-run init, or pass --hook-command"
        )
    for name in agents:
        adapter = ADAPTERS[name]
        planned.append(
            _plan(
                root, adapter.PATH, adapter.render(_read(root, adapter.PATH), hook_command, timeout)
            )
        )
        if warning := getattr(adapter, "WARNING", None):
            notes.append(f"warning: {warning}")
        if note := getattr(adapter, "NOTE", None):
            notes.append(note)

    if options.ci == "github":
        workflow = ci_github.render_workflow(root, det, __version__, policy)
        planned.append(
            _plan(root, ci_github.WORKFLOW_PATH, workflow, keep_existing=not options.yes)
        )
    ci_hint = options.ci != "github" and (root / ".github" / "workflows").is_dir()

    verification = None if options.no_verify else _verify(root, policy)

    for item in planned:
        if item.text is not None:
            target = root / item.path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(item.text.encode("utf-8"))
    return _summary(root, det, options, planned, verification, notes, ci_hint)
