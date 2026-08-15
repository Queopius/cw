# CW Remote operations and recovery

The `cw.remote.v1` lifecycle separates authentication, authorization,
availability, routing, and local workflow results.

Gateway limits are configurable: requests per principal/workspace and device,
public pairing attempts per device/network, concurrent
requests per device, request and agent-message size, agent idle time, and
operation timeout. Local CW locks and operation conflicts remain authoritative;
the gateway cannot create unlimited validation or reviewer sessions.

Request replay uses `(workspace, request_id)` plus a canonical tool/arguments
digest. Same ID and payload is idempotent. Same ID with a different payload is
`OPERATION_CONFLICT`. Operation IDs are preserved through gateway, agent, and
`CWApplication`.

Recovery behavior:

- agent offline returns `AGENT_OFFLINE`, not “project missing” or workflow
  failure;
- after gateway restart, a normal client retry reloads the routed request
  digest and redelivers with the same operation identity; local idempotency
  prevents duplicate state transitions;
- agent restart reloads its owner-only local grant mapping and reconciles from
  `.cw`/application operation evidence;
- transient network failures reconnect with bounded backoff;
- expired OAuth blocks new client calls but does not rewrite a local operation;
- revoked grants/devices fail new delivery closed;
- a mutation already accepted locally may finish after disconnect; `.cw`
  remains authoritative and its result is inspectable after authorization is
  restored;
- queued cancellation follows the existing conservative policy; running
  state mutations are not unsafely killed.

Structured audit events cover authentication decisions, pairing/revocation,
agent availability, grants, tool and capability decisions, operation outcome,
replay, tenant/scope violations, and project-scope violations. Correlation uses
request, operation, principal, workspace, device, project handle, capability,
actor, and origin. Events exclude source and secrets.

Secure MCP Tunnel and local stdio MCP remain supported development/local paths.
They are not replaced by this public-gateway candidate.

Render staging deployment, rollback, incident, backup/restore, and rotation
procedures live under [operations](operations/staging-deploy.md). Their mere
presence is not acceptance evidence; each procedure remains NOT EXERCISED
until its external result is recorded.
