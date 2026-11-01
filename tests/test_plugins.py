"""The plugin for Claude Code, Codex and Cursor (SPEC 6.7): manifests, hooks, marketplaces.

Every vendor format below was written against its primary source, fetched 2026-11-01:

Claude Code
    https://code.claude.com/docs/en/plugins/manifest-reference      plugin.json fields
    https://code.claude.com/docs/en/plugins/marketplace-reference.md  marketplace.json fields
    https://code.claude.com/docs/en/hooks.md                         hook file shape, the
        SessionStart sources (startup, resume, clear, compact, fork), command `timeout`
    https://code.claude.com/docs/en/plugins/publish.md#submit-to-anthropics-directory
        directory limits: README of at least 40 words, LICENSE, kebab-case name, fewer than
        512 files each under 256 KiB, square PNG icon at `.claude-plugin/icon.png`
    `claude plugin validate --strict` (Claude Code 2.1.310), run by hand on the plugin
    directory and on the repository root; it needs the claude binary, so it is not run here.

Codex (openai/codex at commit 54201ea65a3501d09a87c776376a0c16aa39db70)
    codex-rs/core-plugins/src/manifest.rs      RawPluginManifest and
        RawPluginManifestInterface: the camelCase fields, `./`-prefixed paths
    codex-rs/core-plugins/src/marketplace.rs   RawMarketplaceManifest, the `source` object
        (`{"source": "local", "path": ...}`), the policy enums, manifest probe order
        `.codex-plugin`, `.claude-plugin`, `.cursor-plugin`
    codex-rs/core-plugins/src/loader.rs        DEFAULT_HOOKS_CONFIG_FILE is read only when
        the manifest declares no `hooks`, so a declared `hooks` file is the only one loaded
    codex-rs/hooks/src/events/session_start.rs SessionStart sources, including `fork`
    The Agent Plugins schema (https://agent-plugins.org/schemas/1.0.0/plugin.schema.json)
    validates a root `plugin.json`, not `.codex-plugin/plugin.json`, so it does not apply.

Cursor (cursor/plugin-template at commit 46216072ac5750f782f95bb325b4d12b7c3ae9c9)
    .cursor-plugin/marketplace.json and plugins/starter-advanced/.cursor-plugin/plugin.json
        the shapes; scripts/validate-template.mjs the rules, reproduced by
        `test_cursor_template_validator_rules` because running downloaded code is not
        allowed here
    https://cursor.com/docs/agent/hooks   hooks.json: `version: 1`, flat event lists,
        `timeout` in seconds, command hooks only for plugins
    https://cursor.com/docs/plugins       local plugins, team marketplaces, submission

Not verified from a primary source: that Cursor reads `displayName` (the template's own
manifest carries it, the documentation does not mention it).
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import struct
import sys
import zlib
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from greenproof import __version__
from greenproof.init.hooks import claude as claude_hooks
from greenproof.init.hooks import codex as codex_hooks
from greenproof.init.hooks import cursor as cursor_hooks

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "plugins" / "greenproof"
AGENTS = ("claude", "codex", "cursor")
DESCRIPTION = (
    "Done gate for coding agents: when the agent stops, greenproof runs your checks and "
    "blocks deleted, skipped or weakened tests."
)
CLAUDE_MATCHER = "startup|resume|clear|compact|fork"
CODEX_MATCHER = "startup|resume|clear|compact"
STOP_TIMEOUT = 3600
PINNED = f"uvx greenproof@{__version__}"


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def frontmatter(text: str) -> dict[str, str]:
    """The frontmatter reader of the Cursor template validator: `key: value` lines."""
    assert text.startswith("---\n")
    block = text[4 : text.index("\n---\n", 4)]
    fields: dict[str, str] = {}
    for line in block.split("\n"):
        if line.strip() and not line.strip().startswith("#") and ":" in line:
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()
    return fields


# --- hook files -----------------------------------------------------------------------------


@pytest.mark.parametrize("agent", AGENTS)
def test_hook_files_pin_the_running_version(agent: str) -> None:
    text = (PLUGIN / "hooks" / f"{agent}.json").read_text(encoding="utf-8")
    commands = re.findall(r'"command": "([^"]+)"', text)
    assert commands == [
        f"{PINNED} check --hook {agent}",
        f"{PINNED} session-start --hook {agent}",
    ]


def test_claude_hooks_are_the_init_hooks_with_the_plugin_command() -> None:
    expected = json.loads(claude_hooks.render(None, PINNED, STOP_TIMEOUT))
    expected["hooks"]["SessionStart"][0]["matcher"] = CLAUDE_MATCHER
    assert read_json(PLUGIN / "hooks" / "claude.json") == expected


def test_codex_hooks_are_the_init_hooks_with_the_plugin_command() -> None:
    expected = json.loads(codex_hooks.render(None, PINNED, STOP_TIMEOUT))
    assert expected["hooks"]["SessionStart"][0]["matcher"] == CODEX_MATCHER
    assert read_json(PLUGIN / "hooks" / "codex.json") == expected


def test_cursor_hooks_are_the_init_hooks_with_the_plugin_command() -> None:
    expected = json.loads(cursor_hooks.render(None, PINNED, STOP_TIMEOUT))
    assert read_json(PLUGIN / "hooks" / "cursor.json") == expected
    assert expected["version"] == 1
    assert set(expected["hooks"]) == {"stop", "sessionStart"}


def test_no_subagent_hooks_and_the_stop_timeout_is_an_hour() -> None:
    for agent in AGENTS:
        text = (PLUGIN / "hooks" / f"{agent}.json").read_text(encoding="utf-8")
        assert "ubagent" not in text
        assert text.count(f'"timeout": {STOP_TIMEOUT}') == 1


def test_hook_files_end_with_a_newline_and_use_two_space_indent() -> None:
    for agent in AGENTS:
        text = (PLUGIN / "hooks" / f"{agent}.json").read_text(encoding="utf-8")
        assert text.endswith("}\n")
        assert json.dumps(json.loads(text), indent=2, ensure_ascii=False) + "\n" == text


# --- no vendor loads another vendor's hooks -------------------------------------------------


def test_no_default_hooks_file_so_no_agent_loads_another_agents_protocol() -> None:
    # Claude Code and Codex read hooks/hooks.json when the manifest names no hooks file
    # (Codex: loader.rs DEFAULT_HOOKS_CONFIG_FILE); Cursor's template uses the same path.
    assert not (PLUGIN / "hooks" / "hooks.json").exists()
    assert sorted(p.name for p in (PLUGIN / "hooks").iterdir()) == [f"{a}.json" for a in AGENTS]


def test_each_manifest_names_its_own_hooks_file() -> None:
    claude = read_json(PLUGIN / ".claude-plugin" / "plugin.json")
    codex = read_json(PLUGIN / ".codex-plugin" / "plugin.json")
    cursor = read_json(PLUGIN / ".cursor-plugin" / "plugin.json")
    assert claude["hooks"] == "./hooks/claude.json"
    assert codex["hooks"] == "./hooks/codex.json"
    assert cursor["hooks"] == "./hooks/cursor.json"
    for agent, manifest in zip(AGENTS, (claude, codex, cursor), strict=True):
        assert f"--hook {agent}" in (PLUGIN / manifest["hooks"]).read_text(encoding="utf-8")


def test_other_vendor_defaults_are_absent() -> None:
    # Files an agent would load by default from a plugin directory, beyond its manifest.
    for name in (".mcp.json", "mcp.json", "plugin.json", "hooks.json", ".app.json"):
        assert not (PLUGIN / name).exists()
    for directory in ("commands", "agents", "rules", "monitors"):
        assert not (PLUGIN / directory).exists()


# --- manifests ------------------------------------------------------------------------------


@pytest.mark.parametrize("vendor", ["claude", "codex", "cursor"])
def test_manifest_basics(vendor: str) -> None:
    manifest = read_json(PLUGIN / f".{vendor}-plugin" / "plugin.json")
    assert manifest["name"] == "greenproof"
    assert manifest["version"] == __version__
    assert manifest["description"] == DESCRIPTION
    assert manifest["keywords"]
    for key in ("author", "homepage", "repository", "license"):
        if key in manifest:
            if key == "author":
                assert manifest[key]["name"] == "Basant Bhattarai"
            elif key == "license":
                assert manifest[key] == "Apache-2.0"
            else:
                assert manifest[key] == "https://github.com/technobasant/greenproof"


@pytest.mark.parametrize("vendor", ["claude", "cursor"])
def test_manifest_carries_author_homepage_repository_license(vendor: str) -> None:
    manifest = read_json(PLUGIN / f".{vendor}-plugin" / "plugin.json")
    assert manifest["author"]["name"] == "Basant Bhattarai"
    assert manifest["homepage"] == manifest["repository"]
    assert manifest["homepage"] == "https://github.com/technobasant/greenproof"
    assert manifest["license"] == "Apache-2.0"


# Claude Code: manifest-reference. Optional metadata fields and component paths it defines.
CLAUDE_FIELDS = {
    "$schema", "name", "version", "description", "author", "homepage", "repository",
    "license", "keywords", "skills", "commands", "agents", "hooks", "mcpServers",
    "lspServers", "outputStyles", "themes", "monitors", "channels", "userConfig",
    "dependencies", "icon", "displayName",
}  # fmt: skip


def test_claude_manifest_uses_only_documented_fields() -> None:
    manifest = read_json(PLUGIN / ".claude-plugin" / "plugin.json")
    assert set(manifest) <= CLAUDE_FIELDS
    assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", manifest["name"])
    assert (PLUGIN / manifest["hooks"]).is_file()
    assert (PLUGIN / "skills" / "greenproof" / "SKILL.md").is_file()  # the default `skills/`


# Codex: manifest.rs. RawPluginManifest and RawPluginManifestInterface, serde camelCase.
CODEX_FIELDS = {
    "name", "version", "description", "keywords", "skills", "mcpServers", "apps", "hooks",
    "interface", "extensions", "commands",
}  # fmt: skip
CODEX_INTERFACE_FIELDS = {
    "displayName", "shortDescription", "longDescription", "developerName", "category",
    "capabilities", "websiteUrl", "websiteURL", "privacyPolicyUrl", "privacyPolicyURL",
    "termsOfServiceUrl", "termsOfServiceURL", "defaultPrompt", "brandColor", "composerIcon",
    "logo", "logoDark", "screenshots",
}  # fmt: skip


def test_codex_manifest_matches_the_rust_structs() -> None:
    manifest = read_json(PLUGIN / ".codex-plugin" / "plugin.json")
    assert set(manifest) <= CODEX_FIELDS
    interface = manifest["interface"]
    assert set(interface) <= CODEX_INTERFACE_FIELDS
    # Paths must start with `./` and stay under the plugin root (manifest.rs: "path must
    # start with `./` relative to plugin root"); every one must exist.
    for value in (
        manifest["skills"],
        manifest["hooks"],
        interface["composerIcon"],
        interface["logo"],
    ):
        assert value.startswith("./")
        assert ".." not in Path(value).parts
        assert (PLUGIN / value).exists()
    assert (PLUGIN / manifest["skills"]).is_dir()
    assert interface["displayName"]
    assert interface["shortDescription"]
    assert interface["longDescription"]
    assert interface["developerName"] == "Basant Bhattarai"
    assert interface["category"]
    assert isinstance(interface["capabilities"], list)
    assert all(isinstance(c, str) for c in interface["capabilities"])
    assert isinstance(interface["defaultPrompt"], str)
    assert re.fullmatch(r"#[0-9a-fA-F]{6}", interface["brandColor"])
    assert interface["websiteURL"] == "https://github.com/technobasant/greenproof"


# Cursor: plugin-template's starter manifest fields plus the hooks and skills paths its
# validator resolves (scripts/validate-template.mjs `manifestFields`).
CURSOR_FIELDS = {
    "name", "displayName", "version", "description", "author", "homepage", "repository",
    "license", "keywords", "logo", "rules", "skills", "agents", "commands", "hooks", "mcpServers",
}  # fmt: skip


def test_cursor_manifest_uses_only_template_fields() -> None:
    manifest = read_json(PLUGIN / ".cursor-plugin" / "plugin.json")
    assert set(manifest) <= CURSOR_FIELDS
    assert manifest["logo"] == "assets/logo.png"
    assert (PLUGIN / manifest["logo"]).is_file()


# --- marketplaces ---------------------------------------------------------------------------


def test_claude_marketplace() -> None:
    market = read_json(ROOT / ".claude-plugin" / "marketplace.json")
    assert market["name"] == "greenproof"
    assert market["owner"]["name"] == "Basant Bhattarai"
    (entry,) = market["plugins"]
    assert entry["name"] == "greenproof"
    assert entry["source"] == "./plugins/greenproof"
    assert entry["description"] == DESCRIPTION
    # The version lives in plugin.json only: Claude Code lets plugin.json win over the
    # marketplace entry, so a second copy could only disagree.
    assert "version" not in entry
    assert (ROOT / entry["source"] / ".claude-plugin" / "plugin.json").is_file()


def test_codex_marketplace() -> None:
    market = read_json(ROOT / ".agents" / "plugins" / "marketplace.json")
    assert set(market) <= {"name", "interface", "plugins"}
    assert market["name"] == "greenproof"
    (entry,) = market["plugins"]
    assert entry["name"] == "greenproof"
    # marketplace.rs: RawMarketplaceManifestPluginSourceObject, tag `source`, `local` takes
    # a `path`.
    assert entry["source"] == {"source": "local", "path": "./plugins/greenproof"}
    assert (ROOT / entry["source"]["path"] / ".codex-plugin" / "plugin.json").is_file()
    assert entry["policy"]["installation"] in {"NOT_AVAILABLE", "AVAILABLE", "INSTALLED_BY_DEFAULT"}
    assert entry["policy"]["authentication"] in {"ON_INSTALL", "ON_USE"}
    assert entry["category"]


def test_cursor_template_validator_rules() -> None:
    """The checks of cursor/plugin-template's scripts/validate-template.mjs, in Python."""
    plugin_name = re.compile(r"[a-z0-9]([a-z0-9.-]*[a-z0-9])?")
    market_name = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?")
    market = read_json(ROOT / ".cursor-plugin" / "marketplace.json")
    assert market_name.fullmatch(market["name"])
    assert market["owner"]["name"]
    assert market["plugins"]
    assert "pluginRoot" not in market.get("metadata", {})
    seen: set[str] = set()
    for entry in market["plugins"]:
        assert plugin_name.fullmatch(entry["name"])
        assert entry["name"] not in seen
        seen.add(entry["name"])
        source = entry["source"]
        assert isinstance(source, str)
        assert not Path(source).is_absolute()
        assert ".." not in Path(source).parts
        plugin_dir = ROOT / source
        assert plugin_dir.is_dir()
        manifest = read_json(plugin_dir / ".cursor-plugin" / "plugin.json")
        assert plugin_name.fullmatch(manifest["name"])
        assert manifest["name"] == entry["name"]
        for field in ("logo", "rules", "skills", "agents", "commands", "hooks", "mcpServers"):
            value = manifest.get(field)
            for path in [value] if isinstance(value, str) else []:
                assert not Path(path).is_absolute()
                assert ".." not in Path(path).parts
                assert (plugin_dir / path).exists(), f"{field}: {path}"
        for skill in (plugin_dir / "skills").rglob("SKILL.md"):
            fields = frontmatter(skill.read_text(encoding="utf-8"))
            assert fields["name"]
            assert fields["description"]


