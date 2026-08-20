# Architecture

## Terminal presentation

CW keeps terminal presentation separate from workflow behavior. Commands build
structured domain payloads; `cw.ui` renders those payloads through a centralized
console, semantic theme, bounded layout, progress model, and command renderers.
JSON is emitted directly from domain payloads and never parsed from terminal
text.

The CLI intentionally uses only the Python standard library. A dependency
such as Rich was not added because the current visual system needs deterministic
width handling, `NO_COLOR`, non-TTY fallbacks, and stable test fixtures rather
than animation or a full-screen terminal UI. The canvas caps at 88 columns and
adapts to narrower terminals.

CW's engine and normal CLI use the Python standard library. The optional local
MCP adapter adds its SDK only through the `mcp` packaging extra.

```text
cw.cli      argument parsing and thin command orchestration
cw.cli.commands  independently testable command implementations
cw.application UI-independent project scope, capabilities, operation results,
               authorization context, and stable adapter facade
cw.ui       text/ANSI/JSON presentation
cw.core     project identity, workflow, state, locks, gates, completion, persistence
cw.planning repository inspection and plan proposal
cw.checks   deterministic validation
cw.agents   independent review policy and consistency checks
cw.adapters isolated Codex subprocess integration and optional governed MCP
cw.execution normalized events, live state, run identity, profiles, clocks
cw.integrations optional/required capability health and diagnostic normalization
cw.update    release providers, cache, verification, transactions, and rollback
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

`CWApplication` is the stable internal multi-adapter boundary. The CLI delegates
context loading and canonical status construction to it; the MCP adapter calls
the same Python operations rather than spawning `cw`. See
[Plugin readiness architecture](plugin-readiness.md).

`cw.adapters.mcp.runtime` is transport-independent and owns the closed tool
allowlist, typed MCP origin, opaque-handle resolution, privacy projection, and
structured error mapping. `cw.adapters.mcp.server` is the only MCP SDK binding
and runs stdio. Neither core nor application imports the optional SDK. See
[MCP runtime](mcp-runtime.md).

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

`cw.core.progress.EffectiveWorkflowState` is the sole gate-derived workflow
position. It validates the contiguous dependency/gate chain and exposes
completion, current phase, approved/remaining/active counts, and final
gate/review references. Status, repair, start, retry, and batch execution consume
this result; persisted `current_phase` can never override completed evidence.

For a contract-aware workflow, `EffectiveWorkflowState` derives planned-scope
completion separately from semantic completion. `.cw/completion` contains
append-only system reviews, extension proposals, human authorizations, and a
distinct completion gate. The completion reviewer and extension planner are
read-only sibling Codex processes; only the supervisor writes evidence or
authorizes state transitions.

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

Codex-facing output schemas live under `cw/schemas/codex/` and deliberately use
only the structured-output subset accepted by the installed Codex CLI. Richer
internal contracts remain under `cw/schemas/`; constraints such as uniqueness,
dependency ordering, safe paths, criterion identity, and cross-field consistency
are revalidated in Python. A shared adapter rejects known unsupported keywords
before starting a Codex child process, preventing schema drift from being
misreported as a network failure.

No third-party CLI/UI framework is used. This minimizes installation
cost, enables a self-contained global copy, and keeps behavior auditable.

## Live execution observability

```text
codex exec --json (stdout JSONL)     Codex stderr
                 |                       |
                 v                       v
          CodexEventParser       optional diagnostics
                 |
                 v
          ExecutionTracker ---- monotonic clock
            |          |
            v          v
      live renderer   RunRecorder -> .cw/logs/runs/<run-id>.jsonl
```

The adapter reads stdout and stderr independently from a portable `Popen`
boundary. Documented JSON events are the primary activity source; process
liveness is only a secondary safety signal. Agent messages and reasoning items
are deliberately excluded from live and persisted events. Commands are
summarized and secret-looking arguments are redacted at parsing and persistence
boundaries.

`ExecutionTracker` owns the state machine and startup profile. The observer is
line-oriented—no alternate screen or fast redraw—and renders only meaningful
checkpoints. A heartbeat and quiet warning are clock-injected and throttled;
silence never kills a healthy child. The same stream feeds normal, batch, quiet,
verbose, non-TTY, and JSONL modes.

Each managed invocation receives a durable run ID independent of its PID. An
atomic active-run pointer records supervisor/child identity, while redacted
versioned summaries remain inspectable after completion. A live run blocks a
second implementer; a dead supervisor/child pair is an explicit interrupted
run which `cw repair` archives before another launch. Common code has no `/proc`
dependency and the subprocess/liveness boundary works on Linux, macOS, and
Windows.

## Managed installation and updates

```text
GitHub release provider -> strict manifest -> download -> SHA-256
                                                       |
                                                       v
