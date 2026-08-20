# CW 0.13 Remote Gateway + OAuth implementation acceptance

**Date:** 2026-08-15

**Version:** 0.13.0

**Milestone class:** local implementation candidate; no public deployment

## Completion target

Prove a real, typed path from an OAuth-protected Streamable HTTP MCP gateway
through a paired outbound local agent to the existing `CWApplication`, while
keeping repository source, raw `.cw`, workflow policy, and high-consequence
authority local.

## Deterministic evidence

The multi-process-equivalent HTTP E2E fixture starts a signed OAuth identity,
ASGI/FastMCP gateway, transactional store, paired device, outbound HTTP agent,
initialized disposable CW repository, and MCP client. It proves:

- protected-resource metadata, token challenge, issuer/signature/audience/
  expiry/revocation and scope enforcement;
- PKCE S256 and CIMD/DCR authorization-server compatibility contracts;
- pairing success, expiry, single-use replay denial, device revocation, signed
  nonces, and actor substitution denial;
- opaque local project grants, tenant/device binding, unknown/revoked handle
  denial, and no path/source/secret leakage;
- all six read tools with semantic parity to local MCP;
- existing phase start, configured validation, independent review, retry,
  operation polling, and safe queued cancellation through the remote route;
- restart/reconnect followed by same-digest client replay without a duplicate
  local transition;
- rejection of arbitrary phase/command/review decision, shell/filesystem/Git,
  gate approval, extension authorization, repair, release, and deployment.

Local candidate evidence on 2026-08-15:

- complete suite: 578 tests passed with the MCP/remote dependencies installed;
- remote gateway/auth/agent suite: 27 tests passed;
- normal wheel, `[mcp]` wheel acceptance, and `[remote]` wheel E2E: passed;
- plugin/skill/static contract validators and strict documentation build:
  passed;
- real disposable Codex hero workflow: completed with one independently
  reviewed valid gate and satisfied Completion Contract;
- plugin archive SHA-256:
  `74078ba01f6021e22e90435594790de97e3460142fef94dc681f6a60737ab57c`;
- isolated installed `[remote]` dependency audit: no known vulnerability in
  resolved third-party packages (the unpublished local `codex-workflow`
  package itself is not present in the public advisory index).

## Platform verification

Linux local deterministic acceptance is recorded by the final repository run.
CI is configured to install `codex-workflow[remote]` and execute the remote E2E
on Linux x86_64, Windows x86_64, macOS arm64, and macOS Intel. Those native
jobs are not claimed until the exact candidate is pushed and run.

The MCP SDK currently emits an upstream `pydantic_settings` incomplete-field
warning during server construction, and Starlette warns that its legacy test
client is deprecated. Both are non-blocking third-party noise: protocol and
HTTP E2E remain green, and 0.13 does not suppress or patch dependency internals.

## External status

**REAL CHATGPT PUBLIC GATEWAY ACCEPTANCE: NOT RUN.** There is no deployed
domain, public TLS endpoint, production identity provider, domain verification,
or Plugin Directory submission. This does not block the implementation
candidate, but it blocks production deployment and plugin submission.

## Conclusion

The candidate preserves “No valid gate. No next phase,” Completion Contracts,
independent review, local-first operation, and the distinction between
technical capability and governance authority. The recommended next milestone
is CW 0.14 — Public Staging Deployment & ChatGPT Acceptance.
