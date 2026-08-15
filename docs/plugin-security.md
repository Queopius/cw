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

## ChatGPT development and production gap

The development profile fixes `chatgpt_app` origin, requires explicit project
grants, and can omit all controlled actions from discovery. Secure MCP Tunnel
authenticates the development transport but does not make ChatGPT identity the
local OS identity or satisfy public submission. A future public HTTPS relay
still requires OAuth, device pairing, per-user grants, revocation, audit
boundaries, rate limiting, and local source ownership.
