# ADR 0011: Plan revision identity

## Status

Accepted for the implementation candidate; release approval remains pending.

## Context

The workflow file was previously both active contract and only historical
representation. Rebuild after `REVISE` replaced it, causing old reviews to be
validated against new criteria.

## Decision

Store immutable snapshots in `.cw/plan-revisions/`. Their IDs use the full
SHA-256 of canonical JSON, not timestamps or mutable status. State points to one
active revision after current-format approval/rebaseline. Legacy projects derive
the identity without read-time writes. Revision snapshots include parent,
contract hash, origin, schema/CW version, and the complete workflow document.

## Alternatives

Embedding old plans inside reviews duplicated large contracts and complicated
gate consistency. Moving reviews to an ignored archive hid evidence from normal
audit. Revalidating against the newest plan was the original defect. A schema-2
mandatory migration was rejected because additive schema-1 fields provide a
safer compatibility path.

## Consequences

Auditors can resolve exact historical semantics. Hash/content collision checks,
protected paths, backups, and recovery add storage and implementation cost.
Read-only commands remain mutation-free.
