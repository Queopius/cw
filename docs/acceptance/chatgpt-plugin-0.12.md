# CW 0.12 ChatGPT plugin acceptance matrix

Recorded 2026-08-15. This is a deterministic candidate matrix plus a truthful
external-status record. It does not claim a deployed public endpoint.

## Final technical baseline evidence

The accepted implementation candidate is
`45f89472a0d61effc6e1860960c3d3facf6f03cb` on `dev`. It passed the complete
local gate and exact-SHA GitHub acceptance:

| Evidence | Result |
| --- | --- |
| Local unit suite | 551 passed; 4 expected optional skips |
| MCP/plugin/ChatGPT/security suite | 80 passed |
| Normal and `[mcp]` installed wheels | PASS |
| Strict docs and drift validation | PASS |
| Deterministic plugin archive | `c2551fa172fdebd4b7e7bb18997c60cbfa1b55f22995d7d3afe453a90d8754b6` |
| [CI run 31896686105](https://github.com/Queopius/cw/actions/runs/31896686105) | PASS on the accepted candidate SHA |
| [Platform Acceptance 31896686084](https://github.com/Queopius/cw/actions/runs/31896686084) | Linux x86_64, Windows x86_64, macOS arm64, and macOS Intel PASS |

This closes the **CW 0.12 technical baseline as ACCEPTED**. It does not change
the separate conclusions that production readiness is **NOT READY** and plugin
submission readiness is **BLOCKED**.

## Package and architecture

| Case | Expected | Status |
| --- | --- | --- |
| Current manifest/skill/assets | Official current package validates | PASS |
| Deterministic archive | Same bytes and SHA-256 on repeated build | PASS |
| MCP registry parity | Plugin declares exactly the accepted CW tools | PASS |
| Production transport | Public streamable HTTPS, not tunnel | DESIGNED, NOT IMPLEMENTED |
| OAuth | OAuth 2.1 MCP contract with PKCE/scopes/revocation | DESIGNED, NOT IMPLEMENTED |

## Read positive

Status, current phase, gates, history, Completion Contract, and blocker
explanation are covered by deterministic raw-MCP parity and the real CW 0.11
ChatGPT Pro read-only acceptance. **PASS.**

## Read negative

Unknown and unauthorized projects, arbitrary paths, traversal/symlink escape,
malformed handles, prompt injection, privacy leakage, and cross-project reads
must fail closed. Existing deterministic MCP/ChatGPT tests cover these cases.
**PASS.**

## Controlled actions

| Positive/negative case | Status |
| --- | --- |
| Authorized current-phase start | PASS in local MCP/fake-Codex |
| Configured validation only | PASS in local MCP/fake-Codex |
| Independent review and supervisor-only gate | PASS in local MCP/fake-Codex |
| Retry, operation status, queued cancellation | PASS in local MCP |
| Arbitrary phase/command/path/review decision/gate | REJECTED |
| Cross-project, conflicting ID, duplicate replay | PASS / safely rejected |
| ChatGPT Pro production controlled actions | NOT RUN; read-only default policy |

`PLATFORM_CAPABILITY_UNAVAILABLE`, CW policy denial,
`AUTHORIZATION_REQUIRED`, scope violations, operation conflicts, project
conflicts, and infrastructure failures remain different results.

## High-consequence negative

Direct human gate approval, extension authorization, release/deployment,
destructive repair/rebaseline, and fabricated evidence are absent from both
plugin discovery and server dispatch. Conversation and ordinary OAuth scopes
cannot create authorization. **PASS deterministically; real read-only human
gate refusal PASS in CW 0.11.**

## Disconnect and recovery

Local stdio and tunnel restart, identical replay, payload conflict,
cross-project operation IDs, stale supervisors, locking, and conservative
cancellation pass existing deterministic acceptance. The future public gateway
must rerun these through HTTPS/OAuth and add tenant, revocation, and relay-loss
coverage. **LOCAL PASS; PUBLIC NOT RUN.**

## External status

- Real ChatGPT Developer Mode read-only: **PASS** (CW 0.11 evidence).
- Secure MCP Tunnel backward compatibility: **PASS**.
- Public HTTPS MCP: **NOT IMPLEMENTED / NOT RUN**.
- Production OAuth and pairing: **NOT IMPLEMENTED / NOT RUN**.
- OpenAI submission: **NOT AUTHORIZED / NOT RUN**.

## Decision

The 0.12 package, architecture, policy, and evidence baseline is **ACCEPTED**,
but public production remains **NOT READY** and submission remains **BLOCKED**.
The structured companion is
[`plugin-production-readiness-evidence.json`](../plugin-production-readiness-evidence.json).