# --- Anthropic directory limits and the skill -----------------------------------------------


def test_directory_limits() -> None:
    files = [p for p in PLUGIN.rglob("*") if p.is_file()]
    assert len(files) < 512
    assert all(p.stat().st_size < 256 * 1024 for p in files)
    assert (PLUGIN / "LICENSE").read_bytes() == (ROOT / "LICENSE").read_bytes()
    assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", PLUGIN.name)


def png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def test_icon_is_a_square_512_png_and_the_logo_is_the_same_image() -> None:
    icon = PLUGIN / ".claude-plugin" / "icon.png"
    assert png_size(icon) == (512, 512)
    assert icon.stat().st_size < 256 * 1024
    assert icon.read_bytes() == (PLUGIN / "assets" / "logo.png").read_bytes()


def test_readme_describes_what_runs() -> None:
    text = (PLUGIN / "README.md").read_text(encoding="utf-8")
    assert len(text.split()) >= 40
    for needle in (
        f"{PINNED} check --hook AGENT",
        f"{PINNED} session-start --hook AGENT",
        "claude plugin marketplace add technobasant/greenproof",
        "claude plugin install greenproof@greenproof",
        "codex plugin marketplace add technobasant/greenproof",
        "init --agents none",
        "no telemetry",
        "uv on PATH",
    ):
        assert needle in text


