# Workflow model

CW separates five responsibilities that are often collapsed into one autonomous
agent loop:

```text
PLAN → IMPLEMENT → VALIDATE → INDEPENDENT REVIEW → GATE → NEXT PHASE
```

| Stage | Owner | Result |
| --- | --- | --- |
| Plan | Read-only planner | Explicit phases, dependencies, criteria, and commands |
| Implement | Workspace-write implementer | Current-phase artifacts and readiness |
| Validate | CW supervisor | Deterministic checks and final artifact hashes |
| Review | Separate read-only reviewer | `APPROVE`, `REVISE`, or human review required |
| Gate | CW supervisor | Verified evidence permitting advancement |

Implementation is not review, deterministic validation is not semantic review,
and reviewer approval is not yet permission to advance until CW verifies and
persists the gate.

> **No valid gate. No next phase.**

## State transitions

CW uses explicit states and rejects transitions outside the state graph:

```text
UNINITIALIZED → INITIALIZED → PLANNING → PLAN_PROPOSED → READY → IN_PROGRESS
IN_PROGRESS → READY_FOR_REVIEW → REVIEWING
REVIEWING → REVISION_REQUIRED | APPROVED | HUMAN_REVIEW_REQUIRED | ERROR
REVISION_REQUIRED → authorized plan rebaseline → READY
IN_PROGRESS | READY_FOR_REVIEW | REVISION_REQUIRED
  → governed artifact-only plan amendment → PLAN_PROPOSED
  → human plan approval → READY
APPROVED → IN_PROGRESS | COMPLETED
```

`APPROVED` is a validated transition boundary, not a stable non-final operating
state. The approval domain operation persists the review and gate, records the
audit event, consumes readiness, and atomically writes the resulting runtime
state. A non-final phase moves immediately to the next configured phase as
`IN_PROGRESS` with attempt zero. Approval of the final configured phase writes
`COMPLETED`; CW never invents a successor.

Artifact-only amendment is the narrow exception for a declarative omission in
the current, incomplete phase. It preserves prior gates and history, removes
only incompatible current-phase evidence from the active namespace through
append-only supersession, and cannot resume execution until a new human plan
approval. It never changes criteria, commands, review paths, dependencies or
the Completion Contract.

## Planned completion and semantic completion

Legacy projects preserve the original rule:

```text
all configured phases have valid dependency-ordered gates
                         ↓
                    COMPLETED
                         ↓
              current_phase = none
                         ↓
             no implementer is launched
```

The normal command, `cw retry`, and `cw run` all stop at this boundary. They do
not retain the final approved phase as current and never wrap to the first
phase.

Contract-aware projects add an independent system boundary:

```text
all authorized phases have valid gates → PLANNED_COMPLETE
PLANNED_COMPLETE → completion review → completion evidence → COMPLETED
                                    ↘ extension proposal → human authorization
```

`PLANNED_COMPLETE` has no current phase and launches no implementer. It proves
only that the authorized phase list is finished. See [Completion Contracts and
program review](completion-contracts.md).

> **All authorized phase gates valid. Completion Contract satisfied. No next phase.**

## Implementation session boundary

The implementer cannot update state. It works only on the current phase, creates
`.cw/runtime/READY_FOR_REVIEW.json`, and stops. The Stop hook delegates to the
installed `cw review --hook`; it writes mutable data only under `.cw`.
CW snapshots protected workflow metadata around that session and admits only the
precise state, history, review, and gate delta produced by a valid current-phase
review. Any other protected mutation stops the workflow in `ERROR`.

CW distinguishes the immutable **phase contract**—phase definition, acceptance
criteria, dependencies, required commands, policy, and human-gate requirements—
from **CW-managed mutable metadata** such as runtime state, current writer
version, history, and migration records. The supervisor may update operational
metadata through trusted transactions; the implementation agent may not.

