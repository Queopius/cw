# Auth0 staging configuration for CW 0.14 remote gateway

This document is the repository-side staging configuration for a real CW 0.14
public endpoint. It is executable guidance for a human operator; no secrets are
stored in this repository.

## Scope

- Environment: `staging`
- Gateway: `https://staging-mcp.cwcli.dev`
- Gateway resource URL: `https://staging-mcp.cwcli.dev/mcp`
- Tenant/IdP: **Auth0** (dedicated staging tenant, not reused for production)
- Scope: prepare Auth0 only; no plugin submission, no deployment changes

## Reviewed references (2026-08-16)

- <https://developers.openai.com/plugins/concepts/plugins>
- <https://developers.openai.com/plugins/build/auth>
- <https://developers.openai.com/plugins/deploy/connect-chatgpt>
- <https://developers.openai.com/api/docs/guides/secure-mcp-tunnels>
- <https://auth0.com/ai/docs/mcp/guides/registering-your-mcp-client-application>
- <https://auth0.com/docs/get-started/applications/dynamic-client-registration>
- <https://auth0.com/docs/get-started/authentication-and-authorization-flow/authorization-code-flow-with-pkce>

No material model change invalidated CW 0.13/0.14 assumptions.

## Auth0 resource server contract (required)

Create one staging API and client configuration matching the values below.

### Resource/API

- **API name**: `CW Staging MCP`
- **Identifier / Audience**: `https://staging-mcp.cwcli.dev/mcp`
- **Signing algorithm**: `RS256`
- **Token lifetime**: short-lived access token (current policy assumes <= 10 minutes)
- **Permission model**: token scopes (not client-credentials token privileges)
- **RBAC**: optional only if claims remain scope-compatible with token validation
- **Add Permissions in access token**: optional; current gateway validates `scope`
- **Offline access**: use only if explicitly required; short absolute expiry preferred
- **Issuer discovery**: must expose standard OAuth AS metadata for discovery validation
- **JWKS URL**: HTTPS URL serving the tenant public signing keys

### Required scopes

CW runtime currently expects exactly these scopes (no `workflow.admin` equivalent):

- Read:
  - `project.read`
  - `gate.read`
  - `history.read`
  - `completion.read`
  - `operation.read`
- Execution / controlled actions:
  - `validation.execute`
  - `review.execute`
  - `phase.start`
  - `retry.execute`
  - `operation.cancel`

High-consequence capabilities are intentionally unavailable and no OAuth scope
must represent them.

## Workspace claim (required)

Gateway configuration expects:

- `CW_OAUTH_WORKSPACE_CLAIM=https://cwcli.dev/claims/workspace`

The claim must contain a stable string workspace identifier.

### Minimal Action (or equivalent claim source)

If Auth0 does not emit the required namespaced claim by default, attach one via an
Action on Post-Login:

- Input: tenant user/project context (for staging: explicit user/organization field)
- Output: emit a single string claim at `https://cwcli.dev/claims/workspace`
- Must fail closed if missing/invalid
- Must not authorize anything by repository content or model text
- Must not encode secrets

The claim is identity metadata only. CW grants remain in local project grants and
cannot be inferred from this claim alone.

## Client registration model

- Prefer **CIMD** when ChatGPT presents client-id metadata during client setup.
- If CIMD is not available, use **DCR** temporarily for a controlled staging
  window only.
- Create a separate browser OAuth application for human device-pairing
  confirmation if the ChatGPT client registration cannot be reused for an
  ordinary browser redirect.
- Pairing callback URL: `https://staging-mcp.cwcli.dev/remote/pair/callback`.
- Pairing login uses Authorization Code with PKCE S256 and stores no bearer
  token in the browser URL, terminal, repository, or `.cw` state.

If DCR is used:

1. Enable DCR only for the staging tenant registration endpoint.
2. Restrict to expected client type and redirect URIs for ChatGPT.
3. Require PKCE.
4. Record allowed registration window and close it afterward.
5. Keep existing registered clients valid; rotate/revoke as needed.
6. Revoke and disable clients explicitly when stale.

