# Workflow

CW uses explicit states and rejects transitions outside the state graph:

```text
UNINITIALIZED → PLANNING → PLAN_PROPOSED → READY → IN_PROGRESS
IN_PROGRESS → READY_FOR_REVIEW → REVIEWING
REVIEWING → REVISION_REQUIRED | APPROVED | HUMAN_REVIEW_REQUIRED | ERROR
APPROVED → IN_PROGRESS | COMPLETED
```

`APPROVED` is a validated transition boundary, not a stable non-final operating
state. The approval domain operation persists the review and gate, records the
audit event, consumes readiness, and atomically writes the resulting runtime
state. A non-final phase moves immediately to the next configured phase as
`IN_PROGRESS` with attempt zero. Approval of the final configured phase writes
`COMPLETED`; CW never invents a successor.

The implementer cannot update state. It works only on the current phase, creates
`.cw/runtime/READY_FOR_REVIEW.json`, and stops. The Stop hook delegates to the
installed `cw review --hook`; it writes mutable data only under `.cw`.
CW snapshots protected workflow metadata around that session and admits only the
precise state, history, review, and gate delta produced by a valid current-phase
review. Any other protected mutation stops the workflow in `ERROR`.

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

`cw status` reports `Position` (the current configured phase index) separately
from `Approved` (the number of gates that currently validate). `cw history`
projects an audit-oriented phase view from gates first, then reviews and
structured events. It does not invent missing timestamps and treats the gate's
linked review as the canonical final approval, avoiding duplicate prototype
approval records.

Automation receives the same model directly: `cw status --json` includes
`position`, `approved_count`, `gate_states`, and the configured phase list;
`cw history --json` includes the reconstructed per-phase timeline plus retained
structured state events. JSON is versioned with `schema_version` where
applicable and never contains ANSI presentation sequences.
