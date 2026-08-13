# Architecture

CW is a standard-library Python package with a console entry point.

```text
cw.cli      argument parsing and thin command orchestration
cw.cli.commands  independently testable command implementations
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

`cw.cli.parser` owns argument normalization and the public command grammar.
`cw.cli.runner` owns dispatch and the top-level error boundary. `cw.cli.main`
is the composition root and retains the command registry and compatibility
wrappers. Command modules own bounded use cases and receive repository/context
services explicitly. This keeps public dispatch stable while allowing commands
to be extracted and tested without turning the entry point back into a monolith.
Mutable configuration has its own command module; read-oriented status and
diagnostic commands never acquire responsibility for configuration writes.

`cw.ui.theme`, `cw.ui.symbols`, and `cw.ui.renderers` form the presentation
boundary. Commands pass structured status, history, and diagnostic data to that
layer; domain code never emits ANSI and JSON is serialized directly from domain
payloads. Color is state communication only and is disabled for non-TTY output,
`NO_COLOR`, and `--no-color`.

`cw.core.state.advance_after_approval` owns the operational boundary after a
gate is verified. It records the final state once, resets the next phase attempt,
clears stale errors and runtime readiness, or completes the final phase. The
append-only review and gate are written before that state commit, so a crash can
be reconciled from authoritative evidence by backup-first repair.

`cw.core.layout` defines the trusted project filesystem topology. Validation is
performed before init writes, lock acquisition, normal context loading, repair,
and backup. Individual critical loaders retain their own regular-file checks as
defense in depth.

The release demo executes against the copied installation under
`~/.local/share/cw`, not through an editable checkout. It uses dependency
injection for the offline reviewer while retaining real state transitions,
readiness validation, artifact hashing, and gate creation.

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

Criterion severity has one Python domain model: `blocking` and `advisory`.
The prototype-only `non-blocking` value is normalized to `advisory` in the raw
workflow migration layer before strict loading. Unknown values remain errors.
Schema synchronization tests prevent the serialized planner enum from drifting
from the Python enum. Prototype reviews and gates are validated through a
read-only compatibility adapter so approval evidence is preserved byte-for-byte.

Repair uses the non-secret Git-local repository fingerprint as its identity
boundary. Matching fingerprints permit a normal directory rename and retain the
plan. A differing fingerprint means the metadata was copied from another repo:
CW backs it up, then clears active plan, state, policy overrides, runtime,
reviews, gates, logs, and legacy mutable paths instead of adopting them.
For a matching fingerprint, repository rename migration atomically rebinds the
workflow ID in retained reviews, gates, and an optional session after backup;
criteria, decisions, artifact hashes, and approval history are unchanged and
must still pass the full historical audit.

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

Retryable infrastructure state is stored separately from the human-facing
diagnostic string. Its structured metadata binds an error code to the failed
operation and phase, preventing retry dispatch from reinterpreting stale text.
Legacy review failures are normalized only after a metadata backup; their
original records remain available in backups and redacted diagnostics.

Known credential forms are redacted before details reach either diagnostics or
`state.json`. Normal commands show a compact classified message, `cw error`
shows the stored detail, and `cw error --raw` additionally exposes the redacted
internal traceback when one exists. Unexpected Python exceptions are converted
to `INTERNAL_ERROR` instead of printing a traceback during daily use.
