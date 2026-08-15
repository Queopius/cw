# Plugin production deployment contract

CW 0.12 defines, but does not deploy, the production runtime selected in
[ADR 0005](adr/0005-production-mcp-relay.md).

## Components and ownership

| Component | Owner | Responsibility |
| --- | --- | --- |
| Public HTTPS MCP gateway | Queopius | TLS, protocol, auth enforcement, rate limits, routing, redaction, availability |
| OAuth authorization integration | Queopius/approved IdP | discovery, tokens, rotation, revocation, incident controls |
| Relay/control plane | Queopius | tenant isolation, device pairing, grants, bounded routing, audit |
| Local CW agent | user/operator | outbound connection, local runtime health, explicit project grants |
| CWApplication/Engine | CW | invariant, scope, operation, evidence, review, gate, completion policy |
| Repository and `.cw` | user/operator | local source and authoritative workflow evidence |

The local agent makes outbound authenticated connections. It exposes no
general inbound workstation port and accepts only the fixed CW MCP envelope.

## Availability and recovery

The gateway must use bounded timeouts, backpressure, per-principal and
per-project rate limits, health probes, draining deploys, and durable revocation
state. The relay may retain only routing state needed to reconnect a paired
agent. `.cw` remains authoritative for operation reconciliation after gateway,
relay, client, or agent restart.

Transport loss is infrastructure failure, not validation/review/product
failure. Duplicate delivery uses the existing operation digest. Cancellation
keeps the conservative queued-only boundary unless a future operation gains a
proven safe running cancellation point.

## Observability

Structured events may include `CONNECTION_ESTABLISHED`,
`PRINCIPAL_AUTHENTICATED`, `PROJECT_GRANT_RESOLVED`, `TOOL_INVOKED`,
`CAPABILITY_ALLOWED`, `CAPABILITY_DENIED`, operation lifecycle events,
authorization requested/consumed/rejected/expired, and scope violations.

Every event carries only the needed `request_id`, `operation_id`, opaque
`project_handle`, typed actor/origin, capability, outcome, duration, and error
class. Never log source, prompts by default, tokens, credentials, environment,
raw paths, unrestricted output, or reviewer hidden reasoning.

## Production gate before deployment

Implementation must pass TLS/streamable-HTTP MCP, OAuth discovery and negative
auth tests, tenant/project isolation, redaction, replay, reconnect,
concurrency, rate-limit, audit, key rotation, revocation, package provenance,
incident rollback, and installed local-agent tests. A public endpoint must not
be deployed merely to satisfy documentation.
