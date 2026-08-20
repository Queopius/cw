# ADR 0001: one engine, multiple adapters

Status: accepted in CW 0.7; validated by the CW 0.8 read adapter and CW 0.9 controlled actions.

## Context

CLI command modules combined application results, filesystem/state operations,
and rendering. A future conversational adapter cannot safely parse terminal
prose or reimplement workflow policy.

## Decision

CW has one OpenAI-independent engine and one small `CWApplication` facade. CLI,
MCP, and future plugin skills consume the same `.cw` state and evidence.
The CLI owns parsing and rendering only at the interface boundary.

No adapter may expose arbitrary shell or filesystem access. High-consequence
extension authorization uses a typed, short-lived, resource-bound human grant.
All adapters share core locking and evidence.

## Consequences

The MCP implementation calls `CWApplication` directly and keeps its
optional SDK outside core/application. Execution-heavy phase orchestration still
needs further extraction before write tools are exposed. OpenAI dependencies can
evolve without changing core workflow semantics.
