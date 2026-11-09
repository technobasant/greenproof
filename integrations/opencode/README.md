# opencode-greenproof

Done gate for OpenCode 2.x: when the agent stops, [greenproof](https://github.com/technobasant/greenproof)
runs your project's checks and blocks deleted, skipped or weakened tests. A failure goes
back to the agent as the reason it is not done, and the same run continues.

## OpenCode 2.x only

This plugin is for OpenCode 2.x (`@opencode/cli`, plugin contract `@opencode/plugin`). It
does not work with OpenCode 1.x (`opencode-ai`): the 1.x loader rejects the module, logs
that, and runs without the plugin.

## What runs, and when

- **First prompt of a top-level session**: `uvx greenproof@0.4.1 session-start --hook opencode`,
  awaited before the model starts. It writes the session marker under the git directory so the
  gate knows what the session started from. A new session sends source `startup`, a continued
  one `resume`. (2.x has no session-start hook, and `session.created` is published before a
  plugin can subscribe.)
- **Top-level session about to go idle**: `uvx greenproof@0.4.1 check --hook opencode`, in the
  project. It looks at the change since the base revision for deleted, skipped or weakened
  tests and for edits to test, runner or CI configuration, then runs the commands in the
  project's `.greenproof.toml`. A block continues the run with greenproof's message. A
  release (the agent was told and stopped without changing anything, or the loop hit its
  bound) is shown as a warning. Subagent sessions are skipped.

Every failure fails open: if greenproof cannot start, exits non-zero, prints something
unexpected or runs for over an hour, the plugin warns and lets the session stop. Add the CI
gate as the backstop.

The plugin needs [uv](https://docs.astral.sh/uv/) on PATH: `uvx` downloads greenproof from
PyPI on first use and the command pins the version. greenproof itself makes no network
calls in hook mode and has no telemetry. It hooks `experimental.idle`, which OpenCode marks
as experimental; if your OpenCode has no such hook, the plugin warns once and does nothing.

## Install

Add the package to `plugins` in `opencode.json` (the key is `plugins`, not 1.x's `plugin`):

```json
{
  "plugins": ["opencode-greenproof"]
}
```

or, with options syntax, `{"plugins": [{"package": "opencode-greenproof"}]}`.

Or run `greenproof init --agents opencode` in the repository, which writes
`.opencode/plugins/greenproof.js` with the hook command baked in (and `AGENTS.md`, which
tells the agent about the gate). Installing both is safe: when two copies run in one
OpenCode process they share a claim on each event and the gate runs once.

## License

Apache-2.0.
