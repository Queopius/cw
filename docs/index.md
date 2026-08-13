<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/brand/cw-logo-dark.png">
    <source media="(prefers-color-scheme: light)" srcset="assets/brand/cw-logo-light.png">
    <img src="assets/brand/cw-mark.png" alt="" width="180">
  </picture>
</p>

# CW — Codex Workflow

**Build with autonomy. Advance with evidence.**

CW is a controlled workflow layer for AI-assisted software development.

It is designed around bounded autonomy:

```text
PLAN → IMPLEMENT → VALIDATE → INDEPENDENT REVIEW → GATE → NEXT PHASE
```

The core rule is:

> **No valid gate. No next phase.**

And for completed workflows:

> **All valid gates. No next phase.**

CW is intended to help teams use Codex in a controlled, auditable workflow instead of relying on unbounded autonomous execution.

## What CW gives you

- explicit development phases;
- implementation and independent review separation;
- deterministic validation before advancement;
- approval gates with integrity evidence;
- safe retry and recovery paths;
- project-local workflow state;
- fail-closed behavior when workflow evidence becomes inconsistent.

## Product identity

**CW by Queopius**

Legal ownership/operator information should match the repository's canonical `LICENSE`, `NOTICE`, and release metadata.

## Documentation status

This documentation starter is intentionally conservative. Any CLI option not verified against the current `cw --help` output must be validated before public release.
