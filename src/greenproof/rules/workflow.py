"""Text-level reading of a GitHub Actions workflow: its steps' `run:` bodies and its triggers.

PyYAML is not a dependency, so this is a small indentation reader for the two shapes the CI
rule needs: `run:` scripts (single-line, `|` and `>` blocks) and the `on:` section (a scalar,
a flow list or map, or a block mapping or list). Anything it cannot read yields None and the
rule that wanted it says nothing rather than guessing: `triggers()` is None for anchors and
aliases, a flow collection that does not close on its line, or anything else it cannot read.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from greenproof.rules._common import split_lines

FILTER_KEYS = (
    "paths",
    "paths-ignore",
    "branches",
    "branches-ignore",
    "tags",
    "tags-ignore",
    "types",
)
Triggers = dict[str, dict[str, list[str]]]


@dataclass(frozen=True)
class Step:
    line: int  # 1-based line of the step's `- `
    run: str  # the script, "" when the step has no `run:`
    run_line: int  # 1-based file line of the script's first line
    shell: str | None = None  # the step's own `shell:`
    uses: str | None = None
    name: str | None = None
    if_: str | None = None  # the step's `if:`
    if_line: int = 0
    coe: str | None = None  # the step's `continue-on-error:`, as written (unquoted)
    coe_line: int = 0
    with_: tuple[tuple[str, str], ...] = ()
    env: tuple[tuple[str, str], ...] = ()
    stop: int = 0  # 1-based line after the step's last line
    job_line: int = 0  # 1-based line of the job's key, 0 when it is not under a job


@dataclass(frozen=True)
class Job:
    name: str
    line: int  # 1-based line of the job's key
    stop: int  # 1-based line after the job's last line
    runners: tuple[str, ...] = ()  # `runs-on:` labels; a `matrix.x` expands to the matrix's values
    uses: str | None = None  # a reusable workflow the job calls
    if_: str | None = None
    if_line: int = 0
    coe: str | None = None
    coe_line: int = 0
    shell: str | None = None  # the job's `defaults.run.shell`


@dataclass(frozen=True)
class Workflow:
    steps: list[Step]
    jobs: list[Job]
    shell: str | None  # the workflow's `defaults.run.shell`

    def job_of(self, step: Step) -> Job | None:
        return next((j for j in self.jobs if j.line == step.job_line), None)

    def shell_of(self, step: Step) -> str | None:
        """The shell a step names, or its job's or the workflow's default."""
        if step.shell:
            return step.shell
        job = self.job_of(step)
        return (job.shell if job else None) or self.shell


def _indent(raw: str) -> int:
    return len(raw) - len(raw.lstrip(" "))


def _content_lines(text: str) -> list[tuple[int, int, str]]:
    """`(line number, indent, text)` of the non-blank, non-comment lines."""
    out: list[tuple[int, int, str]] = []
    for number, raw in enumerate(split_lines(text), start=1):
        stripped = raw.strip()
        if stripped and not stripped.startswith("#"):
            out.append((number, _indent(raw), stripped))
    return out


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def _clean(value: str) -> str:
    """A one-line scalar: without a trailing comment and quotes."""
    value = value.strip()
    if not value.startswith(("'", '"')):
        value = re.sub(r"\s+#.*$", "", value)
    return _unquote(value)


def _blank(raw: str) -> bool:
    return not raw.strip() or raw.lstrip().startswith("#")


def _block_after(lines: list[str], at: int, stop: int, indent: int) -> list[str]:
    """The lines under line index `at` that are indented more than `indent`."""
    out: list[str] = []
    for k in range(at + 1, stop):
        raw = lines[k]
        if not _blank(raw) and _indent(raw) <= indent:
            break
        out.append(raw)
    return out


# -- steps -----------------------------------------------------------------------------
_STEPS_KEY = re.compile(r"^(\s*)steps\s*:\s*(?:&([\w.-]+)\s*)?(?:#.*)?$")
_STEPS_ALIAS = re.compile(r"^(\s*)steps\s*:\s*\*([\w.-]+)\s*(?:#.*)?$")


def parse(text: str) -> Workflow:
    """The workflow's steps and jobs, read once."""
    lines = split_lines(text)
    jobs = _jobs(lines)
    return Workflow(_steps(lines, jobs), jobs, _workflow_shell(lines))


