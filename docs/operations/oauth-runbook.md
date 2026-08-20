# OAuth staging runbook

Follow [Auth0 staging configuration](../auth0-staging.md). Verify discovery,
issuer, JWKS, resource/audience, PKCE S256, client-registration mode, callback,
and scopes before connecting a real project.

When authentication fails, classify separately:

- no token: `AUTHENTICATION_REQUIRED`;
- invalid signature/issuer/audience: `TOKEN_INVALID`;
- expired token: `TOKEN_EXPIRED`;
- missing CW scope: `SCOPE_REQUIRED`;
- valid token but no project grant: `PROJECT_NOT_GRANTED`.

Do not solve an OAuth failure by broadening scopes. ChatGPT confirmation and a
valid access token never constitute `HIGH_CONSEQUENCE_AUTHORIZATION`.

To revoke access, disable/revoke the Auth0 client or user grant, revoke token
IDs locally when present, revoke the CW project grant, and revoke the paired
device as needed. Validate denial after cached access tokens expire or are
locally denied.
