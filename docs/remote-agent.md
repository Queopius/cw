# CW Remote local agent

The CW Remote agent is a CW component, not a fork or dependency of OpenAI's
Secure MCP Tunnel client. It opens outbound HTTPS long polls to the gateway;
the CW machine requires no public inbound listener.

At startup the local operator supplies a paired device credential and a local
grant file. Each grant maps one opaque `cwp_…` handle to a canonical initialized
CW repository. Remote callers cannot supply or discover filesystem paths.
Before every request the agent verifies protocol version, device, principal,
workspace, handle, request digest, deadline, and closed tool schema. It then
calls the existing local `MCPRuntime`, which calls `CWApplication`.

Device requests are Ed25519-signed with a timestamp, nonce, HTTP method, path,
and body digest. The private key stays local. The portable fallback credential
store uses owner-only file permissions; the abstraction can later use Windows,
macOS, or Linux credential stores without changing the protocol.

Reconnect uses bounded backoff. Duplicate delivery retains the original
operation ID and canonical digest, so local application idempotency remains
authoritative. Authentication or revocation failures stop rather than hiding
behind an endless reconnect loop.

The agent does not expose shell, filesystem, Git, reviewer decisions, gates,
repair, rebaseline, extension authorization, release, or deployment.

The 0.14 staging profile points the same client at
`https://staging-mcp.cwcli.dev` through operator-supplied configuration. It
does not embed that URL in workflow logic, grant a home directory, or discover
projects. See the [agent staging runbook](operations/agent-runbook.md).