def steps(text: str) -> list[Step]:
    """Every step under a `steps:` key, with its `run:` script."""
    return parse(text).steps


def _job_line(jobs: list[Job], line: int) -> int:
    return next((j.line for j in jobs if j.line <= line < j.stop), 0)


def _steps(lines: list[str], jobs: list[Job]) -> list[Step]:
    out: list[Step] = []
    anchored: dict[str, list[Step]] = {}
    i = 0
    while i < len(lines):
        alias = _STEPS_ALIAS.match(lines[i])
        if alias and alias.group(2) in anchored:
            job_line = _job_line(jobs, i + 1)
            out.extend(replace(st, job_line=job_line) for st in anchored[alias.group(2)])
            i += 1
            continue
        m = _STEPS_KEY.match(lines[i])
        i += 1
        if not m:
            continue
        key_indent = len(m.group(1))
        j = i
        while j < len(lines) and (not lines[j].strip() or lines[j].lstrip().startswith("#")):
            j += 1
        if j >= len(lines) or not lines[j].lstrip().startswith("-"):
            continue
        item_indent = _indent(lines[j])
        if item_indent < key_indent:
            continue
        starts: list[int] = []
        end = j
        while end < len(lines):
            raw = lines[end]
            if raw.strip() and not raw.lstrip().startswith("#"):
                ind = _indent(raw)
                if ind < item_indent or (ind == item_indent and not raw.lstrip().startswith("-")):
                    break
                if ind == item_indent:
                    starts.append(end)
            end += 1
        found: list[Step] = []
        for n, start in enumerate(starts):
            stop = starts[n + 1] if n + 1 < len(starts) else end
            found.append(_step(lines, start, stop, _job_line(jobs, start + 1)))
        out.extend(found)
        if m.group(2):
            anchored[m.group(2)] = found
        i = end
    return out


def _mapping(
    lines: list[str], at: int, stop: int, col: int, rest: str
) -> tuple[tuple[str, str], ...]:
    """The `key: value` pairs of a `with:` or `env:` mapping whose key is on line index `at`."""
    rest = rest.strip()
    if rest.startswith("{"):
        try:
            value, _ = _flow(rest)
        except _Unreadable:
            return ()
        if isinstance(value, dict):
            return tuple((str(k), str(v)) for k, v in value.items())
        return ()
    if rest and not rest.startswith("#"):
        return ()
    items: list[tuple[str, str]] = []
    child: int | None = None
    for k in range(at + 1, stop):
        raw = lines[k]
        if _blank(raw):
            continue
        ind = _indent(raw)
        if ind <= col:
            break
        child = ind if child is None else child
        if ind == child and (m := re.match(r"^\s*([^\s:#][^:]*?)\s*:\s*(.*)$", raw)):
            items.append((_unquote(m.group(1)), _clean(m.group(2))))
    return tuple(items)


def _step(lines: list[str], start: int, stop: int, job_line: int = 0) -> Step:
    first = lines[start]
    marker = re.match(r"^(\s*-\s+)", first)
    col = len(marker.group(1)) if marker else _indent(first) + 2
    run: tuple[str, int] | None = None
    shell: str | None = None
    fields: dict[str, tuple[str, int]] = {}
    maps: dict[str, tuple[tuple[str, str], ...]] = {}
    for k in range(start, stop):
        raw = lines[k]
        body = raw[col:] if k == start and marker else raw
        if k != start and _indent(raw) != col:
            continue
        if run is None and (m := re.match(r"^\s*run\s*:\s*(.*)$", body)):
            run = _run_value(lines, k, stop, col, m.group(1))
        elif m := re.match(r"^\s*shell\s*:\s*(.+)$", body):
            shell = _unquote(re.sub(r"\s+#.*$", "", m.group(1)))
        elif m := re.match(r"^\s*(uses|name|if|continue-on-error)\s*:\s*(.*)$", body):
            fields.setdefault(m.group(1), (_clean(m.group(2)), k + 1))
        elif m := re.match(r"^\s*(with|env)\s*:\s*(.*)$", body):
            maps.setdefault(m.group(1), _mapping(lines, k, stop, col, m.group(2)))
    return Step(
        start + 1,
        *(run or ("", start + 1)),
        shell,
        uses=fields.get("uses", (None, 0))[0],
        name=fields.get("name", (None, 0))[0],
        if_=fields.get("if", (None, 0))[0],
        if_line=fields.get("if", ("", 0))[1],
        coe=fields.get("continue-on-error", (None, 0))[0],
        coe_line=fields.get("continue-on-error", ("", 0))[1],
        with_=maps.get("with", ()),
        env=maps.get("env", ()),
        stop=stop + 1,
        job_line=job_line,
    )


