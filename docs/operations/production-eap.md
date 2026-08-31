# Production Early Access operations

This runbook applies only to the separate `cw-mcp` production service and its
`cw-production-data` disk. It never operates on the staging service or disk.

CW Plugin `0.1.0` Production Early Access uses Core `0.18.3` and remote
protocol `cw.remote.v1`. This repository prepares the production deployment;
it does not deploy, publish, tag, create DNS, or modify the identity provider.

## Architecture and identity

The gateway is a separate Render Starter web service named `cw-mcp`, built
from the existing `Dockerfile` and tracking only governed branch `prod`.
Candidates travel `dev → staging → release → prod`; the service must never
deploy an unpromoted `dev` commit. It runs exactly one instance with SQLite
schema 1 on the dedicated disk described below.

| Contract | Production value |
| --- | --- |
| MCP resource | `https://mcp.cwcli.dev/mcp` |
| OAuth issuer | `https://auth.cwcli.dev/` |
| JWKS | `https://auth.cwcli.dev/.well-known/jwks.json` |
| Workspace claim | `https://cwcli.dev/claims/workspace` |
| Initial workspace | `cw-production` |
| Pairing entry | `https://mcp.cwcli.dev/remote/pair` |
| Pairing callback | `https://mcp.cwcli.dev/remote/pair/callback` |

Auth0 is a US-region tenant tagged Production. `CW Pairing` is a public OAuth
client using Authorization Code with PKCE `S256`, delegated scope
`project.read`, and no client secret. External subjects remain opaque and are
normalized into issuer-bound CW principal IDs. OAuth possession is never
high-consequence human authorization.

Anonymous `GET /mcp` must return 401 and advertise the production protected
resource. Its scopes are exactly `project.read`, `gate.read`, `history.read`,
`completion.read`, `operation.read`, `phase.start`, `validation.execute`,
`review.execute`, `retry.execute`, and `operation.cancel`. There is no approval,
extension authorization, repair, rebaseline, release, deployment, shell,
filesystem, or generic-command scope.

The repository file `render.production.yaml` defines non-secret values.
`CW_GATEWAY_ALLOWED_HOSTS` is set externally after Render assigns the exact
hostname and must equal `mcp.cwcli.dev,<exact-production-host>.onrender.com`.
`CW_PAIRING_WEB_CLIENT_ID` is externally supplied and
`CW_PAIRING_SESSION_SECRET` must be a new provider-managed production secret.
`RENDER_GIT_COMMIT` is the sole 40-character build identity.

`/healthz` returns 200 for the gateway process and reports service
`cw-remote-gateway` and environment `production`. `/readyz` returns 200 only
when SQLite schema 1 is usable and reports Core `0.18.3`, Plugin `0.1.0`,
protocol `cw.remote.v1`, production environment, and the exact deploy SHA.

After the candidate reaches `prod`, create the service from the Blueprint, add
`mcp.cwcli.dev` in Render, copy the exact displayed DNS target to the DNS
provider, verify the domain, and wait for managed TLS. Never guess the Render
hostname. Then verify health/readiness, anonymous MCP 401, OAuth metadata, and
a fresh pairing/grant/agent/read acceptance before opening the cohort.

Production must not reuse any staging service, host, disk, database, workspace,
OAuth client, session secret, device credential, token, DCR registration, or
Render credential. Staging remains independently usable.

## Bounded persistence

The invite-only, low-concurrency EAP uses one instance and one dedicated disk
mounted at `/var/lib/cw`; its database is `/var/lib/cw/gateway.sqlite3`. This is
single-instance and not highly available; it does not support horizontal
scaling. Deploys have planned downtime; in-flight operations may fail; agents
reconnect; durable devices, grants,
nonces, request digests, revocations, and audit records persist; in-memory
queues, cache, rate counters, and presence may be lost. Monitor disk growth.
PostgreSQL is not implemented for this bounded cohort.

## Preflight

Record the production deploy SHA from `RENDER_GIT_COMMIT`. Confirm it is the
validated `prod` commit, the service has exactly one instance, the disk is
mounted at `/var/lib/cw`, and readiness reports Core `0.18.3`, Plugin `0.1.0`,
protocol `cw.remote.v1`, environment `production`, schema `1`, and the same
40-character SHA. Confirm the EAP remains invite-only and low concurrency.

