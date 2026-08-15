# MCP tool contract

CW 0.8 implements the read-only subset of this contract as a local stdio MCP
runtime. The controlled-action rows remain future design only.

## Tool rules

Every tool has one coherent user outcome, a narrow schema, a capability class,
structured output, and stable application errors. There is no
`cw_execute(command)`, `shell(command)`, arbitrary path, or arbitrary validator
command.

| Proposed tool | Capability | Mutation | Notes |
| --- | --- | --- | --- |
| `cw_project_status` | `project.read` | No | Normalized current state |
| `cw_project_inspect` | `project.read` | No | Structured evidence summary |
| `cw_history` | `history.read` | No | Audit timeline |
| `cw_explain` | `project.read` | No | Reasons and safe recovery |
| `cw_completion_status` | `completion.read` | No | Contract, review, proposal |
| `cw_gate_status` | `gate.read` | No | Validated gate chain and consistency |
| `cw_validate` | `validation.run` | Controlled execution | Commands come only from the workflow |
| `cw_request_review` | `review.run` | Evidence/state | Independent supervised reviewer |
| `cw_start_phase` | `phase.start` | Yes | Current authorized phase only |
| `cw_repair` | `project.repair` | Yes | Evidence-derived, backup first |
| `cw_authorize_extension` | `extension.authorize` | Yes | Trusted host confirmation required |

Only the first six read tools are registered in CW 0.8. A request for any
controlled-action tool is rejected by the adapter allowlist before application
dispatch.

Common inputs use an opaque `project_id`. Mutations also use a caller-generated
`operation_id`. `cw_authorize_extension` identifies the exact current proposal,
but no `confirmed=true` argument exists: confirmation must arrive through a
trusted adapter context unavailable to the model.

Common output is the serialized `OperationResult`:

```json
{
  "schema_version": 1,
  "operation_id": "01J...",
  "operation": "workflow.status",
  "capability": "project.read",
  "project_id": "8edc4d0c9e6d3fd4c761",
  "status": "SUCCEEDED",
  "idempotent_replay": false,
  "actor_origin": "mcp_client",
  "data": {}
}
```

## Read-only resources

Implemented resources are:

- `cw://projects`;
- `cw://projects/{project_id}/summary`;
- `cw://projects/{project_id}/current-phase`;
- `cw://projects/{project_id}/gates`;
- `cw://projects/{project_id}/completion-contract`;
- `cw://projects/{project_id}/completion-review/latest`;
- `cw://projects/{project_id}/extension-proposal/current`.

Resources contain normalized evidence, not source files, credentials, raw logs,
environment variables, or private absolute paths. Tools remain appropriate for
fresh computation and all mutations.

## Error contract

Adapters map application errors such as `PROJECT_NOT_INITIALIZED`,
`PROJECT_SCOPE_VIOLATION`, `STATE_INCONSISTENT`, `AUTHORIZATION_REQUIRED`,
`EXTENSION_NOT_PROPOSED`, `OPERATION_CONFLICT`, and
`INFRASTRUCTURE_FAILURE`. Diagnostic detail is separately permissioned.
