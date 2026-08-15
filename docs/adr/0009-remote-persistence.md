# ADR 0009: Remote persistence model

**Status:** Accepted for the CW 0.13 candidate

## Decision

Use a transactional repository boundary with a versioned SQLite reference
implementation for deterministic development and E2E. Persist only identities,
public device records, pairing hashes, nonces, opaque project grants, routing
digests/state, revocations, and sanitized audit events.

## Consequences

Uniqueness and tenant constraints are enforced in schema version 1 and
pairing/grant/revocation use transactions. A production implementation may use
PostgreSQL behind the same boundary after staging evidence. Redis or another
queue is not introduced without a demonstrated requirement. Repository source,
raw `.cw`, response bodies, tokens, device private keys, and raw logs are never
remote persistence entities.
