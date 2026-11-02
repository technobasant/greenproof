"""Text-level reading of a GitHub Actions workflow: its steps' `run:` bodies and its triggers.

PyYAML is not a dependency, so this is a small indentation reader for the two shapes the CI
rule needs: `run:` scripts (single-line, `|` and `>` blocks) and the `on:` section (a scalar,
a flow list or map, or a block mapping or list). Anything it cannot read yields None and the
rule that wanted it says nothing rather than guessing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from greenproof.rules._common import split_lines

FILTER_KEYS = ("paths", "paths-ignore", "branches-ignore")
Triggers = dict[str, dict[str, list[str]]]


@dataclass(frozen=True)
class Step:
    line: int  # 1-based line of the step's `- `
    run: str  # the script, "" when the step has no `run:`
    run_line: int  # 1-based file line of the script's first line


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


# -- steps -----------------------------------------------------------------------------
def steps(text: str) -> list[Step]:
    """Every step under a `steps:` key, with its `run:` script."""
    lines = split_lines(text)
    out: list[Step] = []
    i = 0
    while i < len(lines):
        m = re.match(r"^(\s*)steps\s*:\s*(?:#.*)?$", lines[i])
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
        for n, start in enumerate(starts):
            stop = starts[n + 1] if n + 1 < len(starts) else end
            out.append(_step(lines, start, stop))
        i = end
    return out


def _step(lines: list[str], start: int, stop: int) -> Step:
    first = lines[start]
    marker = re.match(r"^(\s*-\s+)", first)
    col = len(marker.group(1)) if marker else _indent(first) + 2
    for k in range(start, stop):
        raw = lines[k]
        body = raw[col:] if k == start and marker else raw
        if (k == start or _indent(raw) == col) and (m := re.match(r"^\s*run\s*:\s*(.*)$", body)):
            return Step(start + 1, *_run_value(lines, k, stop, col, m.group(1)))
    return Step(start + 1, "", start + 1)


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
        return "\n".join(b[pad:] for b in body), at + 2
    parts = [_unquote(value)]
    k = at + 1
    while k < stop and lines[k].strip() and _indent(lines[k]) > col:  # plain multi-line scalar
        parts.append(lines[k].strip())
        k += 1
    return " ".join(parts), at + 1


# -- triggers --------------------------------------------------------------------------
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
        return items, i
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
        return mapping, i
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
        key, _, rest = items[pos][1].partition(":")
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

    An absent `on:` is an empty set of events; None means the section could not be read.
    """
    lines = _content_lines(text)
    for n, (number, indent, content) in enumerate(lines):
        m = re.match(r"^(?:on|\"on\"|'on')\s*:(.*)$", content)
        if indent != 0 or not m:
            continue
        rest = m.group(1).strip()
        raw: object
        if rest and not rest.startswith("#"):
            raw = _value(rest)
        else:
            body = [(ind, c) for _, ind, c in lines[n + 1 :]]
            end = next((k for k, (ind, _) in enumerate(body) if ind == 0), len(body))
            body = body[:end]
            if not body:
                return {}, number
            raw = _block(body, 0)[0]
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
