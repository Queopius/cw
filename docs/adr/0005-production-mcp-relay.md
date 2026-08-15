# ADR 0005: public HTTPS gateway with a paired local CW agent

## Status

Accepted as the CW 0.12 production architecture. Runtime implementation and
public deployment are deferred.

## Context

OpenAI's current plugin model accepts skills, MCP, or both. Developer Mode may
reach a private stdio server through Secure MCP Tunnel, but public submission
requires a stable publicly reachable HTTPS MCP endpoint. CW owns local
repositories and `.cw` evidence, so a conventional hosted server would either
lose access to the project or require source/state upload.

## Options considered

| Model | Privacy | User experience and reliability | Security/operations | Decision |
| --- | --- | --- | --- | --- |
| User-run local runtime plus broker | Source stays local; minimal broker data | Requires a durable local agent but no inbound port | Central auth plus pairing and relay controls | **Selected** |
| User-run public HTTPS endpoint | Source stays local | Certificate, DNS, uptime, and OAuth burden per user | Large attack surface on each workstation | Supported only for advanced self-hosting |
| Managed relay/control plane plus local agent | Source stays local; normalized envelopes transit | Stable public endpoint and revocation; agent must be online | Moderate central operations and tenant isolation | **Selected form of model 1** |
| Fully managed runtime/repository connection | Source may leave device | Simplest always-on UX | Largest privacy, tenancy, compliance, and cost scope | Deferred |

## Decision

Use a Queopius-operated, public streamable-HTTPS MCP gateway and relay paired
with an outbound-only user-run CW agent. The agent resolves explicit project
grants and invokes the existing `CWApplication`; the gateway never becomes a
workflow engine.

```text
ChatGPT / Codex
      │ HTTPS MCP + OAuth access token
      ▼
CW gateway / relay
      │ authenticated principal, scoped request, correlation
      ▼
paired outbound-only local agent
      │ opaque project grant
      ▼
CWApplication → CW Engine → local repository + .cw
```

Secure MCP Tunnel remains supported for private/development evaluation and is
not the production submission endpoint.

## Trust and data boundaries

Repository source, Git credentials, local paths, process environment, raw
logs, and `.cw` files remain on the user's machine. The network carries an
authenticated principal identifier, workspace/tenant identifier, opaque
device and project handles, tool name, bounded arguments, operation/request
IDs, capability decision, and normalized redacted CW result. Source content is
not a default field and cannot be requested by a generic tool.

The control plane may persist account, device, project-grant metadata, token
and revocation state, operation routing state, and minimum audit events. It
must not persist raw source or complete `.cw` evidence by default.

## Project authorization and revocation

The operator pairs a local agent, then explicitly grants individual initialized
CW projects. The local agent creates opaque handles bound to its canonical
repository identity. Every request must match principal, workspace, device,
project, capability, and operation. Revoking the OAuth connection, device, or
project grant stops future routing without changing local workflow evidence.

## Failure modes

- Gateway unavailable: `INFRASTRUCTURE_FAILURE`; no workflow failure or gate.
- Local agent offline: bounded `PROJECT_RUNTIME_UNAVAILABLE`; accepted durable
  operations remain governed by `.cw` reconciliation.
- Duplicate delivery: operation ID and digest produce replay or conflict.
- Stale/revoked grant: authorization failure before local dispatch.
- Relay compromise: closed tool schemas, server-side scope enforcement, local
  project resolution, and CW engine policy remain defense in depth.

## Consequences

CW Core and `CWApplication` remain OpenAI-independent and local-first. The
selected model requires a future gateway, OAuth authorization server contract,
pairing protocol, tenant isolation, availability ownership, incident response,
and public external acceptance. None is implemented or deployed in 0.12.