## Verified SQLite backup

Render disk snapshots are useful provider recovery material but are not the CW
logical backup or restore test. Before launch, deployment, migration, or risky
maintenance:

1. Disable production ingress and wait for in-flight requests to drain. Stop
   the gateway process so this is a quiesced maintenance window.
2. From an authorized production maintenance shell with an existing destination
   directory on separate approved storage, run the repository utility (replace
   the placeholders with the exact UTC timestamp and recorded SHA):

   ```bash
   python -m cw.remote.backup backup \
     --source /var/lib/cw/gateway.sqlite3 \
     --output /approved-backup-location/cw-gateway-<UTC>.sqlite3 \
     --deployed-sha <40-character-production-sha>
   ```

   It uses Python's standard SQLite online-backup API, refuses overwrite, sets
   owner-only permissions where supported, and writes a sibling JSON manifest.
   Never use a live filesystem `cp` of the database.
3. Open the backup read-only and run `PRAGMA integrity_check`; the sole row must
   be `ok`. Query `SELECT MAX(version) FROM schema_migrations`; it must be `1`.
4. Calculate SHA-256 over the completed backup file.
5. Record, without secrets or tokens: UTC timestamp, schema version, deployed SHA,
   backup SHA-256, integrity result, byte size, encrypted storage location,
   operator, and retention/expiry.
6. Resume the same production SHA only after the backup record is complete.

The backup contains sensitive identity, device, grant, nonce, routing,
revocation, and audit metadata. Encrypt it, restrict access, and never commit
it to Git or place it in Plugin artifacts.

## Isolated restore verification

Never test a restore over the production database.

1. Provision an isolated, non-networked temporary volume and copy the verified
   backup there.
2. Verify its recorded SHA-256 before opening it.

   ```bash
   python -m cw.remote.backup verify \
     --backup /isolated-restore/cw-gateway.sqlite3 \
     --sha256 <recorded-backup-sha256>
   ```
3. Start the exact compatible container SHA with ingress disabled and point
   `CW_GATEWAY_DATABASE` at the isolated copy.
4. Confirm `PRAGMA integrity_check` is `ok`, schema version is `1`, and readiness
   reports the expected versions and the test SHA.
5. Using disposable fixture identities only, verify uniqueness, device/grant
   revocation, nonce replay rejection, routed-request idempotency, and restart
   recovery. Do not connect real agents or issue real OAuth tokens.
6. Destroy the isolated restored copy through the approved secure disposal
   process and record the outcome. A backup is not launch evidence until this
   restore exercise passes.

## First-production rollback

If the first deployment fails, disable production ingress/service, preserve
the production disk unchanged, and disconnect the production ChatGPT
connection if clients could still reach it. Staging stays online and unchanged.
There is no previous production SHA on the first deployment: the safe rollback
is service disablement plus diagnosis.

For later code rollbacks, record both current and target SHAs and prove that the
target understands every schema version present on disk before selecting the
previous validated Render deploy. CW schema changes must be backward-compatible
for code rollback. Agents disconnect during downtime and reconnect afterward;
in-flight operations may fail and must be reconciled from local `.cw` evidence.

Restore the database only for proven loss or corruption, never merely to undo
a code deploy. Preserve the damaged database and audit evidence first. On
suspected compromise, keep ingress disabled and revoke/rotate the affected
OAuth, Render, pairing-session, and device credentials through their owning
systems before recovery.

## Revocation boundary

The persistence and gateway service layers implement transactional device and
individual project-grant revocation, including audit events and cascade of a
device revocation to its grants. However, Core `0.18.3` exposes no supported
authenticated operator endpoint or public `cw remote revoke` command.

Therefore **operator device revocation and individual project-grant revocation
are operationally BLOCKED for Production EAP**. Do not edit SQLite directly and
do not invoke internal Python methods as an improvised production interface.
Opening the production cohort is blocked until a separately reviewed,
authenticated, auditable operator primitive and its recovery tests exist.
