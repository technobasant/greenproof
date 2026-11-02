"""Shell scripts that swallow a check's failure, and the selection flags of test runners.

One detector for the CI rule (a workflow `run:` body) and the runner rule (a package.json
script, a Makefile recipe, a script a policy command runs). It reads text, never executes it:
a small splitter turns a script into statements, then a few patterns look at them.

A statement is a *check* only when its command, in command position, is a test runner, a
linter or a type checker, or a command the caller names (a policy command, a script it runs).
Command position is the first word after environment assignments and wrappers (`uv run`,
`python -m`, `npx`, `time`, `timeout 5`, ...): `pip install pytest-xdist || true` and
`pkill -f pytest || true` do not run pytest.

A script swallows a failure when a check can fail and the script still exits 0:

- `check || <anything but a failing exit>` (`|| true`, `|| :`, `|| echo ok`, `|| exit 0`);
- `check; exit 0`, `check; true`, `check; :` or an unconditional `exit 0` after a check ran,
  when the shell is not running with errexit (`set -e`, a GitHub Actions `run:` step);
- `set +e` / `set +o errexit` before a check, up to the next `set -e`, when the check's status
  is never read afterwards.

`rm -rf build || true` and `pip install -e . || pip install .` swallow nothing a check did. A
script with no recognisable check at all (an opaque policy command) is treated as all check,
or, given the statements it had at the base, as those statements only.
"""

from __future__ import annotations

import posixpath
import re
import shlex
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, replace

# What a command-position word is, by the kind of check it runs.
_SIMPLE_CHECKERS = {
    "pytest": "pytest",
    "py.test": "pytest",
    "unittest": "unittest",
    "tox": "tox/nox",
    "nox": "tox/nox",
    "jest": "jest/vitest",
    "vitest": "jest/vitest",
    "mocha": "jest/vitest",
    "rspec": "rspec",
    "rake": "rake",
    "phpunit": "phpunit",
    "ctest": "ctest",
    "bats": "bats",
    "ava": "ava",
    "tap": "tap",
    "gotestsum": "go test",
    "greenproof": "greenproof",
    "ruff": "ruff",
    "mypy": "mypy",
    "pyright": "mypy",
    "flake8": "flake8",
    "pylint": "pylint",
    "eslint": "eslint",
    "biome": "eslint",
    "tsc": "eslint",
    "golangci-lint": "clippy",
}
_INFO_FLAGS = frozenset({"--version", "-V", "--help", "-h"})
_MAKE_NAMES = frozenset({"test", "check", "lint"})
_MAKERS = {"make", "gmake", "just", "$(MAKE)", "${MAKE}", "$MAKE"}
_PKG_MANAGERS = {"npm", "pnpm", "yarn", "bun"}
_PKG_VALUE_OPTS = {"--filter", "-F", "--cwd", "-C", "--prefix", "--workspace", "-w"}
_PKG_SCRIPT = re.compile(r"^(?P<kind>test|lint|typecheck)(?::.*)?$")

# Flags that select or narrow what a runner runs, by runner family. `-k`, `-m` and the like are
# how a candidate runs fewer tests than the base while the command still looks the same.
SELECT_FLAGS: dict[str, frozenset[str]] = {
    "pytest": frozenset(
        {"-k", "-m", "--deselect", "--ignore", "--ignore-glob", "--lf", "--last-failed"}
    ),
    "go": frozenset({"-run", "-skip", "-short", "-test.run", "-test.skip", "-test.short"}),
    "js": frozenset(
        {"-t", "--testNamePattern", "--testPathIgnorePatterns", "--onlyChanged", "-o"}
        | {"--findRelatedTests"}
    ),
    "cargo": frozenset({"--skip"}),
    "mocha": frozenset({"--grep", "-g", "--invert"}),
}
# Flags after which the runner runs no test, or passes with none.
NOOP_FLAGS: dict[str, frozenset[str]] = {
    "pytest": frozenset({"--co", "--collect-only"}),
    "js": frozenset({"--passWithNoTests"}),
}
_ALL_FLAGS = {
    family: SELECT_FLAGS.get(family, frozenset()) | NOOP_FLAGS.get(family, frozenset())
    for family in set(SELECT_FLAGS) | set(NOOP_FLAGS)
}


@dataclass(frozen=True)
class Seg:
    sep: str  # what came before: "", ";", "\n", "&&", "||"
    text: str
    line: int  # 1-based physical line of the segment's first character


@dataclass(frozen=True)
class Swallow:
    line: int
    kind: str  # "or", "exit0", "true", "set+e", "early"
    text: str
    reason: str


# -- splitting -------------------------------------------------------------------------
def _skip_expansion(text: str, i: int) -> int:
    """`i` is at `$`; the index after a `${...}` expansion, else `i + 1`."""
    if text.startswith("${", i):
        end = text.find("}", i + 2)
        return len(text) if end < 0 else end + 1
    return i + 1


_HEREDOC = re.compile(r"<<(-?)[ \t]*(?:'([^'\n]+)'|\"([^\"\n]+)\"|\\?([A-Za-z_][\w.-]*))")


