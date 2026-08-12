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

The core uses dependency-injected planner and reviewer adapters, dataclasses,
enums, pathlib, typed errors, and explicit transitions. Codex planning and review
both use ephemeral structured-output calls, while normal tests inject offline
fakes. This allows future providers, presets, goal-scoped subworkflows, a
dashboard, and a gated autopilot without weakening the existing gate invariant.

No third-party CLI/UI framework is used in v0.1. This minimizes installation
cost, enables a self-contained global copy, and keeps behavior auditable.

## Schema compatibility and historical audit

Critical JSON/YAML documents carry `schema_version`. CW centralizes the
compatibility rule: schema 1 is accepted, schema-less prototype documents are a
known legacy input for `cw repair`, invalid versions are rejected, and a version
newer than the installed binary always fails closed with an upgrade instruction.
Repair performs a complete compatibility pass before its first metadata write.

Migration is backup-first and atomic. CW never rewrites a future schema as an
older one. Project identity, state, workflow plans, reviews, gates, and runtime
manifests participate in this check.

`cw doctor` audits all retained review and gate files, not only the current
phase. It validates review criteria and decisions, gate-to-review references,
gate artifact integrity, state references, and the known event vocabulary in
the append-only history. Unknown evidence is an error rather than approval.

## Diagnostics

Diagnostics are separate from workflow state. The latest structured record is
written atomically to `.cw/logs/last-error.json`; distinct failures are appended
to `.cw/logs/errors.jsonl`. This lets `cw error` remain usable when project state
or a workflow schema cannot be loaded. An informational log cannot approve a
phase or influence the state machine.

Known credential forms are redacted before details reach either diagnostics or
`state.json`. Normal commands show a compact classified message, `cw error`
shows the stored detail, and `cw error --raw` additionally exposes the redacted
internal traceback when one exists. Unexpected Python exceptions are converted
to `INTERNAL_ERROR` instead of printing a traceback during daily use.
