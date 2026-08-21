# Plugin candidate security review

The plugin is an adapter and package, not a second workflow engine. A malicious
plugin client must still pass the same project scope, actor/origin, capability,
operation-digest, locking, review, gate, and Completion Contract policy enforced
by `CWApplication`.

## Threat model

| Threat | Boundary and mitigation |
| --- | --- |
| Repository prompt injection | Repository text is untrusted evidence and cannot select tools, actors, phases, decisions, gates, or authorization. |
| Skill manipulation | Skill prose is advisory; server policy remains authoritative if the skill is absent or altered. |
| Forged actor/origin | Local and ChatGPT development bootstraps create distinct typed origins and reject caller actor/authorization fields. |
| Annotation mismatch | Runtime capability registry, not manifest/tool hints, controls execution. |
| Replay or payload substitution | Project-scoped operation ID and canonical request digest provide idempotent replay or safe conflict. |
| Path escape/cross-project access | Canonical allowed roots, opaque handles, Git/CW identity, and symlink checks fail closed. |
| Arbitrary arguments | Closed schemas omit phase, command, review decision, evidence, actor, and authorization inputs. |
| Fake review or gate | Independent read-only reviewer and supervisor validation are the only gate path. |
| Extension bypass | High-consequence authorization is absent from registration and rejected by application policy. |
| Transport compromise | Local stdio reserves stdout for MCP and isolates child stdin; Secure MCP Tunnel may forward only the explicitly configured scoped CW command. |
| Secret/log leakage | Minimum-disclosure projections, redaction, bounded results, and stderr-only diagnostics. |

## Permission mismatch

OpenAI tool annotations help hosts select and confirm tools, but they are not an
authorization system. Even if a client relabels `cw_validate` as read-only, the
server executes it only as capability `validation.run`, under the normal
project lock and state checks. Unknown or high-consequence tools are rejected
before dispatch.

## ChatGPT development, staging, and production gap

The development profile fixes `chatgpt_app` origin, requires explicit project
grants, and can omit all controlled actions from discovery. Secure MCP Tunnel
authenticates the development transport but does not make ChatGPT identity the
local OS identity or satisfy public submission. The staging HTTPS relay, OAuth
discovery, device pairing, grants, revocation, and audit boundaries are
implemented for testing. They do not establish production availability; the
production service must separately pass rate limiting, isolation, recovery,
privacy, and operational acceptance while preserving local source ownership.

Real ChatGPT Pro acceptance on 2026-08-15 exercised a project that was in
`HUMAN_REVIEW_REQUIRED`. The read-only profile returned the authoritative
state and refused a conversational request to approve the gate. This proves
the intended boundary at the real client/transport layer: controlled mutation
is not high-consequence human authorization, and natural-language intent is
not an authorization artifact.

## Production relay threat model

| Threat | Required production control |
| --- | --- |
| Prompt/tool-description/repository injection | Closed registry and schemas; repository text below engine policy; skill only guidance |
| Forged MCP request or actor | OAuth token validation plus adapter-fixed typed origin; reject caller actor/auth fields |
| Handle guessing, traversal, symlink escape | Opaque grant lookup followed by canonical local CW project resolution |
| Cross-project or cross-tenant confusion | Bind principal, tenant, device, project, tool, operation ID, and payload digest |
| OAuth/refresh token theft | Short access expiry, refresh rotation, revocation, audience/resource binding, secure storage |
| Replay or operation-ID substitution | Single canonical digest; identical replay only; conflicts and cross-project use rejected |
| Confused deputy/privilege escalation | Intersection of OAuth scope, workspace policy, project grant, capability manifest, and engine state |
| Human-approval impersonation | High-consequence ceremony outside normal tools and scopes; typed human, action/evidence binding, nonce, expiry |
| Malicious reviewer output | Independent read-only reviewer, strict schema/semantics, supervisor-only gate path |
| Secret/source/log leakage | Minimum-disclosure projections, deny raw paths/environment/source/logs, evidence references |
| Long operation disconnect/concurrency | Shared operation record, lock, replay, reconciliation, conservative cancellation |
| Revoked/stale access | Online revocation check before routing; local grant recheck before dispatch |
| Denial of service | Tenant/project rate limits, quotas, bounded payloads, timeouts, backpressure |
| Supply-chain/package tampering | Pinned CI actions/dependencies, deterministic plugin archive, hashes, signed-release plan |

Secure MCP Tunnel remains a development transport. A production public gateway
must separately pass TLS, OAuth, tenant isolation, revocation, rate-limit,
incident, and external acceptance gates before submission.
