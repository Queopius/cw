# Auth0 staging configuration

Auth0 is the approved CW 0.14 staging authorization server. It is not a CW
Core dependency or a permanent production-vendor commitment. This procedure
is prepared but **NOT EXERCISED** until a human creates the tenant.

## Current contracts

OpenAI's current plugin authentication guide requires an OAuth 2.1 MCP flow,
protected-resource metadata, authorization-server discovery, the `resource`
parameter, Authorization Code with PKCE S256, and CIMD, DCR, or a predefined
client. ChatGPT prefers CIMD when the authorization server and plugin builder
select it. Auth0 recommends manual CIMD registration for production MCP and
supports DCR as an alternative. Auth0 DCR is disabled by default and is open
registration when enabled, so it must not be enabled casually.

Reviewed 2026-08-16:

- <https://developers.openai.com/plugins/build/auth>
- <https://developers.openai.com/plugins/deploy/connect-chatgpt>
- <https://auth0.com/ai/docs/mcp/guides/registering-your-mcp-client-application>
- <https://auth0.com/docs/get-started/applications/dynamic-client-registration>
- <https://auth0.com/docs/get-started/authentication-and-authorization-flow/authorization-code-flow-with-pkce>

## Tenant and API

1. Create a dedicated staging tenant. Do not reuse a production tenant.
2. Create the API/resource server with identifier
   `https://staging-mcp.cwcli.dev/mcp`, RS256 signing, and a short access-token
   lifetime appropriate for staging.
3. Add the exact CW scopes: `project.read`, `gate.read`, `history.read`,
   `completion.read`, `operation.read`, `validation.execute`, `review.execute`,
   `phase.start`, `retry.execute`, and `operation.cancel`.
4. Do not add `workflow.admin`, a wildcard write scope, or any
   `HIGH_CONSEQUENCE_AUTHORIZATION` scope.
5. Add a post-login Action that derives a stable CW workspace identifier from
   approved tenant/app metadata and emits it only as the namespaced
   `https://cwcli.dev/claims/workspace` access-token claim. Repository content
   and conversation text cannot populate the claim.
6. Confirm the issuer, JWKS URL, authorization endpoint, token endpoint,
   `S256`, and token authentication methods in Auth0 discovery metadata.
7. Configure the resource-parameter compatibility profile so the canonical
   CW resource is preserved in authorization and token issuance.

## Client registration

Prefer manual CIMD registration after ChatGPT displays the exact
MCP-specific client metadata URL and callback URI. Import that exact HTTPS
CIMD in Auth0 and allow only the callback shown by ChatGPT. Select public-client
`none` with PKCE or `private_key_jwt` only when the observed Auth0/OpenAI
metadata intersection supports it.

If the tested ChatGPT flow cannot use CIMD, DCR is a documented fallback:

- configure default third-party API permissions narrowly;
- promote only the required login connection to domain level;
- enable Auth0 DCR only for the acceptance window and apply tenant ACL/rate
  controls where available;
- verify DCR creates a third-party client with mandatory PKCE;
- disable DCR after registration unless ongoing connector creation requires it.

Never copy a returned client secret into Git or chat. The gateway itself does
not need that secret.

## Revocation

Access expires according to Auth0 token policy. Revoke the ChatGPT client or
its grants in Auth0, revoke the CW token ID locally where available, and revoke
the CW device/project grant independently. Authentication, project access,
controlled mutation, and high-consequence authority remain separate.
