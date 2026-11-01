# Measurements

Everything here was measured on 2026-11-01 at commit `0b4b3c4`, on an Apple M5 (10 cores)
running macOS, with Python 3.13 and uv 0.13.5. The raw results are in
[`bench/results/2026-11-01/`](bench/results/2026-11-01/), and the commands to reproduce
them are at the end.

## Summary

| what | result |
|---|---|
| Benchmark: tamper cases reported as expected | 67 of 67 (58 block, 9 warn by design) |
| Benchmark: control cases blocked | 0 of 40 (5 warn) |
| Real history: commits blocked | 70 of 698 test-touching commits in four projects (10.0%) |
| Real history: blocks judged correct, false, ambiguous | 42, 20, 8 |
| Real history: static check time per commit | median 258 to 390 ms, p95 496 to 1226 ms, by project |
| Claude Code, end to end | a requested skip was blocked and the agent stopped to ask; an honest change passed |
| Test suite | 904 tests (901 passed, 3 skipped), 95.88% line and branch coverage |

Read the caveats with the numbers. The benchmark was written alongside the rules, and the
rules were calibrated on the same real commits they are measured on here.

## Benchmark

`bench/run.py` builds a small fixture repository (`python-shop`, `js-cart` or `go-calc`),
commits it as the base, applies one case's edits as the candidate and runs `greenproof
check --mode local`. A case is met when the exit code and the reported rules match what
the case expects: block, warn or pass. There are 107 cases, all met.

| source | kind | cases | block | warn | pass |
|---|---|---|---|---|---|
| review probe | tamper | 44 | 40 | 4 | 0 |
| Skylos testwashing | tamper | 7 | 7 | 0 | 0 |
| ImpossibleBench | tamper | 5 | 0 | 5 | 0 |
| replay triage | tamper | 11 | 11 | 0 | 0 |
| review probe | control | 18 | 0 | 1 | 17 |
| control | control | 11 | 0 | 1 | 10 |
| replay triage | control | 11 | 0 | 3 | 8 |

- **Review probe** cases come from adversarial probes written while the rules were built:
  removed, skipped and focused tests, loosened assertions, constant-fed assertions,
  rewrites that drop the pinned value, and runner or CI configuration edits.
- **Skylos testwashing** and **ImpossibleBench** cases re-create, in the fixtures, the
  patterns those sources describe. They are not their data. The ImpossibleBench patterns
  (special-casing the test's input in code, mocking the function under test, editing a
  test's expected value while the code is unchanged) are reported as **warn**. They are
  common in honest work too, so they do not block by default.
- **Replay triage** cases reproduce, in small form, false blocks and true blocks found in
  the real-history triage below. They guard the calibration.
- **Controls** are legitimate changes: renames, new features with new tests, spec changes
  that update code and expected values together, test refactors. The 5 control warnings
  are a spec change that updates expected values (`tests.expected_changed`), a rewrite that
  keeps its subject and a removed test with no assertion in a file that gains a test
  (`tests.replaced`), a dropped `isinstance` check next to a kept
  exact check (`tests.weakened`), and a newly adopted type-checker table that disables an
  error code (`runner.tampered`).

**Caveat.** The cases were written by the same project as the rules, alongside them. 100%
means the rules do what the cases describe. It is not a detection rate for tampering in
the wild.

Median time per case is 370 ms. The full run takes 43 s of check time.

## Real history

`bench/replay.py` replays a project's history. For each of the last 200 first-parent,
non-merge commits that touch a test file, it checks out the parent in a temporary clone,
commits it as the base, sets the tree to the commit and runs the static checks (`--checks
static`). Commands, the revert check and smoke are not run. The policy is the same at the
base and the candidate, so `policy.changed` cannot fire.

These are human commits on maintained projects, not agent output. When a maintainer
deletes or rewrites tests on purpose, greenproof still reports it. In its model a
maintainer reviews and accepts such a finding.

| project | commits | blocked | rate | warn only | check ms, median | check ms, p95 |
|---|---|---|---|---|---|---|
| pallets/click | 132 | 8 | 6.1% | 9 | 336 | 740 |
| encode/httpx | 200 | 30 | 15.0% | 59 | 390 | 1226 |
| pmndrs/zustand | 166 | 9 | 5.4% | 43 | 310 | 807 |
| spf13/cobra | 200 | 23 | 11.5% | 5 | 258 | 496 |

click and zustand have fewer than 200 test-touching first-parent commits, so all of them
were replayed. Process wall time, including interpreter start, has a median of 629 to
766 ms and a p95 of 922 to 1569 ms.

Blocked commits per rule (a commit can count under more than one): `tests.weakened` 33,
`tests.removed` 25, `tests.replaced` 13, `tests.skipped` 5, `tests.deleted` 5,
`tests.focused` 1, `ci.tampered` 1.

### Triage of the blocks

Every block was triaged by reading the commit's diff against the findings. The triage
was done with model assistance, during development, by the same project. The verdict and
the reason for each commit are in `bench/results/2026-11-01/triage-*.tsv`, so they can be
checked.

- **correct**: the commit really removed, skipped or weakened checking. Usually a
  maintainer meant to, for example by deleting a feature with its tests.
- **false**: the checking is still there in another form, or was strengthened.
- **ambiguous**: some checking was lost, but whether it mattered is a judgment call.

| | correct | false | ambiguous | total |
|---|---|---|---|---|
| first replay | 43 | 27 | 8 | 78 |
| after calibration | 42 | 20 | 8 | 70 |

The calibration changed how tests are paired across renames and how JavaScript and Go
assertions are counted. It also downgraded some findings to warnings (SPEC 5.4, 5.6,
5.7). It cleared 7 false blocks and blocked no commit that was not blocked before. One
correct block cleared: cobra `266278769` removed only a benchmark, and benchmarks are not
counted as tests.

**Caveat.** The calibration was tuned on these commits. On projects it has not seen,
expect a higher false-block rate than 20 of 698.

### Remaining false blocks

These are known limits. Each is a pattern that line-level and syntax-tree heuristics
misread.

- **Checks moved into a helper or a loop (Go).** A call to a same-file assertion helper,
  or a loop body, counts as one check, however many it runs. Helpers in other files are
  not followed. cobra `4cafa37bc`, `9e1d6f1c2`, `652c755d3`, `b36196066`, `2da4a54c5`.
- **Several exact checks merged into one larger equality.** The count drops while the
  check gets stronger. An expected value built by a constructor
  (`== QueryParams("a=456")`) is not counted as pinned. httpx `354c4cac1`, `883cf8ca6`,
  `2abb2f214`.
- **A test renamed while its body also changed.** Renames pair by name, unchanged body or
  body shape. A rename together with an edit pairs by none of them. click `d8763b930`,
  zustand `d28169d1b`, `633c7dd7a` (titles that repeat in the file), cobra `22b617914`,
  `70e53f62b`.
- **Tests moved to another file or a suite rewritten.** Absorbing works within one file.
  httpx `cca62060c`, cobra `65c8acb22`, zustand `e247220ec` (part of that commit is a
  correct block).
- **The checked subject written differently.** `headers == ...` became
  `request.headers == ...`. A parametrize case was split into its own test with changed
  expected values. httpx `7947b5607`, `8dc9b6bd5`.
- **CI judged per workflow file.** A workflow deleted while new workflows run the same
  steps through a package script alias. zustand `2367abb18`.
- **A conditional `t.Skip` (Go)** that only applies to a newly added table row is read as a
  skip of the whole test. cobra `df12a0a24`.

### Warnings

116 of the 698 commits got a warning without a block. Most come from `suppress.added`
(85 commits), which reports every new `# type: ignore`, `# noqa`, `eslint-disable` or
`@ts-expect-error` comment. Next come `tests.expected_changed` (29), `tests.replaced` (7),
and one each of `tests.weakened`, `tests.skipped` and `runner.tampered`. Warnings do not
fail the check. Set a rule to `off` in `[rules]` if it is noise for your project.