def split_segments(script: str) -> list[Seg]:
    """Top-level commands of a script, split at `;`, newlines, `&&`, `||` and `&`.

    Quotes, `$(...)`, `( ... )` and `{ ...; }` groups keep their contents in one segment, a
    trailing backslash joins lines, `#` starts a comment, and a here-document's body is data
    that is skipped.
    """
    text = script.replace("\r\n", "\n").replace("\r", "\n")
    segs: list[Seg] = []
    buf: list[str] = []
    sep = ""
    line = 1
    seg_line = 1
    paren = brace = 0
    quote = ""
    heredocs: list[tuple[str, bool]] = []  # (delimiter, tabs stripped) read after this line
    i, n = 0, len(text)

    def flush(next_sep: str) -> None:
        nonlocal sep, seg_line
        body = "".join(buf).strip()
        if body:
            segs.append(Seg(sep, body, seg_line))
            sep = next_sep
        elif next_sep in ("&&", "||"):
            sep = next_sep
        elif not segs:
            sep = ""
        buf.clear()
        seg_line = line

    def skip_heredocs(j: int) -> int:
        """`j` starts the line after a newline: the index after the bodies of pending here-docs."""
        nonlocal line
        for delim, tabs in heredocs:
            while j < n:
                end = text.find("\n", j)
                body_line = text[j : n if end < 0 else end]
                j = n if end < 0 else end + 1
                line += 1
                if (body_line.lstrip("\t") if tabs else body_line) == delim:
                    break
        heredocs.clear()
        return j

    while i < n:
        c = text[i]
        if quote:
            buf.append(c)
            if c == "\n":
                line += 1
            if c == "\\" and quote == '"' and i + 1 < n:
                buf.append(text[i + 1])
                i += 1
            elif c == quote:
                quote = ""
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            if text[i + 1] == "\n":
                line += 1
                buf.append(" ")
            else:
                buf.append(text[i : i + 2])
            i += 2
            continue
        if c in "'\"":
            quote = c
            buf.append(c)
        elif c == "#" and (not buf or buf[-1] in " \t"):
            while i < n and text[i] != "\n":
                i += 1
            continue
        elif c == "$":
            j = _skip_expansion(text, i)
            if j > i + 1:
                buf.append(text[i:j])
                i = j
                continue
            buf.append(c)
        elif c == "<" and not paren and text.startswith("<<", i) and text[i + 2 : i + 3] != "<":
            m = _HEREDOC.match(text, i)
            if m:
                heredocs.append((m[2] or m[3] or m[4], bool(m[1])))
                buf.append(m[0])
                i = m.end()
                continue
            buf.append(c)
        elif c == "(":
            paren += 1
            buf.append(c)
        elif c == ")":
            paren = max(0, paren - 1)
            buf.append(c)
        elif c == "{" and (not buf or buf[-1] in " \t;") and text[i + 1 : i + 2] in (" ", "\n"):
            brace += 1
            buf.append(c)
        elif c == "}" and brace and (not buf or buf[-1] in " \t;\n"):
            brace -= 1
            buf.append(c)
        elif paren or brace:
            buf.append(c)
            if c == "\n":
                line += 1
                if heredocs:
                    i = skip_heredocs(i + 1) - 1
        elif c == "\n":
            flush("\n")
            line += 1
            seg_line = line
            if heredocs:
                i = skip_heredocs(i + 1) - 1
                seg_line = line
        elif c == ";":
            flush(";")
        elif c == "&" and text[i + 1 : i + 2] == "&":
            flush("&&")
            i += 1
        elif c == "&" and (not buf or buf[-1] not in "<>") and text[i + 1 : i + 2] != ">":
            flush(";")
        elif c == "|" and text[i + 1 : i + 2] == "|":
            flush("||")
            i += 1
        else:
            buf.append(c)
        i += 1
    flush("")
    return segs


# -- command position --------------------------------------------------------------------
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\+?=")
# Words that open a statement without being its command.
_LEADING = {"if", "then", "elif", "else", "while", "until", "do", "!", "{", "(", "}", ")"}
_PLAIN_WRAPPERS = {"time", "exec", "command", "nohup", "stdbuf"}
# Wrapper -> the options that take a value.
_OPT_VALUES: dict[str, frozenset[str]] = {
    "env": frozenset({"-u", "-C", "-S"}),
    "sudo": frozenset({"-u", "-g", "-C", "-h", "-p", "-r", "-t", "-T", "-U"}),
    "nice": frozenset({"-n"}),
    "timeout": frozenset({"-s", "-k", "--signal", "--kill-after"}),
    "xvfb-run": frozenset({"-s", "-n", "-f", "-e", "-p", "-l", "--server-args", "--server-num"}),
    "stdbuf": frozenset({"-i", "-o", "-e"}),
    "time": frozenset({"-o", "-f"}),
}
_RUN_VALUE_OPTS = frozenset(
    {"--with", "--extra", "--python", "-p", "--group", "--package", "--project", "--directory"}
    | {"--env-file", "--with-requirements", "--from", "--with-editable", "--only-group", "--index"}
    | {"-w", "-e", "--cwd", "--env"}
)
_RUN_TOOLS = {"uv", "poetry", "pipenv", "pdm", "hatch", "rye"}
_RUNX = {"uvx", "npx", "bunx", "pnpx"}
# `docker compose run`, `docker exec`, `conda run`, `bundle exec`: launchers that run the command
# after their own options (and, for a container, its service or image name).
_DOCKER_VALUES = frozenset(
    {"-e", "--env", "-v", "--volume", "-u", "--user", "-w", "--workdir", "--entrypoint"}
    | {"-p", "--publish", "--name", "-l", "--label", "--cap-add", "--cap-drop", "--network"}
    | {"--net", "--env-file", "--platform", "--mount", "-h", "--hostname", "--add-host", "--pull"}
    | {"-m", "--memory", "--cpus", "--gpus"}
)
_COMPOSE_VALUES = frozenset(
    {"-f", "--file", "-p", "--project-name", "--profile", "--env-file", "--project-directory"}
    | {"--ansi", "--parallel", "--progress"}
)
_CONDA_VALUES = frozenset({"-n", "--name", "-p", "--prefix", "--cwd"})
_CONDA = {"conda", "mamba", "micromamba"}
_SHELLS = {"sh", "bash", "zsh", "dash", "ksh"}
_VAR = re.compile(r"^\$(?:\((?P<a>[^()]*)\)|\{(?P<b>[^{}]*)\}|(?P<c>[A-Za-z_]\w*))$")