Each `cw start` creates an atomic `.cw/runtime/implementer-session.json` and
passes its random session ID to the implementer and Stop hook. The session also
records its owning CW process as a lease: a concurrent `cw start` is rejected,
and an orphan without readiness requires backup-first `cw repair`. Readiness must
contain that exact ID, so a manifest from an older invocation cannot be replayed.
The hook is inert in unrelated Codex sessions and reviewer sessions. A semantic
decision consumes both runtime files; an infrastructure failure preserves them
so `cw retry` can rerun only the reviewer.

Infrastructure failures carry an explicit error code, retryability flag,
operation, phase, and occurrence timestamp. They never increment the semantic
review attempt. For prototype-era reviewer records, backup-first repair recognizes
known transport, process, timeout, permission, smoke-test, and response-schema
signatures and restores the effective attempt count. Retry records a fresh audit
event, preserves the current phase and approved gates, and reuses valid readiness.
Without readiness it validates completed work and may regenerate only the
manifest; it does not blindly invoke the implementer.

CW follows the official [Codex Stop hook contract](https://learn.chatgpt.com/docs/hooks):
terminal review outcomes return `continue: false`, while `decision: block` is
avoided because it asks Codex to create a continuation turn. A repeated event
with `stop_hook_active` is stopped explicitly.

Every `cw start` validates dependency gates and their artifact hashes before it
launches the current phase. Compatibility logic can consume a v0.1.3
post-approval state, but new reviews no longer defer advancement until start.

Only one mutating operation can hold `.cw/locks/operation.lock`. A dead process
lock is recognized as stale and safely replaced. The session lease extends
exclusion across the long-lived Codex subprocess while still allowing its Stop
hook to acquire the short metadata lock for review.

After Codex exits, CW verifies the outcome. A semantic review may have consumed
the session, or a valid readiness manifest may remain for manual `cw review`.
Exiting without either is `IMPLEMENTER_PROCESS_ERROR` and is safely retryable.
`cw start --json` is rejected because a machine-output shortcut must never move
state without actually starting the implementer.

Every retained review, gate, and history event remains part of the workflow's
audit surface. `cw doctor` checks the entire surface, including records from
earlier phases, so tampering with old evidence cannot remain hidden behind a
healthy current state.

Reviewed plan correction uses immutable plan revision identities. Global phase
attempts remain monotonic across revisions; `revision_attempt` restarts at zero
when the corrected revision activates. Thus the first review under revision B
can be global attempt 2 and revision attempt 1. Only active-revision evidence
governs current state, while superseded evidence remains fully audited.

`cw status` reports `Position` (the current configured phase index) separately
from `Approved` (the number of gates that currently validate). `cw history`
projects an audit-oriented phase view from gates first, then reviews and
structured events. It does not invent missing timestamps and treats the gate's
linked review as the canonical final approval, avoiding duplicate prototype
approval records.

## Canonical state and reconciliation

Renderers do not independently guess progress. CW derives effective state from
configured phase order, dependencies, validated gates, cached state, readiness,
reviews, and history. Only the highest contiguous valid gate chain counts.

If a later gate exists beyond a broken dependency, that later file does not
advance the workflow. If cached state points behind a healthy chain, read
commands fail closed and `cw repair` performs backup-first reconciliation. See
[Repairing inconsistent workflow state](troubleshooting.md#workflow-state-invalid).

## Bounded multi-phase runs

`cw run N` repeatedly invokes the canonical single-phase supervisor, not an
alternate workflow engine. Each iteration must finish implementation,
deterministic validation, independent review, gate creation, and the domain
advance transition. Phase, time, semantic-revision, and agent-run budgets are
checked before another iteration begins. See [Controlled batch
execution](batch-execution.md).

Automation receives the same model directly: `cw status --json` includes
`position`, `approved_count`, `gate_states`, and the configured phase list;
`cw history --json` includes the reconstructed per-phase timeline plus retained
structured state events. JSON is versioned with `schema_version` where
applicable and never contains ANSI presentation sequences.
