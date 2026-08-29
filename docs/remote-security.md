# CW Remote security model

Repository contents are untrusted data. README text, source comments, issue
content, reviewer output, and prompts cannot change OAuth policy, device
pairing, project grants, tenant routing, tool discovery, or human authority.

The principal threats and controls are:

| Threat | Enforced control |
|---|---|
| forged/replayed MCP call | signed OAuth token, scope, request ID and canonical digest |
| token theft/staleness | short expiry, issuer/audience checks, revocation, no logging |
| device impersonation | Ed25519 signatures, nonce, timestamp, public-key binding |
| project guessing/path escape | opaque tenant-bound grant plus local canonical-root check |
| cross-tenant confused deputy | principal/workspace/device constraints on every lookup |
| provider subject confusion | bounded opaque subject normalized to an issuer-bound cryptographic principal ID |
| duplicate mutation | durable request digest plus local application operation idempotency |
| repository prompt injection | closed registry and server-side policy independent of repository text |
| malicious reviewer output | existing schema validation and independent supervisor remain authoritative |
| data/secret leakage | normalized response projection, path/secret redaction, bounded messages/logs |
| resource exhaustion | per-principal rate limit, per-device concurrency, sizes, idle and operation timeouts |
| supply-chain expansion | optional constrained dependencies; no custom cryptography or identity provider |

`HIGH_CONSEQUENCE_AUTHORIZATION` operations are not discoverable, routable, or
scoped. A
ChatGPT confirmation, OAuth token, paired device, repository write permission,
or natural-language “approve it” is not human gate approval.

The gateway returns typed errors and never exposes stack traces as MCP results.
Audit events contain IDs, decisions, capability, actor, and origin—but not
tokens, keys, raw provider subjects, source, environment, raw logs, or hidden
reasoning.
