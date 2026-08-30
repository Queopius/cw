# Staging deployment procedure

This runbook has been exercised for the portable CW gateway on the authorized
Render staging service. As of 2026-08-29, health/readiness report Core `0.18.3`,
Plugin `0.1.0`, `cw.remote.v1`, and source SHA
`64830ae85b1df4c751d62dc0c4b24b2e1f1a3fc0`. This is not production.

1. Confirm the candidate reached `staging` through a governed `dev → staging`
   pull request, all required checks are green, and record the exact staging SHA.
2. In Render, create a Blueprint from `render.yaml` and the Queopius CW
   repository. Review the paid Starter instance and 1 GB disk before approval.
3. Enter the non-secret Auth0 issuer and JWKS values only after the staging
   tenant exists. Do not enter management credentials.
4. Deploy and record Render service/deploy identifiers, exact Git SHA, and
   image/build identity from `/readyz`.
5. Add the custom domain in Render. Copy the exact DNS target Render displays;
   do not infer it from the service name.
6. Add only the `staging-mcp` DNS record. Do not modify the apex, `www`, or
   `docs` records.
7. Wait for Render domain verification and managed TLS. Verify HTTP redirects,
   HTTPS, `/healthz`, `/readyz`, `/mcp`, and protected-resource metadata from
   outside the local machine.
8. Run machine OAuth/MCP acceptance before pairing a real local agent or
   connecting ChatGPT.

Render auto-deploy is `checksPass` and the service source branch is `staging`:
a commit becomes eligible for public staging only after the governed
`dev → staging` promotion and required checks pass. A direct push or merge to
`dev` must never deploy public staging. The persistent disk makes deploys
briefly unavailable; schedule staging acceptance accordingly.

The live Render service branch setting is provider-managed. After changing the
repository Blueprint, an operator must separately verify that `cw-staging-mcp`
tracks `staging`; repository configuration alone does not update an existing
service automatically.

## Rollback

Select the last known-good deploy in Render and redeploy it. Verify `/readyz`
reports that exact SHA and schema version. Schema v1 is backward compatible
through 0.13/0.14; do not restore a database snapshot merely to roll back
application code. If schema compatibility ever changes, follow a dedicated
migration/rollback plan.

## Teardown

Revoke project grants and the paired device, disconnect ChatGPT, disable the
Auth0 client/DCR, export any required sanitized audit evidence, remove the
custom domain, then delete the Render service/disk and staging Auth0 tenant.