def _cmd_name(token: str) -> str:
    """A command word as the tool it names: the basename, lowercased, with `$(PYTHON)`,
    `${PYTHON}` and `$PYTHON` (a Makefile or shell variable) read as `python`."""
    base = posixpath.basename(token)
    if m := _VAR.match(base):
        name = m["a"] or m["b"] or m["c"] or ""
        if ":-" in name:
            name = name.partition(":-")[2] or name.partition(":-")[0]
        base = posixpath.basename(name)
    return base.lower()


def _tokens(statement: str) -> list[str]:
    try:
        return shlex.split(statement, posix=True)
    except ValueError:
        return statement.split()


def _skip_opts(toks: list[str], values: Collection[str], positional: int = 0) -> list[str]:
    """`toks` without its leading options (a value-taking option also drops its value), then
    without `positional` plain words (`timeout 5`)."""
    i = 0
    while i < len(toks) and toks[i].startswith("-") and toks[i] != "--":
        i += 2 if toks[i] in values else 1
    if i < len(toks) and toks[i] == "--":
        i += 1
    return toks[min(i + positional, len(toks)) :]


def _container_command(base: str, rest: list[str]) -> list[str] | None:
    """The command `docker run|exec IMAGE ...` or `docker compose run|exec SERVICE ...` runs."""
    if base == "docker-compose" or rest[:1] == ["compose"]:
        rest = _skip_opts(rest if base == "docker-compose" else rest[1:], _COMPOSE_VALUES)
    if rest[:1] in (["run"], ["exec"]):
        return _skip_opts(rest[1:], _DOCKER_VALUES, 1)
    return None


def _strip(tokens: list[str]) -> list[str]:
    """Drop what comes before the command: assignments, `if`/`then`/`!`, and wrappers."""
    toks = list(tokens)
    while toks:
        tok = toks[0]
        base = _cmd_name(tok)
        nxt = toks[1] if len(toks) > 1 else ""
        if _ASSIGN.match(tok) or tok in _LEADING:
            toks = toks[1:]
        elif base in _OPT_VALUES or base in _PLAIN_WRAPPERS:
            rest = _skip_opts(toks[1:], _OPT_VALUES.get(base, frozenset()), int(base == "timeout"))
            if base == "env":
                while rest and _ASSIGN.match(rest[0]):
                    rest = rest[1:]
            toks = rest
        elif base in _RUN_TOOLS and nxt == "run":
            toks = _skip_opts(toks[2:], _RUN_VALUE_OPTS)
            if base == "hatch" and toks and re.match(r"^[\w.-]*:", toks[0]):
                toks[0] = toks[0].partition(":")[2]
        elif base == "uv" and toks[1:3] == ["tool", "run"]:
            toks = _skip_opts(toks[3:], _RUN_VALUE_OPTS)
        elif base == "pipx" and nxt == "run":
            toks = _skip_opts(toks[2:], _RUN_VALUE_OPTS)
        elif base in _CONDA and nxt == "run":
            toks = _skip_opts(toks[2:], _CONDA_VALUES)
        elif base in ("bundle", "bundler") and nxt == "exec":
            toks = _skip_opts(toks[2:], frozenset())
        elif base in ("docker", "docker-compose") and (
            container := _container_command(base, toks[1:])
        ):
            toks = container
        elif base in _RUNX:
            toks = _skip_opts(toks[1:], _RUN_VALUE_OPTS | {"-p", "--package"})
        elif base in _PKG_MANAGERS and nxt in ("exec", "dlx", "x"):
            toks = _skip_opts(toks[2:], _PKG_VALUE_OPTS)
        else:
            break
    return toks


_PYTHON = re.compile(r"^(?:python[\d.]*|pypy[\d.]*|py)$")
_CODE_FLAG = re.compile(r"""['"](-[^'"\s]+)['"]""")


def _collapse(toks: list[str]) -> list[str]:
    """`python -m X ...` and `coverage run -m X ...` are `X ...`; `python -c "pytest.main(...)"`
    is `pytest ...`."""
    if not toks:
        return toks
    base = _cmd_name(toks[0])
    if _PYTHON.match(base):
        j = 1
        while j < len(toks) and toks[j].startswith("-") and toks[j] != "-m":
            if toks[j] == "-c" and j + 1 < len(toks):
                code = toks[j + 1]
                return ["pytest", *_CODE_FLAG.findall(code)] if "pytest.main" in code else toks
            j += 2 if toks[j] in ("-W", "-X") else 1
        if j < len(toks) and toks[j] == "-m":
            return toks[j + 1 :]
    elif base in ("coverage", "coverage3") and toks[1:2] == ["run"] and "-m" in toks:
        return toks[toks.index("-m") + 1 :]
    return toks


def _shell_c(toks: list[str]) -> str | None:
    """The script string of `sh|bash -c "..."` (`-lc`, `-ec`, `-e -c` too), else None."""
    if not toks or _cmd_name(toks[0]) not in _SHELLS:
        return None
    i = 1
    while i < len(toks) and toks[i][:1] in "-+" and len(toks[i]) > 1:
        if re.match(r"^-[A-Za-z]*c[A-Za-z]*$", toks[i]):
            return toks[i + 1] if i + 1 < len(toks) else None
        i += 2 if toks[i] in ("-o", "+o") else 1
    return None


def shell_c_body(text: str) -> str | None:
    """The script a `bash -c "..."` statement runs (wrappers like `sudo` allowed), else None."""
    return _shell_c(_strip(_tokens(text)))


def command_tokens(text: str) -> list[str]:
    """The words of a statement from its command on: the wrappers are gone, `python -m X` is X."""
    toks = _tokens(text)
    previous: list[str] | None = None
    while toks != previous:
        previous = toks
        toks = _strip(_collapse(_strip(toks)))
        if (body := _shell_c(toks)) is not None:
            segs = split_segments(body)
            toks = _tokens(segs[0].text) if segs else []
    return toks


def _group_inner(text: str) -> str | None:
    t = text.strip()
    if t.startswith("("):
        return t[1:-1] if t.endswith(")") else t[1:]
    if t.startswith("{") and t[1:2] in (" ", "\n"):
        inner = t[1:].rstrip()
        return inner[:-1] if inner.endswith("}") else inner
    return None


