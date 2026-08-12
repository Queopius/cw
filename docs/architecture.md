# Architecture

CW is a standard-library Python package with a console entry point.

```text
cw.cli      argument parsing and command orchestration
cw.ui       text/ANSI/JSON presentation
cw.core     project identity, workflow, state, locks, config, gates, persistence
cw.planning repository inspection and plan proposal
cw.checks   deterministic validation
cw.agents   independent review policy and consistency checks
cw.adapters isolated Codex subprocess integration
cw.templates project-installed static integration
cw.schemas  reviewer/readiness contracts
```

The shell installer and generated launcher contain no workflow business logic.
The installed package owns runtime code; a project receives only static Codex
integration. Runtime operations do not need `.codex` to be writable.

The core uses dependency-injected reviewer adapters, dataclasses, enums, pathlib,
typed errors, and explicit transitions. This keeps normal tests offline and
allows future planner backends, presets, goal-scoped subworkflows, a dashboard,
and a gated autopilot without weakening the existing gate invariant.

No third-party CLI/UI framework is used in v0.1. This minimizes installation
cost, enables a self-contained global copy, and keeps behavior auditable.
