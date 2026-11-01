# Security policy

## Supported versions

Security fixes go into the latest minor release. While greenproof is below 1.0, that is the
only supported line.

## Reporting a vulnerability

Please do not open a public issue. Report it privately through
[GitHub's private vulnerability reporting](https://github.com/technobasant/greenproof/security/advisories/new),
or by email to contact@basantbhattarai.com.np. Include the version, a minimal repository
that reproduces it, and what an attacker gains.

You will get an acknowledgement within seven days.

## What greenproof trusts

* **The base revision's `.greenproof.toml` is code.** Its commands run with `sh -c` in your
  repository, as your user. Review it as you would a CI workflow.
* **greenproof never touches your index or working tree** to build its snapshot; it uses a
  temporary index file and removes any temporary worktree it creates.
* **Reports and the cache contain command output tails.** Do not run commands that print
  secrets.
