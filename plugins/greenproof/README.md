# greenproof plugin

Done gate for coding agents: when the agent stops, greenproof runs your checks and blocks
deleted, skipped or weakened tests. This plugin installs greenproof's hooks in Claude Code,
Codex and Cursor, so you do not run `greenproof init` in each repository.

## What runs, and when

- **On stop**: `uvx greenproof@0.1.0 check --hook AGENT`, in the project. It looks at the change
  since the base revision for deleted, skipped or weakened tests and for edits to test,
  runner or CI configuration, then runs the commands in the project's `.greenproof.toml`,
  which are the project's own tests. A failure goes back to the agent as the reason it is
  not done. The hook has a one-hour timeout, because a plugin cannot read the project's
  policy.
- **On session start**: `uvx greenproof@0.1.0 session-start --hook AGENT`, which writes the
  session marker under the git directory.

uv downloads greenproof from PyPI on first use and pins the version in the command.
greenproof itself makes no network calls in hook mode and has no telemetry. The plugin needs
uv on PATH; without it the hook cannot start and fails open, so nothing is gated.

## Install

Claude Code:

```
claude plugin marketplace add technobasant/greenproof
claude plugin install greenproof@greenproof
```

Codex:

```
codex plugin marketplace add technobasant/greenproof
```

Then install `greenproof` from that marketplace in Codex. The public Codex directory does
not accept plugins with lifecycle hooks, so this marketplace is the way to get it.

Cursor: copy the plugin directory to `~/.cursor/plugins/local/greenproof` (with its
`.cursor-plugin/plugin.json`), then restart Cursor or run "Developer: Reload Window".
For a team, an admin can import https://github.com/technobasant/greenproof as a team
marketplace (Dashboard, Plugins & MCPs, Add Marketplace, Import from Repo), and developers
install the plugin from Customize. Listing in Cursor's public marketplace is a manual
review at cursor.com/marketplace/publish.

## Turn it off for one project

Disable the plugin for that project in the agent (in Claude Code,
`claude plugin disable greenproof --scope project`). To keep the gate but change what it
runs, edit the project's `.greenproof.toml`.

## With `greenproof init`

Running `greenproof init --agents ...` in a repository that has the plugin is unnecessary.
`uvx greenproof@0.1.0 init --agents none` adds only the policy and the AGENTS.md section. If
both are installed, greenproof makes one decision per stop.

## Source and license

https://github.com/technobasant/greenproof, Apache-2.0.
