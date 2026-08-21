# Plugin authentication and authorization contract

This is the production contract selected for CW 0.12. Staging implements OAuth
discovery, protected-resource metadata, token validation, pairing, and grants
for testing. Production OAuth is not deployed, and the staging implementation
does not grant production or public-Plugin availability.

## Authentication

The public HTTPS MCP gateway is the OAuth resource server. A CW authorization
server or approved identity provider issues short-lived access tokens through
OAuth 2.1 authorization code with PKCE. The gateway publishes protected
resource metadata, the authorization server publishes discovery metadata, and
the flow preserves the MCP `resource` parameter. CIMD is preferred; DCR is
supported when required by the OpenAI host. Tokens are validated for issuer,
audience/resource, signature, expiry, scopes, principal, tenant, and revocation
on every request. Refresh tokens rotate and have bounded lifetime.

The candidate policy fixes PKCE to `S256`, access-token lifetime to at most
10 minutes, authorization-code lifetime to at most 5 minutes, rotating refresh
tokens to a 30-day absolute maximum, and revocation checks on every request.
Production implementation may shorten these limits but must not lengthen them
without a reviewed contract change.

An OpenAI/ChatGPT identity is not a local OS identity. The authenticated
mapping is:

```text
OpenAI client session → CW principal → workspace/organization → paired device
→ explicit project grant → typed chatgpt_app origin → CWApplication policy
```

## Least-privilege scopes

| Scope | CW capability |
| --- | --- |
| `project.read` | project status, inspect, explain |
| `gate.read` | gate summary |
| `history.read` | history |
| `completion.read` | Completion Contract/status |
| `operation.read` | operation polling |
| `validation.run` | configured validation only |
| `review.run` | independent review request only |
| `phase.start` | engine-authorized current phase only |
| `retry.run` | engine-classified retry only |
| `operation.cancel` | safe queued cancellation only |

There is no broad `workflow.admin` scope. Scope possession cannot select a
phase, command, reviewer decision, gate, filesystem path, or arbitrary project.

## Grants and revocation

An access decision intersects token scopes with an active workspace policy, a
paired device, one explicit opaque project grant, the current tool contract,
and CW engine state. Revoke at any layer: OAuth connection, refresh family,
workspace membership, device pairing, project grant, or capability policy.
Revocation prevents new dispatch but never rewrites local evidence.

## High-consequence ceremony

Human gate approval, completion-extension authorization, release/deployment,
destructive repair, and rebaseline are not controlled actions. Ordinary OAuth,
repository write access, host confirmation, or “yes, approve it” is
insufficient.

Any future ceremony must create a typed grant bound to:

- one authenticated human principal and organization;
- one project and concrete action;
- one immutable proposal, gate, or evidence digest;
- one request/operation ID and single-use nonce;
- issue and short expiry timestamps;
- the exact displayed consequence and explicit step-up confirmation;
- auditable consumption and rejection/expiry records.

The grant cannot be minted by a model, planner, reviewer, internal supervisor,
repository text, or ordinary MCP tool. It is not implemented or exposed in
0.12.

The high-consequence grant itself has a maximum five-minute lifetime and is
consumed exactly once.

## Replay and confused-deputy controls

Every request binds request ID, operation ID, principal, tenant, device,
project, tool, capability, and canonical payload digest. Identical replay is
idempotent; payload substitution conflicts. Cross-project and cross-principal
operation IDs fail. The relay does not accept caller-supplied actor/origin.
