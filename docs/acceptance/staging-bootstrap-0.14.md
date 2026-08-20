# CW 0.14 staging bootstrap acceptance

**Date:** 2026-08-16

**Version:** 0.14.0

**Baseline:** CW 0.13.0 at
`8fafe4a12cf082272d924135de5ba78123ce271e`

## Scope completed in repository

- portable non-root Docker gateway image with the existing `[remote]` extra;
- Render Blueprint for one staging web service, readiness health check,
  custom hostname registration, bounded limits, and a persistent disk;
- strict environment-to-gateway bootstrap with exact deploy SHA and sanitized
  health/readiness identity;
- Auth0 issuer/JWKS/resource/workspace-claim contract with no provider logic in
  CW Core or `CWApplication`;
- local-agent staging profile that requires explicit private credential/state
  files and a single explicit project/root;
- deployment, rollback, OAuth, agent, incident, backup/restore, rotation, and
  teardown runbooks;
- deterministic static, configuration, runtime, packaging, and secret-shape
  checks.

## Persistence decision

The chosen Render staging topology is exactly one instance with SQLite schema
v1 on an attached persistent disk. It is durable across restarts but has brief
deploy downtime and cannot scale horizontally. This is acceptable for staging
acceptance and explicitly not the production HA decision.

## Current evidence

Repository-side acceptance on 2026-08-16 produced:

- complete deterministic suite: 584 tests passed;
- remote gateway plus staging bootstrap: 33 tests passed;
- portable Docker image build and local container startup: passed;
- `/healthz`, `/readyz`, and unauthenticated `/mcp` challenge: passed;
- normal wheel, `[remote]` wheel/import, and local compatibility acceptance:
  passed;
- strict documentation and drift validators: passed;
- plugin archive SHA-256:
  `18f1a93045e8e157ae5a1076837d527592e6873e18d4551cdd9fa9dc9fc43b77`;
- isolated runtime dependency audit: no known vulnerability in resolved
  third-party packages (the unpublished local package is not in the public
  advisory index);
- real disposable CW 0.14 hero workflow with independent approved review and
  one valid gate: passed.

The following external evidence is still **NOT DEPLOYED** or **NOT EXERCISED**:

- Render service, custom-domain target, DNS, public TLS, and live readiness;
- Auth0 tenant/API, discovery, CIMD or DCR, real PKCE, and real token issuance;
- backup/restore exercise;
- local-agent pairing and opaque real project grant;
- public machine MCP/OAuth E2E;
- real ChatGPT public HTTPS acceptance.

No secret, tenant identifier, tunnel ID, local private path, device key, or
fictional public plugin connection is part of this evidence.

## Governance result

The staging bootstrap does not change `cw.remote.v1`, the accepted tool
registry, or CW workflow policy. Source and raw `.cw` stay local; the agent is
outbound-only; callers cannot select local paths; `HIGH_CONSEQUENCE_AUTHORIZATION`
remains absent. Render and Auth0 are staging adapters, not CW architecture
owners.
