# CW staging environment contract

CW 0.14 prepares, but has **NOT DEPLOYED**, the Render/Auth0 staging service.
The machine-readable source of truth is
[`config/staging-environment.json`](https://github.com/Queopius/cw/blob/dev/config/staging-environment.json).
This page explains how those values are owned and operated.

## Gateway and persistence

| Variable | Required | Secret | Owner | Purpose |
|---|---:|---:|---|---|
| `CW_DEPLOYMENT_ENV` | yes | no | CW | Sanitized environment label (`staging`) |
| `RENDER_GIT_COMMIT` | yes | no | Render | Exact deployed 40-character Git SHA |
| `PORT` | platform | no | Render | Container bind port; defaults to 10000 |
| `CW_GATEWAY_RESOURCE_URL` | yes | no | CW | Canonical resource URL, including `/mcp` |
| `CW_GATEWAY_DATABASE` | yes | no | CW | Absolute database path on the persistent disk |
| `CW_GATEWAY_HOST` | no | no | CW | Container bind address |
| `CW_GATEWAY_ALLOWED_HOSTS` | yes | no | CW | DNS-rebinding allowlist |
| `CW_GATEWAY_DOCUMENTATION_URL` | no | no | CW | RFC 9728 resource documentation |

## OAuth and Auth0

| Variable | Required | Secret | Owner | Purpose |
|---|---:|---:|---|---|
| `CW_OAUTH_ISSUER_URL` | yes | no | Auth0 admin | Exact tenant issuer |
| `CW_OAUTH_JWKS_URL` | yes | no | Auth0 admin | Tenant JWKS endpoint |
| `CW_OAUTH_WORKSPACE_CLAIM` | yes | no | CW/Auth0 | Namespaced workspace claim |
| `CW_OAUTH_ALGORITHMS` | yes | no | CW/Auth0 | Accepted asymmetric JWT algorithms |
| `CW_PAIRING_WEB_CLIENT_ID` | yes | no | Auth0 admin | Browser OAuth client for human pairing confirmation |
| `CW_PAIRING_WEB_REDIRECT_URI` | yes | no | CW/Auth0 | `https://staging-mcp.cwcli.dev/remote/pair/callback` |
| `CW_PAIRING_SESSION_SECRET` | yes | yes | Render secret | HMAC secret for short-lived pairing session cookies |

The gateway is an OAuth resource server and needs no OAuth client secret,
Auth0 Management API credential, or signing private key. The only gateway
secret in staging is `CW_PAIRING_SESSION_SECRET`; it signs HttpOnly browser
pairing cookies and must be generated inside Render. Provider credentials must
not be added to Render, Git, plugin metadata, or CW project state.

## Limits

`CW_LIMIT_REQUESTS_PER_MINUTE`, `CW_LIMIT_DEVICE_REQUESTS_PER_MINUTE`,
`CW_LIMIT_PAIRING_REQUESTS_PER_MINUTE`, `CW_LIMIT_CONCURRENT_PER_DEVICE`,
`CW_LIMIT_REQUEST_BYTES`, `CW_LIMIT_AGENT_MESSAGE_BYTES`,
`CW_LIMIT_OPERATION_TIMEOUT_SECONDS`, `CW_LIMIT_AGENT_IDLE_SECONDS`, and
`CW_LIMIT_COMPLETED_CACHE` have the bounded defaults recorded in the JSON
contract and `render.yaml`. All values must be positive.

## Validation rules

Startup fails closed when a required value is absent, a URL is not HTTPS, the
database path is relative, the build SHA is not exact, or a limit is invalid.
`/healthz` and `/readyz` expose only service, version, protocol, environment,
schema, and build SHA—never issuer details, host internals, paths, or secrets.

The current gateway secret set is empty. Provider credentials used by a human
to manage Render, Auth0, or DNS stay in those provider control planes.