def _is_info_only(rest: list[str]) -> bool:
    return any(a in _INFO_FLAGS for a in rest)


def _package_script(rest: list[str]) -> list[str]:
    """The words of an `npm|pnpm|yarn|bun` call from its script (or command) word on."""
    for _ in range(2):
        rest = _skip_opts(rest, _PKG_VALUE_OPTS)
        if rest and rest[0] in ("run", "run-script"):
            rest = rest[1:]
    return _skip_opts(rest, _PKG_VALUE_OPTS)


# Commands that are a check only with a given subcommand: `playwright test`, `cypress run`, ...
_SUB_CHECKERS: dict[str, tuple[frozenset[str], str]] = {
    "playwright": (frozenset({"test"}), "playwright"),
    "cypress": (frozenset({"run"}), "cypress"),
    "karma": (frozenset({"start", "run"}), "karma"),
    "ng": (frozenset({"test"}), "ng test"),
    "deno": (frozenset({"test"}), "deno test"),
    "swift": (frozenset({"test"}), "swift test"),
    "mix": (frozenset({"test"}), "mix test"),
    "hatch": (frozenset({"test"}), "pytest"),
    "rails": (frozenset({"test"}), "rake"),
}
_CARGO_TESTS = frozenset({"test", "nextest", "llvm-cov", "tarpaulin"})


def classify(toks: list[str], make_check: Callable[[str], bool | None] | None = None) -> str | None:
    """The kind of check a command is (`pytest`, `go test`, `ruff`, ...), None when it is none.

    `make_check(target)` says whether a target of the same Makefile holds a check (None when
    the target is unknown); without it `make test|check|lint` is a check by name.
    """
    if not toks:
        return None
    cmd = _cmd_name(toks[0])
    rest = toks[1:]
    if "|" in rest:
        rest = rest[: rest.index("|")]
    if cmd in _SIMPLE_CHECKERS:
        return None if _is_info_only(rest) else _SIMPLE_CHECKERS[cmd]
    if cmd in _SUB_CHECKERS:
        subs, kind = _SUB_CHECKERS[cmd]
        words = _skip_opts(rest, frozenset())
        return kind if words[:1] and words[0] in subs and not _is_info_only(rest) else None
    if cmd in ("node", "nodejs"):
        return "node test" if "--test" in rest[:3] else None
    if cmd == "go":
        return "go test" if rest[:1] == ["test"] else None
    if cmd == "cargo":
        rest = [a for a in rest if not a.startswith("+")]
        sub = rest[0] if rest else ""
        if sub in _CARGO_TESTS:
            return "cargo test"
        return "clippy" if sub in ("clippy", "fmt") else None
    if cmd == "dotnet":
        return "dotnet test" if rest[:1] == ["test"] else None
    if cmd in ("mvn", "mvnw"):
        return "mvn" if any(a in ("test", "verify") for a in rest) else None
    if cmd in ("gradle", "gradlew"):
        return "gradle" if any(a in ("test", "check") for a in rest) else None
    if cmd in _MAKERS:
        return _make_kind(rest, make_check)
    if cmd in _PKG_MANAGERS:
        words = _package_script(rest)
        if not words:
            return None
        if m := _PKG_SCRIPT.match(words[0]):
            return "package test" if m["kind"] == "test" else "package lint"
        name = _cmd_name(words[0])
        return (
            classify(words, make_check)
            if name in _SIMPLE_CHECKERS or name in _SUB_CHECKERS
            else None
        )
    return None


def _make_kind(rest: list[str], make_check: Callable[[str], bool | None] | None) -> str | None:
    for arg in rest:
        if arg in ("-C", "-f", "--directory", "--file", "--justfile", "--makefile") or (
            arg.startswith(("-C", "-f", "--directory=", "--file=", "--justfile=", "--makefile="))
        ):
            return None  # another Makefile: opaque
    for target in (a for a in rest if not a.startswith("-") and "=" not in a):
        known = make_check(target) if make_check is not None else None
        if known if known is not None else target in _MAKE_NAMES:
            return "make test"
    return None


def check_kind(
    text: str,
    *,
    literals: Sequence[str] = (),
    scripts: Collection[str] = (),
    make_check: Callable[[str], bool | None] | None = None,
) -> str | None:
    """The kind of check a statement runs in command position, None when it is not a check.

    `literals` are policy commands (a statement containing one is a check), `scripts` the
    repo-relative paths of scripts a policy command runs.
    """
    inner = _group_inner(text)
    if inner is not None:
        for seg in split_segments(inner):
            kind = check_kind(seg.text, literals=literals, scripts=scripts, make_check=make_check)
            if kind:
                return kind
        return None
    if (body := shell_c_body(text)) is not None:  # sh -c "..."
        for seg in split_segments(body):
            kind = check_kind(seg.text, literals=literals, scripts=scripts, make_check=make_check)
            if kind:
                return kind
        return None
    toks = command_tokens(text)
    if kind := classify(toks, make_check):
        return kind
    if literals and any(lit in text for lit in literals):
        return "policy"
    if scripts and (script := script_of(_tokens(text))) is not None:
        norm = posixpath.normpath(script)
        if norm in scripts:
            return "policy"
    return None


