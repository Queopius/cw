# ADR 0010: Render staging hosting and persistence

**Status:** Accepted for CW 0.14 staging bootstrap

**Date:** 2026-08-16

## Context

CW 0.13 selected a hosting-neutral public gateway with an outbound-only local
agent. CW 0.14 needs a real, non-production endpoint. The operator selected
Render and `staging-mcp.cwcli.dev` for staging; neither selection binds the
production architecture.

Render supports Docker web services, public HTTPS, custom domains, HTTP health
checks, WebSockets, rollback, and Blueprint configuration. Its persistent
disks are encrypted and snapshotted daily, but a disk is attached to one
instance, prevents horizontal scaling, and disables zero-downtime deploys.

## Decision

Deploy the existing ASGI gateway as a portable Docker web service on one
Render Starter instance. Terminate TLS at Render, bind the container to
Render's `PORT`, use `/readyz` as the health check, and register the staging
custom hostname. The image and gateway contain no Render SDK.

Use the existing transactional SQLite schema on a 1 GB persistent disk at
`/var/lib/cw` for this single-instance staging environment. This is not the
production multi-instance persistence decision. It avoids an unproven new
database adapter while preserving pairing, grant, nonce, routing, revocation,
and audit metadata across restarts.

## Consequences

- Staging has one gateway instance and brief deployment downtime.
- A scale-out or high-availability gateway requires a shared transactional
  backend, expected to be PostgreSQL behind the existing repository boundary.
- Disk backup must be quiesced or use Render's snapshot facility; the gateway
  database never contains repository source or raw `.cw`.
- `render.yaml` is staging infrastructure-as-code. Production provider and
  topology remain open decisions.
- DNS values are not guessed. The operator copies the exact target Render
  displays after the custom domain is registered.

## Sources

Reviewed 2026-08-16:

- <https://render.com/docs/web-services>
- <https://render.com/docs/blueprint-spec>
- <https://render.com/docs/disks>
- <https://render.com/docs/health-checks>
- <https://render.com/docs/custom-domains>