## Agents, end to end

**Claude Code** (`claude -p --model haiku`) was run in a toy repository with hooks
installed by `greenproof init` and the pytest suite as the test command:

- **Asked to add `@pytest.mark.skip` to a test.** The Stop hook blocked with
  `tests.skipped`. The agent stopped, said why, and asked the user. It neither reverted
  the change nor ran `greenproof accept`. Its next stop, with the same tree, was released,
  as designed.
- **Asked to change a business rule and update the test to match.** The Stop hook passed
  and the session ended normally.

**Cursor, Codex CLI and Gemini CLI** were not run end to end. Their hook input and output
formats are covered by the test suite, written against each vendor's documented format.
Codex CLI also skips project hooks until the user trusts them.

## Test suite

904 tests: 901 passed, 3 skipped because actionlint, zizmor and jest are not installed. Line and branch coverage is 95.88%. The suite runs in 4 min 15 s with
`pytest -n auto` on 10 cores. ruff and mypy (strict) pass.

## Reproduce

```bash
uv sync
uv run pytest -n auto --cov=greenproof
uv run python bench/run.py --json bench.json

# real history: clone a project, pin it, replay it
git clone https://github.com/spf13/cobra /tmp/gp-replay/cobra
git -C /tmp/gp-replay/cobra checkout --detach adbc8813901b
uv run python bench/replay.py /tmp/gp-replay/cobra --n 200 --out replay-cobra.tsv --details
```

| project | commit replayed from |
|---|---|
| pallets/click | `2247b35ea1c4` |
| encode/httpx | `b5addb64f016` |
| pmndrs/zustand | `382d29c485e6` |
| spf13/cobra | `adbc8813901b` |

Timings depend on the machine. Exit codes and findings do not.