# -- statements that are recognisably not a check ---------------------------------------------
# Words that run nothing, or only set things up, move files or talk to the network. A step made
# only of these has no check in it; any other command might be one (`poe test`, `pre-commit run`).
_NON_CHECK_WORDS = frozenset(
    {"echo", "printf", "true", "false", ":", "exit", "return", "sleep", "cat", "head", "tail"}
    | {"tee", "ls", "pwd", "cd", "pushd", "popd", "mkdir", "rmdir", "rm", "cp", "mv", "ln", "touch"}
    | {"chmod", "chown", "export", "unset", "set", "shopt", "env", "printenv", "which", "type"}
    | {"test", "[", "[[", "read", "source", ".", "tar", "zip", "unzip", "gzip", "gunzip", "find"}
    | {"curl", "wget", "git", "gh", "ssh", "scp", "rsync", "date", "uname", "whoami", "id", "wc"}
    | {"sed", "awk", "grep", "sort", "uniq", "tr", "cut", "xargs", "basename", "dirname", "jq"}
    | {"apt", "apt-get", "dpkg", "brew", "choco", "winget", "yum", "dnf", "apk", "pacman"}
    | {"aws", "gcloud", "az", "kubectl", "helm", "terraform", "ansible", "ansible-playbook"}
    | {"twine", "wrangler", "vercel", "netlify", "firebase", "heroku", "fly", "flyctl", "gpg"}
    | {"fi", "done", "esac", "else", "for", "case", "select", "in", "then", "do"}
)
_NON_CHECK_SUBCOMMANDS: dict[str, frozenset[str]] = {
    "pip": frozenset({"install", "uninstall", "download", "freeze", "list", "show", "config"}),
    "pip3": frozenset({"install", "uninstall", "download", "freeze", "list", "show", "config"}),
    "pipx": frozenset({"install", "uninstall", "ensurepath", "upgrade"}),
    "uv": frozenset({"sync", "pip", "venv", "build", "publish", "lock", "add", "remove", "python"}),
    "poetry": frozenset({"install", "build", "publish", "config", "lock", "add", "remove"}),
    "npm": frozenset({"ci", "install", "i", "publish", "pack", "version", "config", "login"}),
    "pnpm": frozenset({"install", "i", "add", "publish", "config", "store", "setup"}),
    "yarn": frozenset({"install", "add", "publish", "config", "set", "cache"}),
    "bun": frozenset({"install", "i", "add", "publish", "pm"}),
    "cargo": frozenset({"build", "publish", "install", "fetch", "doc", "package", "login"}),
    "go": frozenset({"build", "mod", "get", "install", "generate", "env", "version"}),
    "docker": frozenset(
        {"build", "push", "pull", "login", "logout", "tag", "save", "load", "image", "buildx"}
        | {"version", "info", "system", "volume", "network", "ps", "logs", "cp", "stop", "rm"}
        | {"rmi", "kill", "inspect"}
    ),
    "make": frozenset(
        {"build", "clean", "install", "docs", "doc", "deploy", "publish", "release", "dist"}
        | {"package", "format", "fmt"}
    ),
}
_PKG_BUILD_SCRIPT = re.compile(
    r"^(?:build|compile|bundle|deploy|publish|release|docs?|clean|prepare|start|dev|format)(?::.*)?$"
)
_DEPLOY_SCRIPT = re.compile(
    r"^(?:deploy|publish|release|upload|notify|announce|tag|changelog|docs?|package|dist|bump"
    r"|sign|install|setup|bootstrap)[\w.-]*$"
)
_PS_CMDLET = re.compile(
    r"^(?:Write|Set|New|Copy|Remove|Get|Start|Out|Add|Move|Test|Invoke-WebRequest)-?[A-Za-z]*$"
)


def is_non_check(text: str) -> bool:
    """Whether a statement is recognisably not a check: a command from the list of ones that run
    no tests (`echo`, `exit 0`, `pip install`, `git`, `docker push`, a `deploy.sh`)."""
    inner = _group_inner(text)
    if inner is not None:
        return all(is_non_check(seg.text) for seg in split_segments(inner))
    if (body := shell_c_body(text)) is not None:
        return all(is_non_check(seg.text) for seg in split_segments(body))
    toks = command_tokens(text)
    if not toks:
        return True  # nothing but assignments
    cmd = _cmd_name(toks[0])
    if cmd in _NON_CHECK_WORDS or (
        re.match(r"^[A-Za-z]+-[A-Za-z]+$", toks[0]) and _PS_CMDLET.match(toks[0])
    ):
        return True
    if cmd in _MAKERS and classify(toks) is None:
        words = [a for a in toks[1:] if not a.startswith("-") and "=" not in a]
        return bool(words) and all(w in _NON_CHECK_SUBCOMMANDS["make"] for w in words)
    if cmd in _PKG_MANAGERS:
        words = _package_script(toks[1:])
        if words and _PKG_BUILD_SCRIPT.match(words[0]) and classify(toks) is None:
            return True
    if (cmd == "docker" and toks[1:2] == ["compose"]) or cmd == "docker-compose":
        sub = _skip_opts(toks[2:] if cmd == "docker" else toks[1:], _COMPOSE_VALUES)[:1]
        return bool(sub) and sub[0] in {"build", "pull", "push", "down", "stop", "rm", "logs", "ps"}
    subs = _NON_CHECK_SUBCOMMANDS.get(cmd)
    if subs is not None:
        words = _skip_opts(toks[1:], frozenset())
        return bool(words) and words[0] in subs and classify(toks) is None
    stem = posixpath.basename(toks[0]).rpartition(".")[0] or posixpath.basename(toks[0])
    return ("/" in toks[0] or toks[0].endswith(_SCRIPT_EXT)) and bool(_DEPLOY_SCRIPT.match(stem))


# -- scripts a statement runs --------------------------------------------------------------
_INTERPRETERS = re.compile(
    r"^(?:sh|bash|zsh|dash|ksh|python[\d.]*|node|nodejs|deno|ruby|perl|php|source|\.)$"
)
_SCRIPT_EXT = (".sh", ".bash", ".zsh", ".py", ".js", ".mjs", ".cjs", ".ts", ".rb", ".pl", ".php")


