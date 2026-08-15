# ADR 0001: one engine, multiple adapters

Status: accepted for CW 0.7.

## Context

CLI command modules combined application results, filesystem/state operations,
and rendering. A future conversational adapter cannot safely parse terminal
prose or reimplement workflow policy.

## Decision

CW has one OpenAI-independent engine and one small `CWApplication` facade. CLI,
future MCP, and future plugin skills consume the same `.cw` state and evidence.
The CLI owns parsing and rendering only at the interface boundary.

No adapter may expose arbitrary shell or filesystem access. High-consequence
extension authorization uses a typed, short-lived, resource-bound human grant.
All adapters share core locking and evidence.

## Consequences

Read-only MCP implementation is now an adapter task. Execution-heavy phase
orchestration still needs further extraction before write tools are exposed.
OpenAI dependencies can evolve without changing core workflow semantics.

