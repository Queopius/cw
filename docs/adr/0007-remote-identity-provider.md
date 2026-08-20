# ADR 0007: Remote identity-provider strategy

**Status:** Accepted for the CW 0.13 candidate

## Decision

CW is an OAuth 2.1 MCP protected resource, not an authorization server or user
database. Integrate an established provider through issuer discovery and JWKS,
requiring Authorization Code + PKCE S256 and CIMD or DCR compatibility.

## Consequences

Token validation is vendor-neutral and tests use deterministic signed fixture
tokens. A real provider, domain, consent configuration, client registration,
refresh/revocation integration, and operational ownership remain 0.14 staging
work. OAuth scope never implies CW state eligibility or high-consequence human
authority.