def script_of(tokens: list[str]) -> str | None:
    """The script file a statement runs, when its command is a script or an interpreter's."""
    toks = _strip(tokens)
    if not toks:
        return None
    tok = toks[0]
    if _INTERPRETERS.match(_cmd_name(tok)):
        rest = [t for t in toks[1:] if not t.startswith("-")]
        if toks[1:2] in (["-m"], ["-c"]):
            return None
        return rest[0] if rest else None
    return tok if "/" in tok or tok.endswith(_SCRIPT_EXT) else None


def scripts_in(text: str, here: str) -> list[str]:
    """Repo-relative paths of the scripts the statements of `text` run."""
    found: list[str] = []
    for seg in split_segments(text):
        tok = script_of(_tokens(seg.text))
        if tok is None or tok.startswith(("/", "~", "$")) or "$" in tok:
            continue
        norm = posixpath.normpath(tok)
        if not norm.startswith(".."):
            found.append(norm)
        if here:
            sibling = posixpath.normpath(posixpath.join(here, tok))
            if sibling != norm and not sibling.startswith(".."):
                found.append(sibling)
    return found


# -- patterns --------------------------------------------------------------------------
_EXIT = re.compile(r"^(?P<word>exit|return)(?:\s+(?P<code>\S+))?$")
_SET_PLUS_E = re.compile(r"^set\s+(?:\+[A-Za-z]*e[A-Za-z]*|\+o\s+errexit)(?:\s|$)")
_SET_MINUS_E = re.compile(r"^set\s+(?:-[A-Za-z]*e[A-Za-z]*|-o\s+errexit)(?:\s|$)")
_STATUS_READ = re.compile(
    r"\$\?|\$\{\?|PIPESTATUS|\bexit\s+[\"']?\$|\breturn\s+[\"']?\$|\bif\b|\bwhile\b"
    r"|\|\|\s*(?:exit|return|false|\{|\()|\bwait\b|\$LASTEXITCODE\b(?!\s*=(?!=))"
)
# PowerShell: `$LASTEXITCODE = 0` throws away a native command's status; a cmdlet (`Write-Host`,
# `echo`, `cd`, `$x = 1`) does not touch it.
_RESET_STATUS = re.compile(r"^\$LASTEXITCODE\s*=\s*0\s*$", re.IGNORECASE)
_CMDLET = re.compile(
    r"^(?:[A-Za-z]+-[A-Za-z]+|echo|cd|ls|dir|cat|type|sleep|pwd|cls|clear|\$\w+\s*=)(?:\s|$)",
    re.IGNORECASE,
)
# What `A || B` may retry with: flags that rerun the failures of the run `A` made.
RERUN_FLAGS = frozenset(
    {"--lf", "--last-failed", "--ff", "--failed-first", "--sw", "--stepwise", "--reruns"}
)
_CONDITION = re.compile(r"^(?:if|elif|while|until)\b|^\[|^test\b")
_KEYWORD = re.compile(r"^(?P<kw>then|else|do)\b\s*(?P<rest>.*)$", re.DOTALL)
_BARE_EXIT = re.compile(r"^exit(?:\s+0+)?$")


def _unquote(code: str) -> str:
    return code.strip("\"'")


def _is_zero_exit(text: str) -> bool:
    m = _EXIT.match(text.strip())
    return bool(m and m["code"] is not None and re.fullmatch(r"0+", _unquote(m["code"])))


def _is_failing_exit(text: str) -> bool:
    """`exit N` (N not 0), a bare `exit`, `exit $x`, `false`, `return N`, or a group of them."""
    t = text.strip()
    if t == "false":
        return True
    if t.startswith(("{", "(")):
        inner = t[1:-1] if t[-1] in "})" else t[1:]
        return any(_is_failing_exit(s.text) for s in split_segments(inner.strip(" ;")))
    m = _EXIT.match(t)
    if not m:
        return False
    return m["code"] is None or not re.fullmatch(r"0+", _unquote(m["code"]))


def _short(text: str) -> str:
    return " ".join(text.split())[:80]


def _first_word(text: str) -> str:
    parts = text.split(None, 1)
    return parts[0] if parts else ""


def _top_level(segs: Sequence[Seg]) -> list[bool]:
    """For each segment: whether it runs unconditionally, at the top level of the script.

    Statements inside `if`/`case`/`for`/`while`, after `then`/`else`/`do`, and after `&&` or
    `||` are conditional. `if true; then ...; fi` and `if :; then ...; fi` are not.
    """
    stack: list[bool] = []  # per open block: whether it always runs
    out: list[bool] = []
    for seg in segs:
        text = seg.text
        kw = _KEYWORD.match(text)
        if kw:
            text = kw["rest"]
        word = _first_word(text)
        if word in ("fi", "esac", "done") and stack:
            stack.pop()
        always = all(stack)
        if kw:
            always = always and bool(stack) and stack[-1] and kw["kw"] != "else"
        out.append(always and seg.sep in ("", ";", "\n") and not (kw and not stack))
        if word in ("if", "case", "for", "while", "until"):
            stack.append(word == "if" and text[len(word) :].strip() in ("true", ":"))
    return out


def early_exit(script: str) -> bool:
    """An unconditional `exit 0` (or bare `exit`) with more commands after it."""
    segs = split_segments(script)
    top = _top_level(segs)
    for i, seg in enumerate(segs):
        text = _KEYWORD.sub(r"\g<rest>", seg.text) if _KEYWORD.match(seg.text) else seg.text
        if top[i] and _BARE_EXIT.match(text.strip()):
            return i + 1 < len(segs)
    return False