# -- jobs ------------------------------------------------------------------------------
_KEY = re.compile(r"^\s*([\w.-]+)\s*:\s*(.*)$")


def _shell_in(block: list[str]) -> str | None:
    for raw in block:
        if m := re.match(r"^\s*shell\s*:\s*(.+)$", raw):
            return _clean(m.group(1))
    return None


def _workflow_shell(lines: list[str]) -> str | None:
    for k, raw in enumerate(lines):
        if re.match(r"^defaults\s*:\s*(?:#.*)?$", raw):
            return _shell_in(_block_after(lines, k, len(lines), 0))
    return None


def _list_items(value: str) -> list[str]:
    value = value.strip()
    if value.startswith("["):
        try:
            parsed, _ = _flow(value)
        except _Unreadable:
            return []
        return [str(v) for v in parsed] if isinstance(parsed, list) else []
    return [_clean(value)] if value else []


def _matrix_values(block: list[str], key: str) -> list[str]:
    """Every value the matrix gives `key`: `key: [a, b]`, a block list or `include` rows."""
    out: list[str] = []
    for n, raw in enumerate(block):
        m = re.match(rf"^(\s*)(?:-\s+)?{re.escape(key)}\s*:\s*(.*)$", raw)
        if not m:
            continue
        if m.group(2).strip() and not m.group(2).strip().startswith("#"):
            out.extend(_list_items(re.sub(r"\s+#.*$", "", m.group(2))))
            continue
        for later in block[n + 1 :]:
            if _blank(later):
                continue
            if _indent(later) < len(m.group(1)) or (
                _indent(later) == len(m.group(1)) and not later.lstrip().startswith("-")
            ):
                break
            if later.lstrip().startswith("-"):
                out.append(_clean(later.lstrip()[1:]))
    return out


_MATRIX_REF = re.compile(r"\$\{\{\s*matrix\.([\w-]+)\s*\}\}")


def _runners(value: str, strategy: list[str]) -> tuple[str, ...]:
    if m := _MATRIX_REF.search(value):
        return tuple(_matrix_values(strategy, m.group(1)))
    return (value,) if value else ()


def _jobs(lines: list[str]) -> list[Job]:
    start = next((k for k, raw in enumerate(lines) if re.match(r"^jobs\s*:", raw)), None)
    if start is None:
        return []
    keys: list[tuple[int, str]] = []
    job_indent: int | None = None
    end = len(lines)
    for k in range(start + 1, len(lines)):
        raw = lines[k]
        if _blank(raw):
            continue
        if _indent(raw) == 0:
            end = k
            break
        job_indent = _indent(raw) if job_indent is None else job_indent
        if _indent(raw) == job_indent:
            m = re.match(r"^\s*(\"[^\"]*\"|'[^']*'|[^\s:#][^:]*?)\s*:", raw)
            keys.append((k, _unquote(m.group(1)) if m else raw.strip()))
    out: list[Job] = []
    for n, (k, name) in enumerate(keys):
        stop = keys[n + 1][0] if n + 1 < len(keys) else end
        out.append(_job(lines, k, stop, name))
    return out


