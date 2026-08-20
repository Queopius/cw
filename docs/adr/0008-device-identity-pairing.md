# ADR 0008: Device identity and pairing

**Status:** Accepted for the CW 0.13 candidate

## Decision

Each local agent uses an Ed25519 device key. Pairing binds its public key to an
authenticated principal/workspace through a short-lived, single-use human
challenge. The private key remains local; an owner-only file is the portable
fallback behind a credential-storage abstraction.

## Consequences

Device, OAuth session, and project grant are independently revocable. Pairing
does not grant a project. Future OS keychain adapters and hardware-backed keys
can replace the fallback without changing gateway or workflow semantics.