def test_skill() -> None:
    text = (PLUGIN / "skills" / "greenproof" / "SKILL.md").read_text(encoding="utf-8")
    assert len(text.splitlines()) <= 60
    fields = frontmatter(text)
    assert fields["name"] == "greenproof"
    assert "greenproof: not done" in fields["description"]
    for needle in (
        f"{PINNED} check",
        f"{PINNED} explain RULE_ID",
        f"{PINNED} init --agents none",
        "Do not run `greenproof accept`",
        ".greenproof.toml",
    ):
        assert needle in text


def test_pinned_commands_name_no_other_version() -> None:
    for path in (PLUGIN / "skills/greenproof/SKILL.md", PLUGIN / "README.md"):
        pins = set(re.findall(r"greenproof@(\d[\w.]*)", path.read_text(encoding="utf-8")))
        assert pins == {__version__}


def test_versions_in_step_across_every_target() -> None:
    bump = load_script("bump_version")
    for target in bump.targets():
        text = (ROOT / target.path).read_text(encoding="utf-8")
        found = target.pattern.findall(text)
        assert found, target.path
        versions = {
            m.group(0).removeprefix("greenproof@")
            if target.pattern is bump.PIN
            else re.search(r"\d+\.\d+\.\d+", m.group(0)).group(0)  # type: ignore[union-attr]
            for m in target.pattern.finditer(text)
        }
        assert versions == {__version__}, target.path
    market = read_json(ROOT / ".cursor-plugin" / "marketplace.json")
    assert market["metadata"]["version"] == __version__


