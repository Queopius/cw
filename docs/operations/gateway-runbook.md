# Gateway runbook

## Diagnose

- `/healthz` proves the process answers.
- `/readyz` proves schema v1 is readable and reports sanitized build identity.
- `401` plus `WWW-Authenticate` is expected without a bearer token.
- `AGENT_OFFLINE` means authentication and project grant succeeded but the
  paired local agent is unavailable.
- `SCOPE_REQUIRED`, `PROJECT_NOT_GRANTED`, and `DEVICE_REVOKED` are distinct
  security decisions, not workflow failures.

Correlate using request, operation, principal, workspace, device, project
handle, capability, actor, and origin identifiers. Never log bearer tokens,
source, raw `.cw`, local paths, or raw request bodies.

## Respond

For readiness failure, stop traffic and inspect database availability/schema.
For elevated authentication or scope failures, preserve sanitized audit data
and verify Auth0 configuration before changing policy. For tenant/project
violations, revoke affected tokens, devices, and grants first. For repeated
timeouts, check agent availability and limits; do not loosen CW locks.

Render's single-instance disk topology has brief deploy downtime. Restore the
last known-good deploy if a new image fails readiness. Do not fabricate an
operation result during an outage.
