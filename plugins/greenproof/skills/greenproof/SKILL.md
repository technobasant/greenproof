---
name: greenproof
description: 'Use when a "greenproof: not done" message blocks you from stopping, or when the user asks to run the greenproof done gate, explain a greenproof finding, or set greenproof up in a repository.'
---

# greenproof

greenproof is a done gate. When you try to stop, it checks the change since the base
revision for deleted, skipped or weakened tests and for edits to test, runner or CI
configuration, then runs the commands in the project's `.greenproof.toml`. If something
fails, it blocks the stop and tells you why.

## When you are blocked

A message starting "greenproof: not done" lists findings, each with a rule id, a file and a
line.

- Fix the code, not the check. Make the failing test pass by changing the code under test.
- Never delete, skip, xfail or weaken a test, loosen an assertion, or edit test, runner or CI
  configuration to get past the block.
- If the requirement itself changed and a test is now wrong, stop and tell the user which
  test and why. The user decides whether to accept the finding.
- Do not run `greenproof accept`. Do not edit `.greenproof.toml`, the hook files, or
  greenproof's state under the git directory.

## Commands

- Run the gate on demand: `uvx greenproof@0.1.0 check`
- Explain a finding: `uvx greenproof@0.1.0 explain RULE_ID`, for example
  `uvx greenproof@0.1.0 explain tests.skipped`
- Add a policy in a repository where this plugin already provides the hooks:
  `uvx greenproof@0.1.0 init --agents none`. It writes `.greenproof.toml` and an AGENTS.md
  section, and no hooks, because the plugin has them.

## What it needs

`uvx` (from uv) on PATH. Without it the hook cannot start and the stop is not gated.