def _job(lines: list[str], at: int, stop: int, name: str) -> Job:
    child: int | None = None
    fields: dict[str, tuple[str, int]] = {}
    shell: str | None = None
    strategy: list[str] = []
    runs_on_block: list[str] = []
    for k in range(at + 1, stop):
        raw = lines[k]
        if _blank(raw):
            continue
        child = _indent(raw) if child is None else child
        if _indent(raw) != child or not (m := _KEY.match(raw)):
            continue
        key, value = m.group(1), m.group(2)
        if key in ("runs-on", "uses", "if", "continue-on-error"):
            fields.setdefault(key, (_clean(value), k + 1))
            if key == "runs-on" and not value.strip():
                runs_on_block = _block_after(lines, k, stop, child)
        elif key == "defaults":
            shell = _shell_in(_block_after(lines, k, stop, child))
        elif key == "strategy":
            strategy = _block_after(lines, k, stop, child)
    runs_on = fields.get("runs-on", ("", 0))[0]
    if runs_on_block:
        runs_on = " ".join(ln.strip().lstrip("-").strip() for ln in runs_on_block)
    return Job(
        name,
        at + 1,
        stop + 1,
        _runners(runs_on, strategy),
        uses=fields.get("uses", (None, 0))[0],
        if_=fields.get("if", (None, 0))[0],
        if_line=fields.get("if", ("", 0))[1],
        coe=fields.get("continue-on-error", (None, 0))[0],
        coe_line=fields.get("continue-on-error", ("", 0))[1],
        shell=shell,
    )


def default_shell(text: str, at: int) -> str | None:
    """`defaults.run.shell` of the job that holds 1-based line `at`, else the workflow's."""
    wf = parse(text)
    job = next((j for j in wf.jobs if j.line <= at < j.stop), None)
    return (job.shell if job else None) or wf.shell


def _fold(body: list[str]) -> str:
    """A folded scalar (`>`): lines join with a space; blank lines and more-indented lines keep
    their breaks, as YAML folds them."""
    out: list[str] = []
    previous: str | None = None
    blanks = 0
    for b in body:
        if not b.strip():
            blanks += 1
            continue
        if previous is None:
            out.append(b)
        else:
            plain = not b[0].isspace() and not previous[0].isspace()
            if blanks:
                out.append("\n" * (blanks if plain else blanks + 1) + b)
            else:
                out.append((" " if plain else "\n") + b)
        previous, blanks = b, 0
    return "".join(out)


def _run_value(lines: list[str], at: int, stop: int, col: int, value: str) -> tuple[str, int]:
    """The script of a `run:` at line index `at` whose key sits at column `col`, and its line."""
    value = value.strip()
    if not value.startswith(("'", '"')):
        value = re.sub(r"\s+#.*$", "", value)
    if value[:1] in ("|", ">"):
        body: list[str] = []
        k = at + 1
        while k < stop and (not lines[k].strip() or _indent(lines[k]) > col):
            body.append(lines[k])
            k += 1
        while body and not body[-1].strip():
            body.pop()
        pad = min((_indent(b) for b in body if b.strip()), default=0)
        if value[0] == ">":
            return _fold([b[pad:] for b in body]), at + 2
        return "\n".join(b[pad:] for b in body), at + 2
    parts = [_unquote(value)]
    k = at + 1
    while k < stop and lines[k].strip() and _indent(lines[k]) > col:  # plain multi-line scalar
        parts.append(lines[k].strip())
        k += 1
    return " ".join(parts), at + 1


# -- triggers --------------------------------------------------------------------------
class _Unreadable(Exception):
    """The `on:` section uses YAML this reader does not follow."""


def _flow(text: str, i: int = 0) -> tuple[object, int]:
    """A flow scalar, `[...]` or `{...}` starting at `text[i]`; the value and the next index."""
    n = len(text)
    while i < n and text[i] in " \t\n":
        i += 1
    if i < n and text[i] == "[":
        items: list[object] = []
        i += 1
        while i < n:
            while i < n and text[i] in " \t\n,":
                i += 1
            if i < n and text[i] == "]":
                return items, i + 1
            value, j = _flow(text, i)
            i = j if j > i else i + 1
            items.append(value)
        raise _Unreadable("flow list not closed")
    if i < n and text[i] == "{":
        mapping: dict[str, object] = {}
        i += 1
        while i < n:
            while i < n and text[i] in " \t\n,":
                i += 1
            if i < n and text[i] == "}":
                return mapping, i + 1
            key, j = _flow_scalar(text, i, ":,}")
            i = j if j > i else i + 1
            while i < n and text[i] in " \t":
                i += 1
            item: object = None
            if i < n and text[i] == ":":
                item, i = _flow(text, i + 1)
            mapping[key] = item
        raise _Unreadable("flow map not closed")
    return _flow_scalar(text, i, ",]}")