def _flat_swallows(
    script: str,
    extra: Sequence[str] = (),
    *,
    opaque: bool = True,
    errexit: bool = False,
    early: bool = False,
    scripts: Collection[str] = (),
    make_check: Callable[[str], bool | None] | None = None,
    base_segments: Collection[str] | None = None,
    known: bool | None = None,
    strict_status: bool = False,
    powershell: bool = False,
) -> list[Swallow]:
    """The places where `script` can fail a check and still exit 0 (see the module docstring).

    `extra` are literal command strings that also count as a check (a policy command), and
    `scripts` the scripts such a command runs. `opaque` treats a script with no recognisable
    check as all check, or, with `base_segments`, as those statements only. `errexit` is whether
    the shell starts with `set -e` (a GitHub Actions step); `early` also reports an `exit 0`
    that comes before a check. `known` overrides whether the script holds a recognised check
    (a Makefile recipe is judged as a whole, a line at a time). `strict_status` is for a shell
    whose exit status is its last command's (`bash {0}`, `sh {0}`, PowerShell, cmd): a check
    followed by another command, with its status neither read nor chained to the end, is swallowed
    (kind `noerr`). `powershell` makes cmdlets and `$x = ...` leave the status alone.
    A retry, `check || check --lf`, is not a swallow (see `RERUN_FLAGS`).
    """
    segs = split_segments(script)
    literals = [e for e in extra if len(e) >= 6]
    kinds = [
        check_kind(s.text, literals=literals, scripts=scripts, make_check=make_check) for s in segs
    ]
    is_check = [k is not None for k in kinds]
    has_check = any(is_check) if known is None else known

    def check(i: int) -> bool:
        if is_check[i]:
            return True
        if opaque and not has_check:
            return base_segments is None or segs[i].text in base_segments
        return False

    top = _top_level(segs)
    chain_of: list[int] = []
    chain_guard: list[bool] = []
    chain_members: list[list[int]] = []
    for i, seg in enumerate(segs):
        if not chain_members or seg.sep in ("", ";", "\n"):
            chain_members.append([])
            chain_guard.append(False)
        chain_of.append(len(chain_members) - 1)
        chain_members[-1].append(i)
        if seg.sep == "||" and _is_failing_exit(seg.text):
            chain_guard[-1] = True
    reads = [bool(_STATUS_READ.search(s.text)) for s in segs]
    next_read = [-1] * (len(segs) + 1)  # the first statement at or after each one that reads
    for i in range(len(segs) - 1, -1, -1):
        next_read[i] = i if reads[i] else next_read[i + 1]

    out: list[Swallow] = []
    seen: set[tuple[int, str]] = set()

    def add(seg: Seg, kind: str, reason: str) -> None:
        if (seg.line, kind) not in seen:
            seen.add((seg.line, kind))
            out.append(Swallow(seg.line, kind, _short(seg.text), reason))

    state_errexit = errexit
    unguarded = [False] * len(segs)
    last_ok = -1  # the last check that could fail with nothing turning that into a failure
    last_read = -1  # the last statement before the current one that reads a status
    first_exit: Seg | None = None
    exit_reported = False
    for i, seg in enumerate(segs):
        text = seg.text
        if kw := _KEYWORD.match(text):
            text = kw["rest"]
        if seg.sep == "||" and not _CONDITION.match(segs[chain_members[chain_of[i]][0]].text):
            rhs = [text]
            for k in range(i + 1, len(segs)):
                if segs[k].sep != "&&":
                    break
                rhs.append(segs[k].text)
            before = [j for j in chain_members[chain_of[i]] if j < i and check(j)]
            if (
                before
                and not any(_is_failing_exit(r) for r in rhs)
                and not _is_retry(text, kinds[before[-1]], literals, scripts, make_check)
            ):
                add(seg, "or", f"a failure is swallowed by `|| {_short(text)}`")
        elif (_is_zero_exit(text) or _RESET_STATUS.match(text)) and top[i]:
            if last_ok >= 0 and last_ok >= last_read:
                what = text if _RESET_STATUS.match(text) else "exit 0"
                add(seg, "exit0", f"`{what}` after the checks ran, whatever their result")
        elif text in ("true", ":") and seg.sep == ";" and chain_of[i] > 0:
            nxt_sep = segs[i + 1].sep if i + 1 < len(segs) else "\n"
            before = chain_members[chain_of[i] - 1]
            if nxt_sep in ("\n", "") and not state_errexit and any(check(j) for j in before):
                add(seg, "true", f"`; {text}` after a check discards its status")
        elif _SET_PLUS_E.match(text):
            state_errexit = False
            end = next(
                (k for k in range(i + 1, len(segs)) if _SET_MINUS_E.match(segs[k].text)), len(segs)
            )
            for c in range(i + 1, end):
                if not check(c) or _is_zero_exit(segs[c].text):
                    continue
                joined = c + 1 < len(segs) and segs[c + 1].sep in ("||", "&&")
                if next_read[c] < 0 and not joined:
                    add(seg, "set+e", "`set +e` and the exit status is never read afterwards")
                    break
        elif _SET_MINUS_E.match(text):
            state_errexit = True
        if early and top[i] and _is_zero_exit(text):
            if first_exit is None:
                first_exit = seg
        elif early and first_exit is not None and not exit_reported and check(i):
            exit_reported = True
            add(first_exit, "early", "`exit 0` comes before the checks, so none of them runs")
        if check(i) and not _is_zero_exit(text) and not (state_errexit or chain_guard[chain_of[i]]):
            last_ok = i
            unguarded[i] = True
        if reads[i]:
            last_read = i
    if strict_status:
        depth = _block_depth(segs)
        for i, seg in enumerate(segs):
            if not (check(i) and unguarded[i]) or depth[i] or reads[i]:
                continue
            members = chain_members[chain_of[i]]
            if any(segs[j].sep != "&&" for j in members if j > i):
                continue  # a `||` after it: judged above
            after = range(members[-1] + 1, len(segs))
            if not after or next_read[i + 1] >= 0:
                continue
            if powershell and all(_CMDLET.match(segs[j].text) for j in after):
                continue
            nxt = segs[after[0]]
            if (nxt.line, "true") in seen or (nxt.line, "exit0") in seen:
                continue
            label = kinds[i] or "check"
            out.append(
                Swallow(
                    seg.line,
                    "noerr",
                    label,
                    f"the {label} command `{_short(seg.text)}` is followed by more commands and"
                    " this shell's exit status is the last command's",
                )
            )
    return out


