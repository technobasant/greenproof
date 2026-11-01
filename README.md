# greenproof

A definition-of-done gate for code written by LLM coding agents. It runs outside the agent,
runs the project's checks itself, reads its policy and commands from the base revision so
the agent cannot weaken the gate in the same change, and names each way a test or check was
weakened with a rule id, file and line.

Work in progress: see [SPEC.md](SPEC.md) for the design.