# --- scripts/bump_version.py ----------------------------------------------------------------


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A copy of every file bump_version touches."""
    bump = load_script("bump_version")
    for target in bump.targets():
        destination = tmp_path / target.path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / target.path, destination)
    return tmp_path


def test_bump_rewrites_every_target(tree: Path) -> None:
    bump = load_script("bump_version")
    changed = bump.bump(tree, "9.8.7")
    assert len(changed) == len(bump.targets())
    for target in bump.targets():
        text = (tree / target.path).read_text(encoding="utf-8")
        assert "9.8.7" in text
        assert __version__ not in text.replace("9.8.7", "")
    assert read_json(tree / ".cursor-plugin/marketplace.json")["metadata"]["version"] == "9.8.7"
    assert '__version__ = "9.8.7"\n' in (tree / "src/greenproof/_version.py").read_text()
    hooks = read_json(tree / "plugins/greenproof/hooks/cursor.json")
    assert hooks["version"] == 1  # the hooks-file schema version is not the release version
    assert "uvx greenproof@9.8.7 check --hook cursor" in json.dumps(hooks)


def test_bump_is_idempotent(tree: Path) -> None:
    bump = load_script("bump_version")
    before = {t.path: (tree / t.path).read_bytes() for t in bump.targets()}
    bump.bump(tree, __version__)
    assert {t.path: (tree / t.path).read_bytes() for t in bump.targets()} == before


@pytest.mark.parametrize("bad", ["1.2", "v1.2.3", "1.2.3rc1", "", "1.2.3 "])
def test_bump_rejects_a_malformed_version(tree: Path, bad: str) -> None:
    bump = load_script("bump_version")
    with pytest.raises(ValueError, match=r"X\.Y\.Z"):
        bump.bump(tree, bad)


def test_bump_changes_nothing_when_one_target_does_not_match(tree: Path) -> None:
    bump = load_script("bump_version")
    broken = tree / "plugins/greenproof/hooks/codex.json"
    broken.write_text('{"hooks": {}}\n', encoding="utf-8")
    before = {t.path: (tree / t.path).read_bytes() for t in bump.targets()}
    with pytest.raises(ValueError, match=r"codex\.json"):
        bump.bump(tree, "9.8.7")
    assert {t.path: (tree / t.path).read_bytes() for t in bump.targets()} == before


def test_bump_refuses_a_manifest_with_two_versions(tree: Path) -> None:
    bump = load_script("bump_version")
    manifest = tree / "plugins/greenproof/.claude-plugin/plugin.json"
    manifest.write_text('{"version": "0.1.0", "x": {"version": "0.1.0"}}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="expected 1"):
        bump.bump(tree, "9.8.7")


def test_bump_main(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bump = load_script("bump_version")
    assert bump.main([]) == 2
    assert bump.main(["not-a-version"]) == 1
    assert "bump_version:" in capsys.readouterr().err


# --- scripts/make_icon.py -------------------------------------------------------------------


def decode_png(data: bytes) -> tuple[int, int, list[bytes]]:
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    position, chunks = 8, {}
    while position < len(data):
        (length,) = struct.unpack(">I", data[position : position + 4])
        kind = data[position + 4 : position + 8]
        body = data[position + 8 : position + 8 + length]
        (crc,) = struct.unpack(">I", data[position + 8 + length : position + 12 + length])
        assert crc == zlib.crc32(kind + body) & 0xFFFFFFFF
        chunks[kind] = body
        position += 12 + length
    width, height, depth, colour = struct.unpack(">IIBB", chunks[b"IHDR"][:10])
    assert (depth, colour) == (8, 6)
    raw = zlib.decompress(chunks[b"IDAT"])
    stride = 1 + width * 4
    rows = [raw[i * stride : (i + 1) * stride] for i in range(height)]
    assert all(row[0] == 0 for row in rows)
    return width, height, [row[1:] for row in rows]


def test_make_icon_draws_a_green_square_with_a_white_tick(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    icon = load_script("make_icon")
    monkeypatch.setattr(icon, "SIZE", 64)
    out = tmp_path / "nested" / "logo.png"
    assert icon.main([str(out)]) == 0
    width, height, rows = decode_png(out.read_bytes())
    assert (width, height) == (64, 64)

    def pixel(fx: float, fy: float) -> tuple[int, ...]:
        row = rows[int(fy * height)]
        x = int(fx * width) * 4
        return tuple(row[x : x + 4])

    assert pixel(0.0, 0.0)[3] == 0  # the corner is transparent
    assert pixel(0.5, 0.5)[3] == 255
    assert pixel(0.15, 0.15) == (*icon.GREEN, 255)
    assert pixel(0.43, 0.69) == (*icon.WHITE, 255)  # the bottom of the tick


def test_make_icon_needs_an_output_path(capsys: pytest.CaptureFixture[str]) -> None:
    icon = load_script("make_icon")
    assert icon.main([]) == 2
    assert "usage" in capsys.readouterr().err
