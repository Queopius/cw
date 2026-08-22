# ADR 0012: Review supersession

## Status

Accepted for the implementation candidate; release approval remains pending.

## Context

A plan correction must stop an old `REVISE` from governing the active contract
without deleting, editing, overturning, or laundering that review.

## Decision

Use a separate append-only `.cw/supersessions/` record. It binds review path and
hash, old/new revisions, exact proposal, mandatory reason, typed human actor,
expiring authorization, nonce, operation ID, CW version, timestamp, and result.
`cw plan rebaseline` has distinct preview and apply steps. Apply is lock-guarded,
backup-first, journaled, recoverable, and activates revision B as `READY`.

Planner, reviewer, CI, and internal origins cannot authorize. A supersession
creates no gate and cannot use human gate approval to replace independent
review. Only a later review for revision B can gate revision B.

## Alternatives

Mutating the review violated immutability. A history-only event lacked enough
contract material for independent audit. Generic `--force` lacked exact intent,
hash binding, expiry and replay protection. Reusing Completion extension
semantics confused adding scope with correcting a current contract.

## Consequences

History exposes both reviews and attempts. Operators gain a deliberate extra
ceremony. Interrupted writes require the documented recovery path rather than
silent read-time repair.