per-user CW data/                          safe extract + smoke test
├── versions/0.1.6/                                    |
├── versions/0.2.0/
├── versions/0.3.0/ <----------------------------------+
├── current                                (atomic pointer switch)
└── update-state.json
```

On POSIX, the per-user root defaults to `~/.local/share/cw`, `current` is an
atomically selected relative symlink, and `~/.local/bin/cw` is the stable
launcher. On Windows, the root is `%LOCALAPPDATA%\Queopius\CW`, `current` is an
atomically replaced UTF-8 version marker, and `bin\cw.cmd` is the stable
launcher. The Windows representation avoids Administrator and Developer Mode
requirements while retaining the same staged, verified, rollback-capable
semantics. Neither launcher points at the development checkout.

Provider, downloader, cache, installation, and service layers are independently
injected in tests. Strict release manifests describe platform artifacts,
SHA-256, channel, publication data, project-schema compatibility, and a reserved
future signature field. Production accepts only trusted HTTPS GitHub hosts.

The global update lock covers download through switch. Extraction accepts only
bounded regular files/directories and rejects absolute paths, traversal,
duplicates, links, devices, and expansion limits. Failed or interrupted staging
never changes `current`; stale staging is cleaned by the next transaction. The
new command must pass `version --json` from staging before selection. Current
plus two recent versions are retained, and the previous healthy version is
protected for rollback.

Update checks use public release metadata and a 24-hour global cache under
`~/.config/cw/update.json`. They include no repository identity or content and
failure is non-critical. Application updates never scan or migrate projects;
project metadata migration remains an explicit backup-first `cw repair`.

## Batch execution

```text
CLI request
    ↓
ExecutionBudget + BatchSession
    ↓
BatchRunner
    ↓ repeated only while budgets permit
canonical single-phase supervisor
    ↓
verified approval gate
    ↓
canonical workflow advancement
```

Batch state is an execution-session overlay under `.cw/runtime`; it is not a
workflow state and cannot approve a phase. The runner uses an injected monotonic
clock, verifies dependencies before each phase, validates the newly created gate
before counting the phase, and rechecks every gate created by the session before
reporting success. A separate project batch lock prevents concurrent batch
mutations; the global updater lock remains independent.

## Integration isolation

Codex MCP configuration remains user-owned. CW discovers effective servers with
`codex mcp list`; it does not assume that `~/.codex/config.toml` is the only
source because plugins can contribute MCP definitions. Planner, reviewer, and
implementer subprocesses retain the normal effective configuration and never
inject `mcp_servers.*` overrides. Planner and reviewer remain ephemeral,
read-only, and disable hooks; the implementer retains the project Stop hook.
CW never reconstructs MCP definitions or emits an unsupported `transport`
property.

Before opening an interactive implementer CW runs a local Codex configuration
preflight with the same global arguments. A rejected effective MCP configuration
becomes non-retryable `CODEX_CONFIG_ERROR`, not a generic implementer crash.
Sanitized invocation records under `.cw/logs/` retain exact flags and a small
environment allowlist; prompts are represented by SHA-256 identifiers.

`CodexRunResult` is the canonical process result shared by planner, reviewer,
and implementer. It carries exit code, separately captured stdout/stderr,
structured payload, deduplicated integration diagnostics, and the terminal
error. A valid expected result with exit code zero wins over optional MCP noise.
Required integrations receive an active preflight and fail closed before
implementation when missing, disabled, unauthenticated, or unavailable. Cached health contains normalized
fields only—never raw HTML or credentials.

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

Workflow consistency is derived once in the domain layer from configured order,
dependency-valid gates, readiness, and persisted state. Only the highest
contiguous gate prefix counts as approved. Read commands never mutate a mismatch:
`cw status`, `cw doctor`, and `cw explain` fail closed and direct the user to the
backup-first repair transaction. Repair restores the first unapproved phase,
latest gate/review references, zeroed attempt, and missing approval history from
validated evidence without creating a gate or running an agent.

Project and state documents use `created_with_cw_version` for immutable origin
provenance. Their `cw_version` is the version of the most recent CW writer or
migrator. Gate/review `cw_version` remains historical evidence from the process
that created that record. The installed application version is independent.

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