## Auth0 endpoints and metadata mapping

After save, capture these non-secret URLs:

- Issuer: tenant OpenID Connect issuer (e.g. `https://TENANT.eu.auth0.com/`)
- JWKS: `https://TENANT.eu.auth0.com/.well-known/jwks.json`

Set gateway environment variables:

- `CW_OAUTH_ISSUER_URL=<Issuer URL>`
- `CW_OAUTH_JWKS_URL=<JWKS URL>`
- `CW_PAIRING_WEB_CLIENT_ID=<Pairing browser application client ID>`
- `CW_PAIRING_WEB_REDIRECT_URI=https://staging-mcp.cwcli.dev/remote/pair/callback`
- `CW_PAIRING_SESSION_SECRET=<Render-managed random secret>`

### Render gateway variable policy (non-secret)

These are the Auth0 values in staging config:

- `CW_OAUTH_ISSUER_URL` (required, public metadata)
- `CW_OAUTH_JWKS_URL` (required, public metadata)
- `CW_OAUTH_WORKSPACE_CLAIM` (required, non-secret)
- `CW_OAUTH_ALGORITHMS` (required, non-secret, e.g. `RS256`)
- `CW_PAIRING_WEB_CLIENT_ID` (required, public client identifier)
- `CW_PAIRING_WEB_REDIRECT_URI` (required, public callback URL)

`CW_PAIRING_SESSION_SECRET` is the only gateway-managed secret in the staging
contract. It signs short-lived browser pairing cookies and must be generated in
Render. No OAuth client secret is required by the gateway for token validation
because verification uses public key metadata (JWKS).

## Validation checks (must pass before pairing)

Run these checks after configuration:

- Protected-resource metadata reachable: `/.well-known/oauth-protected-resource`
- Pairing page reachable: `/remote/pair`
- Authorization server discovery reachable and consistent with issuer
- PKCE `S256` advertised
- `resource` equals gateway resource URL
- `issuer` matches configured issuer exactly
- token scope list is restricted to expected scopes
- `HTTP`-scheme issuer/jwks URLs rejected (must be HTTPS)
- `cimd` advertised or `registration_endpoint` available for DCR
- project grant resolution remains explicit and opaque-handle based

## Failure handling

The gateway is fail-closed:

- missing token -> `AUTHENTICATION_REQUIRED`
- malformed/unsupported token -> `TOKEN_INVALID`
- wrong issuer/audience -> `TOKEN_INVALID`
- missing/malformed workspace claim -> `TOKEN_INVALID`
- expired token -> `TOKEN_EXPIRED`
- revoked token -> `TOKEN_INVALID`
- missing scope -> `SCOPE_REQUIRED`

No secret is stored in the repository for these operations.

## Revocation and teardown

- disable/revoke the ChatGPT client registration;
- revoke user grants where applicable;
- rotate OAuth keys per Auth0 policy;
- locally revoke token IDs when fixture support is available;
- revoke paired device/project grants when local access must be blocked.

## Render variable map

Set these values only from non-secret sources in Render:

- `CW_OAUTH_ISSUER_URL`
- `CW_OAUTH_JWKS_URL`
- `CW_OAUTH_WORKSPACE_CLAIM`
- `CW_OAUTH_ALGORITHMS`
- `CW_PAIRING_WEB_CLIENT_ID`
- `CW_PAIRING_WEB_REDIRECT_URI`
- `CW_PAIRING_SESSION_SECRET`

Keep all of the following out of docs and code:

- client secrets, Management API tokens, private keys, repository tokens, local private
  file paths, tunnel IDs

## Human action after this document

From this repository side, once the artifact changes are committed and tests pass,
human actions remain:

1. create a staging Auth0 tenant (not in-code),
2. create API and token policy with the exact audience and scopes above,
3. configure required claim emission,
4. register ChatGPT client (CIMD first, DCR only if required),
5. register the browser pairing callback URL,
6. fill `CW_OAUTH_*` and `CW_PAIRING_*` values in Render.