def _flow_scalar(text: str, i: int, stops: str) -> tuple[str, int]:
    n = len(text)
    if i < n and text[i] in "'\"":
        end = text.find(text[i], i + 1)
        end = n - 1 if end < 0 else end
        return text[i + 1 : end], end + 1
    start = i
    while i < n and text[i] not in stops:
        i += 1
    return text[start:i].strip(), i


def _value(text: str) -> object:
    text = re.sub(r"\s+#.*$", "", text.strip())
    if text[:1] in ("&", "*", "!", "|", ">"):
        raise _Unreadable("anchor, alias, tag or block scalar")
    if text[:1] in ("[", "{"):
        return _flow(text)[0]
    return _unquote(text)


def _block(items: list[tuple[int, str]], pos: int) -> tuple[object, int]:
    """A block mapping or sequence starting at `items[pos]`; the value and the next index."""
    indent, first = items[pos]
    if first.startswith("-"):
        seq: list[object] = []
        while pos < len(items) and items[pos][0] == indent and items[pos][1].startswith("-"):
            seq.append(_value(items[pos][1][1:]))
            pos += 1
        return seq, pos
    mapping: dict[str, object] = {}
    while pos < len(items) and items[pos][0] == indent and not items[pos][1].startswith("-"):
        key, colon, rest = items[pos][1].partition(":")
        if not colon or key.strip() == "<<":
            raise _Unreadable("not a key")
        pos += 1
        rest = rest.strip()
        if rest and not rest.startswith("#"):
            mapping[_unquote(key)] = _value(rest)
        elif pos < len(items) and (
            items[pos][0] > indent or (items[pos][0] == indent and items[pos][1].startswith("-"))
        ):
            mapping[_unquote(key)], pos = _block(items, pos)
        else:
            mapping[_unquote(key)] = None
    return mapping, pos


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str)]
    return []


def triggers(text: str) -> tuple[Triggers, int] | None:
    """The workflow's events with their path and branch filters, and the line of `on:`.

    An absent `on:` is an empty set of events; None means the section could not be read
    reliably (an anchor or alias, a flow collection that does not close on its line, or
    anything else this reader does not follow), and a rule that wanted it says nothing.
    """
    lines = _content_lines(text)
    for n, (number, indent, content) in enumerate(lines):
        m = re.match(r"^(?:on|\"on\"|'on')\s*:(.*)$", content)
        if indent != 0 or not m:
            continue
        rest = m.group(1).strip()
        raw: object
        if rest and not rest.startswith("#"):
            try:
                raw = _value(rest)
            except _Unreadable:
                return None
        else:
            body = [(ind, c) for _, ind, c in lines[n + 1 :]]
            end = next((k for k, (ind, _) in enumerate(body) if ind == 0), len(body))
            body = body[:end]
            if not body:
                return {}, number
            try:
                raw, used = _block(body, 0)
            except _Unreadable:
                return None
            if used < len(body):
                return None
        return _to_triggers(raw), number
    return {}, 1


def _to_triggers(raw: object) -> Triggers:
    out: Triggers = {}
    if isinstance(raw, str):
        out[raw] = {}
    elif isinstance(raw, list):
        for event in raw:
            if isinstance(event, str):
                out[event] = {}
    elif isinstance(raw, dict):
        for event, spec in raw.items():
            filters: dict[str, list[str]] = {}
            if isinstance(spec, dict):
                for key in FILTER_KEYS:
                    if key in spec:
                        filters[key] = _strings(spec[key])
            out[str(event)] = filters
    return out