def find_swallows(
    script: str,
    extra: Sequence[str] = (),
    *,
    opaque: bool = True,
    errexit: bool = False,
    early: bool = False,
    scripts: Collection[str] = (),
    make_check: Callable[[str], bool | None] | None = None,
    base_segments: Collection[str] | None = None,
    known: bool | None = None,
    strict_status: bool = False,
    powershell: bool = False,
) -> list[Swallow]:
    """The places where `script` can fail a check and still exit 0 (see `_flat_swallows`), also
    inside the body of a `sh -c` or `bash -c` statement, which is a shell of its own."""
    out = _flat_swallows(
        script,
        extra,
        opaque=opaque,
        errexit=errexit,
        early=early,
        scripts=scripts,
        make_check=make_check,
        base_segments=base_segments,
        known=known,
        strict_status=strict_status,
        powershell=powershell,
    )
    for seg in split_segments(script):
        body = shell_c_body(seg.text)
        if body is None:
            continue
        out.extend(
            replace(w, line=seg.line + w.line - 1)
            for w in _flat_swallows(
                body,
                extra,
                opaque=False,
                errexit=False,
                scripts=scripts,
                make_check=make_check,
                strict_status=True,
            )
        )
    return out


def _is_retry(
    text: str,
    kind: str | None,
    literals: Sequence[str],
    scripts: Collection[str],
    make_check: Callable[[str], bool | None] | None,
) -> bool:
    """Whether `text`, the right side of `A || ...`, reruns a check of A's kind unnarrowed (only
    the rerun-the-failures flags): the failure of A is then not swallowed."""
    if kind is None or kind == "policy":
        return False
    if check_kind(text, literals=literals, scripts=scripts, make_check=make_check) != kind:
        return False
    return all(flag in RERUN_FLAGS for _, flag, _ in selection_uses(text))


def _block_depth(segs: Sequence[Seg]) -> list[int]:
    """For each segment: how many `if`/`case`/`for`/`while` blocks it is inside."""
    depth = 0
    out: list[int] = []
    for seg in segs:
        text = seg.text
        kw = _KEYWORD.match(text)
        if kw:
            text = kw["rest"]
        word = _first_word(text)
        if word in ("fi", "esac", "done") and depth:
            depth -= 1
        out.append(depth)
        if word in ("if", "case", "for", "while", "until"):
            depth += 1
    return out


# -- selection flags -------------------------------------------------------------------
def _flag_name(token: str, flags: frozenset[str]) -> str | None:
    """The selection flag `token` is (`-k`, `-k=x`, `-kx`, `--grep=x`), else None."""
    if token in flags:
        return token
    head = token.partition("=")[0]
    if head in flags:
        return head
    if re.match(r"^-[km].+", token) and token[:2] in flags:
        return token[:2]
    return None


def _is_tox(token: str) -> bool:
    return _cmd_name(token) in ("tox", "nox")


def _runner_end(family: str, tokens: list[str]) -> int | None:
    """The index just after the family's runner word, or None when the command is not it."""
    if not tokens:
        return None
    head = _cmd_name(tokens[0])
    if family == "pytest":
        return 1 if head in ("pytest", "py.test") else None
    if family == "mocha":
        return 1 if head == "mocha" else None
    if family == "go":
        if head == "gotestsum":
            return 1
        return 2 if head == "go" and tokens[1:2] == ["test"] else None
    if family == "cargo":
        rest = [t for t in tokens[1:] if not t.startswith("+")]
        return (
            len(tokens) - len(rest) + 1
            if head == "cargo" and rest[:1] in (["test"], ["nextest"])
            else None
        )
    if head in ("jest", "vitest"):
        return 1
    if head in _PKG_MANAGERS:
        for i, tok in enumerate(tokens[1:4], start=1):
            if tok == "test":
                return i + 1
    return None


def selection_uses(command: str, *, retries_ok: bool = False) -> list[tuple[str, str, str]]:
    """`(runner family, flag, the statement)` for each narrowing flag a runner is run with.

    With `retries_ok`, the rerun-the-failures flags (`--lf`) do not count on the right side of
    `check || check --lf`: that reruns what the first run failed.
    """
    found: list[tuple[str, str, str]] = []
    chain: list[str] = []  # the kinds of check on the current `&&` / `||` chain
    for seg in split_segments(command):
        if (body := shell_c_body(seg.text)) is not None:
            found.extend(selection_uses(body, retries_ok=retries_ok))
            continue
        if seg.sep not in ("&&", "||"):
            chain = []
        kind = check_kind(seg.text)
        retry = retries_ok and seg.sep == "||" and kind is not None and kind in chain
        if kind is not None:
            chain.append(kind)
        tokens = command_tokens(seg.text)
        if "|" in tokens:
            tokens = tokens[: tokens.index("|")]
        for family, flags in _ALL_FLAGS.items():
            start = _runner_end(family, tokens)
            if start is not None:
                found.extend(
                    (family, n, seg.text)
                    for t in tokens[start:]
                    if (n := _flag_name(t, flags)) and not (retry and n in RERUN_FLAGS)
                )
        if "--" in tokens and tokens and _is_tox(tokens[0]):
            pytest_flags = _ALL_FLAGS["pytest"]
            found.extend(
                ("pytest", n, seg.text)
                for t in tokens[tokens.index("--") + 1 :]
                if (n := _flag_name(t, pytest_flags)) and not (retry and n in RERUN_FLAGS)
            )
    return found


def selection_flags(command: str) -> list[tuple[str, str]]:
    """`(runner family, flag)` for each narrowing flag a test runner is invoked with.

    Flags before the runner (`python -m pytest`: the `-m`) do not count; for `tox` and `nox`
    the arguments after `--` are the runner's. `--collect-only` and `--passWithNoTests` count:
    the run then passes without running a test.
    """
    return sorted({(family, flag) for family, flag, _ in selection_uses(command)})
