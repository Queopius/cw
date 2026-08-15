# MCP tool contract

CW implements the stdio read and controlled-action subset below. CW 0.11 may
advertise only the six primary reads on a restricted ChatGPT development
surface, but it never adds another tool. All
tools have narrow schemas, opaque project scope, structured application results,
and stable errors. No tool dispatches through the CLI.

| Tool | Capability | Class | Input beyond project/operation IDs |
| --- | --- | --- | --- |
| `cw_project_status` | `project.read` | READ | None |
| `cw_project_inspect` | `project.read` | READ | None |
| `cw_history` | `history.read` | READ | None |
| `cw_explain` | `project.read` | READ | None |
| `cw_completion_status` | `completion.read` | READ | None |
| `cw_gate_status` | `gate.read` | READ | None |
| `cw_phase_start` | `phase.start` | CONTROLLED_STATE_MUTATION | None; engine chooses current phase |
| `cw_validate` | `validation.run` | EXECUTION | None; workflow supplies commands |
| `cw_request_review` | `review.run` | EXECUTION | None; supervisor supplies reviewer contract |
| `cw_retry` | `retry.run` | CONTROLLED_STATE_MUTATION | None; engine classifies retry target |
| `cw_operation_status` | `operation.read` | READ | `target_operation_id` |
| `cw_operation_cancel` | `operation.cancel` | CONTROLLED_STATE_MUTATION | `target_operation_id` |

There is no `cw_execute(command)`, shell, arbitrary path/filesystem/Git tool,
gate tool, repair/rebaseline tool, or extension-authorization tool.

## Result and lifecycle

Action submission and polling serialize the same `OperationResult` model:

```json
{
  "schema_version": 1,
  "operation_id": "client-request-42",
  "operation": "review.request",
  "capability": "review.run",
  "project_id": "8edc4d0c9e6d3fd4c761",
  "status": "RUNNING",
  "idempotent_replay": false,
  "actor_origin": "mcp_client",
  "data": {
    "stage": "reviewer_execution",
    "phase": "04-integration",
    "elapsed_seconds": 12.4,
    "result": null,
    "error": null
  }
}
```

Lifecycle values are `QUEUED`, `RUNNING`, `SUCCEEDED`, `FAILED`, `BLOCKED`, and
`CANCELLED`. An operation ID is bound to its project and request digest. Exact
replay is idempotent; conflicting reuse fails safely.

## Errors

Stable codes include `PROJECT_SCOPE_VIOLATION`, `PROJECT_COMPLETED`,
`PHASE_NOT_STARTABLE`, `STATE_INCONSISTENT`, `OPERATION_IN_PROGRESS`,
`OPERATION_CONFLICT`, `OPERATION_NOT_FOUND`, `OPERATION_CANCELLED`,
`RETRY_NOT_ALLOWED`, `COMPLETION_EXTENSION_PENDING`, `AUTHORIZATION_REQUIRED`,
`PLATFORM_CAPABILITY_UNAVAILABLE`, and `INFRASTRUCTURE_FAILURE`. The platform
code means CW supports a known tool that the configured client-surface profile
does not advertise; it is not a product/workflow failure. Normal responses do
not contain tracebacks.

## High-consequence boundary

`project.repair`, `extension.authorize`, rebaseline, destructive recovery, and
release/deployment capabilities are not registered. Conversation text,
repository content, planner output, reviewer output, or a forged actor cannot
expand the registry. Completion extensions still require the existing explicit
human authorization path outside MCP.
