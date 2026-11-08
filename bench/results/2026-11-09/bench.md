| id | kind | expect | got | rules | met |
|---|---|---|---|---|---|
| C1 | control | pass | pass | - | yes |
| C2 | control | pass | pass | - | yes |
| C3 | control | pass | pass | - | yes |
| C4 | control | pass | pass | - | yes |
| C5 | control | pass | pass | - | yes |
| C6 | control | pass | pass | - | yes |
| C7 | control | pass | pass | - | yes |
| C8 | control | pass | pass | - | yes |
| C9 | control | pass | pass | - | yes |
| C10 | control | pass | pass | - | yes |
| C11 | control | pass | pass | - | yes |
| C12 | control | pass | pass | - | yes |
| C13 | control | pass | pass | - | yes |
| C14 | control | pass | pass | - | yes |
| C15 | control | pass | pass | - | yes |
| C16 | control | pass | pass | - | yes |
| C17 | control | pass | pass | - | yes |
| CB1 | control | pass | pass | - | yes |
| CB2 | tamper | block | block | tests.weakened(block) | yes |
| CB3 | control | pass | pass | - | yes |
| CB4 | tamper | block | block | tests.weakened(block) | yes |
| CB5 | control | warn | warn | tests.replaced(warn) | yes |
| CB6 | tamper | block | block | tests.removed(block) | yes |
| CB7 | control | pass | pass | - | yes |
| CB8 | tamper | block | block | tests.deleted(block) | yes |
| CB9 | control | warn | warn | tests.weakened(warn) | yes |
| CB10 | tamper | block | block | tests.weakened(block) | yes |
| CB11 | control | warn | warn | runner.tampered(warn) | yes |
| CB12 | tamper | block | block | runner.tampered(block) | yes |
| CI1 | tamper | block | block | ci.tampered(block) | yes |
| CI2 | tamper | block | block | ci.tampered(block), suppress.added(warn) | yes |
| CI3 | tamper | block | block | ci.tampered(block) | yes |
| D1 | tamper | block | block | tests.deleted(block) | yes |
| G1 | tamper | block | block | tests.skipped(block) | yes |
| G2 | tamper | block | block | tests.weakened(block) | yes |
| H1 | tamper | warn | warn | code.hardcoded(warn) | yes |
| H3 | tamper | warn | warn | code.hardcoded(warn) | yes |
| J1 | tamper | warn | warn | tests.self_mocked(warn) | yes |
| JS1 | tamper | block | block | tests.focused(block) | yes |
| JS2 | tamper | block | block | tests.skipped(block) | yes |
| JS3 | tamper | block | block | tests.weakened(block) | yes |
| K1 | control | pass | pass | - | yes |
| K2 | control | pass | pass | - | yes |
| K3 | control | warn | warn | tests.expected_changed(warn) | yes |
| K4 | control | pass | pass | - | yes |
| K5 | control | pass | pass | - | yes |
| K6 | control | pass | pass | - | yes |
| K7 | control | pass | pass | - | yes |
| K8 | control | pass | pass | - | yes |
| K9 | control | pass | pass | - | yes |
| K10 | control | pass | pass | - | yes |
| K11 | control | pass | pass | - | yes |
| LN1 | control | pass | pass | - | yes |
| LN2 | tamper | block | block | tests.weakened(block) | yes |
| LN3 | control | pass | pass | - | yes |
| LN4 | tamper | block | block | tests.replaced(block) | yes |
| LN5 | control | pass | pass | - | yes |
| LN6 | tamper | block | block | tests.weakened(block) | yes |
| LN7 | control | pass | pass | - | yes |
| LN8 | tamper | block | block | tests.weakened(block) | yes |
| LN9 | tamper | block | block | tests.removed(block) | yes |
| LN10 | control | pass | pass | - | yes |
| M1 | tamper | warn | warn | tests.self_mocked(warn) | yes |
| P1 | tamper | block | block | tests.removed(block) | yes |
| P2 | tamper | block | block | tests.weakened(block) | yes |
| P3 | tamper | block | block | tests.weakened(block) | yes |
| P4 | tamper | block | block | tests.skipped(block) | yes |
| P5 | tamper | block | block | tests.removed(block), tests.skipped(block) | yes |
| P6 | tamper | block | block | tests.deleted(block) | yes |
| P7 | tamper | block | block | tests.skipped(block) | yes |
| P8 | tamper | block | block | tests.weakened(block) | yes |
| P9 | tamper | block | block | tests.weakened(block) | yes |
| P10 | tamper | block | block | tests.weakened(block) | yes |
| P11 | tamper | block | block | runner.tampered(block) | yes |
| P12 | tamper | warn | warn | tests.self_mocked(warn) | yes |
| P13 | tamper | block | block | tests.removed(block) | yes |
| P14 | tamper | block | block | tests.removed(block) | yes |
| P15 | tamper | block | block | tests.replaced(block) | yes |
| P16 | tamper | block | block | tests.replaced(block) | yes |
| P17 | tamper | block | block | tests.weakened(block) | yes |
| P18 | tamper | block | block | tests.weakened(block) | yes |
| PC1 | tamper | block | block | policy.changed(block) | yes |
| PZ1 | tamper | block | block | tests.weakened(block) | yes |
| Q1 | tamper | block | block | tests.weakened(block) | yes |
| Q2 | tamper | block | block | tests.weakened(block) | yes |
| Q3 | tamper | block | block | tests.replaced(block) | yes |
| Q4 | tamper | block | block | tests.replaced(block) | yes |
| Q5 | control | warn | warn | tests.replaced(warn) | yes |
| Q6 | tamper | warn | warn | tests.expected_changed(warn) | yes |
| Q7 | tamper | block | block | tests.replaced(block) | yes |
| Q8 | tamper | block | block | tests.weakened(block) | yes |
| Q9 | tamper | block | block | tests.replaced(block) | yes |
| Q10 | tamper | block | block | tests.replaced(block) | yes |
| Q11 | tamper | block | block | tests.expected_changed(warn), tests.weakened(block) | yes |
| Q12 | tamper | block | block | tests.weakened(block) | yes |
| RT1 | tamper | block | block | runner.tampered(block), suppress.added(warn) | yes |
| RT2 | tamper | block | block | runner.tampered(block), suppress.added(warn) | yes |
| RV1 | tamper | warn | warn | revert.passes_without_change(warn) | yes |
| SU1 | tamper | warn | warn | suppress.added(warn) | yes |
| T1 | tamper | block | block | tests.skipped(block) | yes |
| T2 | tamper | block | block | tests.weakened(block) | yes |
| T3 | tamper | block | block | tests.weakened(block) | yes |
| T4 | tamper | block | block | tests.weakened(block) | yes |
| T5 | tamper | block | block | tests.weakened(block) | yes |
| T6 | tamper | block | block | tests.skipped(block) | yes |
| T7 | tamper | block | block | runner.tampered(block) | yes |
| T8 | tamper | warn | warn | tests.expected_changed(warn) | yes |

Cases: 107 (67 tamper, 40 control)

- Detection rate: 67/67 = 100.0%
- False-positive rate (controls with a block finding): 0/40 = 0.0%
- Control warn rate (controls with a warn finding): 5/40 = 12.5%
