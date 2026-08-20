# Error code reference

CW stores a stable structured code with each classified failure. Normal output
shows a concise explanation; `cw error` shows structured detail and `cw error
--raw` shows the complete redacted diagnostic.

Retryability is decided from the recorded operation and evidence, not from a
substring in stderr. “Context” below means CW will offer `cw retry` only when the
stored diagnostic proves that retry is safe.

## Workflow state and integrity errors

| Code | Meaning | Retryable? | Normal recovery |
| --- | --- | --- | --- |
| `WORKFLOW_ERROR` | General classified workflow failure | Context | Read `cw error` and follow its explicit action |
| `WORKFLOW_PROJECT_MISMATCH` | Project identity and workflow metadata disagree | No | `cw explain`, then backup-first `cw repair` |
| `RUNTIME_NOT_WRITABLE` | CW cannot safely write required local metadata | No | Correct ownership/permissions, then diagnose again |
| `INVALID_STATE` | Persisted state cannot support the requested operation | No | `cw explain`; use `cw repair` only when directed |
| `STATE_INCONSISTENT` | Cached state contradicts validated workflow evidence | No | `cw repair`, then `cw status` |
| `INVALID_GATE` | Approval evidence, dependencies, review, or artifact hashes fail validation | No | Inspect evidence; explicitly reopen only when necessary |
| `PROTECTED_PATH_MODIFIED` | An implementation session changed protected workflow inputs/evidence | No | Inspect `cw error`; reconcile trusted metadata or reopen deliberately |
| `HOOK_UNTRUSTED` | Codex has not trusted the repository Stop hook | No | Inspect and trust the hook through Codex |
| `SCHEMA_VALIDATION_ERROR` | A CW document does not match its schema/structural contract | No | Repair supported legacy data or restore valid metadata |
| `SCHEMA_VERSION_ERROR` | Data uses an unsupported older/newer schema | No | Upgrade CW or run supported migration; never downgrade future data |
| `LOCKED` | Another mutating operation/session owns the project | Context | Wait; inspect an interrupted run before repair |
| `INTERNAL_ERROR` | An unexpected CW defect escaped normal classification | No | Preserve diagnostics and report the reproducible failure |

## Planning errors

| Code | Meaning | Retryable? | Normal recovery |
| --- | --- | --- | --- |
| `PLAN_REQUIRED` | No approved plan exists for execution | No | Run `cw plan --goal ...` |
| `PLAN_UNCLEAR` | Repository evidence/goal is insufficient | No | Supply a precise `--goal` or improve local documentation |
| `PLANNER_NETWORK_ERROR` | Planner network request failed | Yes, when recorded | `cw retry` |
| `PLANNER_TRANSPORT_ERROR` | Planner transport failed before a valid result | Yes, when recorded | `cw retry` |
| `PLANNER_PROCESS_ERROR` | Planner process exited without a valid result | Context | Inspect `cw error`; retry if offered |
| `PLANNER_SCHEMA_ERROR` | Planner returned invalid structured plan data | Context | Inspect diagnostics; retry after environment/build correction |
| `PLAN_TIMEOUT` | Planner exceeded its bounded timeout | Yes, when recorded | `cw retry` or adjust the supported timeout policy |

## Implementation and validation errors

| Code | Meaning | Retryable? | Normal recovery |
| --- | --- | --- | --- |
| `CODEX_NOT_FOUND` | The Codex executable is unavailable | No | Install/fix `PATH`, then run `cw doctor` |
| `CODEX_CONFIG_ERROR` | Codex rejected effective configuration before normal operation | No | Inspect `cw doctor --codex --verbose`; correct the actual config/build source |
| `IMPLEMENTER_PROCESS_ERROR` | Implementer exited without a terminal review or valid readiness | Yes, when recorded | `cw retry`; valid readiness proceeds directly to review |
| `EXECUTION_INTERRUPTED` | User requested a safe foreground stop | No automatic retry | Inspect status, then intentionally continue |
| `NOTHING_TO_VALIDATE` | Current phase has no valid readiness to validate | No | Run/finish implementation or follow current status |

## Review errors

| Code | Meaning | Retryable? | Normal recovery |
| --- | --- | --- | --- |
| `REVIEW_TIMEOUT` | Independent review exceeded its timeout | Yes, when recorded | `cw retry` without rerunning valid implementation |
| `REVIEWER_NETWORK_ERROR` | Reviewer network request failed | Yes, when recorded | `cw retry` |
| `REVIEWER_PROCESS_ERROR` | Reviewer exited or returned invalid structured output | Context | Inspect diagnostics; retry if CW preserved valid readiness |

Semantic `REVISE` is not an infrastructure error code. It is a valid reviewer
decision and consumes one semantic revision attempt.

## Integration errors

| Code | Meaning | Retryable? | Normal recovery |
| --- | --- | --- | --- |
| `MCP_OPTIONAL_UNAVAILABLE` | Optional integration is unavailable | Not blocking | Continue unrelated work; inspect with `cw integrations` |
| `MCP_REQUIRED_UNAVAILABLE` | Required integration failed preflight | No | Restore provider availability before starting the phase |
| `MCP_AUTH_REQUIRED` | Required integration needs authentication | No | Authenticate through Codex/provider tooling |
| `MCP_SERVER_ERROR` | Integration provider returned a server failure | Context | Retry provider check later; required phases remain stopped |
| `MCP_TRANSPORT_ERROR` | Integration transport could not operate | Context | Diagnose provider/transport without rewriting Codex config |
| `MCP_DISABLED` | A required integration is disabled | No | Enable it through its owning Codex/provider configuration |
| `MCP_NOT_CONFIGURED` | Requested/required integration is absent | No | Configure it through Codex/provider tooling |

Optional integration diagnostics never override an exit-zero Codex operation
with the expected structured result.

## Batch execution errors

| Code | Meaning | Retryable? | Normal recovery |
| --- | --- | --- | --- |
| `BATCH_TOO_LARGE` | Request exceeds the effective hard phase cap | No | Request a smaller batch or change explicit policy |
| `BATCH_TIME_EXHAUSTED` | Wall-clock budget reached a safe stop boundary | New/resume action | Inspect progress; resume only with remaining original budget |
| `BATCH_REVISION_EXHAUSTED` | Current phase consumed its semantic revision budget | No automatic retry | Review the phase and start a deliberate new action |
| `BATCH_INTERRUPTED` | Batch received an explicit safe stop | New/resume action | Inspect preserved session metadata before resuming |

## Update errors

| Code | Meaning | Retryable? | Normal recovery |
| --- | --- | --- | --- |
| `UPDATE_CHECK_ERROR` | Release metadata could not be checked | Yes later | Continue normal work; run `cw update --check` later |
| `UPDATE_DOWNLOAD_ERROR` | Selected artifact download failed | Yes later | Retry when connectivity/provider is healthy |
| `UPDATE_CHECKSUM_ERROR` | Artifact SHA-256 did not match trusted metadata | No | Do not install; inspect release source |
| `UPDATE_SIGNATURE_ERROR` | Signature evidence failed validation | No | Do not install; inspect trusted release metadata |
| `UPDATE_MANIFEST_ERROR` | Release manifest is invalid/incompatible | No | Use a valid published release |
| `UPDATE_INSTALL_ERROR` | Staged installation failed | Context | Active version remains selected; inspect diagnostics |
| `UPDATE_SMOKE_TEST_ERROR` | Staged CW failed its pre-switch smoke test | No | Keep active version; investigate the staged build |
| `UPDATE_ROLLBACK_ERROR` | Prior retained version could not be restored | Context | Inspect managed runtime and update state |
| `UPDATE_INCOMPATIBLE` | Release cannot support the current installation/project schema | No | Choose a compatible release |
| `UPDATE_DEVELOPMENT_INSTALL` | Self-update was requested from a source/editable install | No | Use source tooling or a managed installation |

## CLI usage errors

| Code | Meaning | Retryable? | Normal recovery |
| --- | --- | --- | --- |
| `USAGE_ERROR` | Arguments, configuration values, or command combinations are invalid | No | Correct the command/configuration; exit code is `2` |

## Application and adapter boundary errors

| Code | Meaning | Retryable? | Normal recovery |
| --- | --- | --- | --- |
| `AUTHORIZATION_REQUIRED` | A high-consequence mutation lacks matching, current, explicit human authorization | New confirmation | Ask the operator to confirm the exact current proposal and action |
| `OPERATION_CONFLICT` | An operation identifier was reused for a different request or another adapter holds the project lock | Context | Reuse the original request exactly, or wait and issue a new operation ID |
| `PROJECT_SCOPE_VIOLATION` | A requested path or project handle is outside the adapter's authorized roots | No | Select an explicitly authorized CW repository |
| `PLAN_REBASELINE_REQUIRED` | Reviewed workflow correction requires the explicit rebaseline ceremony | Human boundary | Create and inspect an exact proposal with a reason, then authorize it |
| `PLAN_REVISION_INVALID` | Active or historical plan revision identity/hash/contract is invalid | No | Stop; inspect revision evidence and recover from the verified backup |
| `SUPERSESSION_INVALID` | Review supersession, its authorization, or its historical links are invalid | No | Stop; preserve evidence and investigate tampering or incomplete migration |
| `TRANSACTION_RECOVERY_REQUIRED` | A rebaseline transaction journal cannot be recovered deterministically | Operator | Do not edit state; restore the recorded backup or repair Core first |
| `PROJECT_COMPLETED` | A controlled action targeted a semantically completed project | No | Inspect completion evidence; do not reopen implicitly |
| `PHASE_NOT_STARTABLE` | Current state/readiness/session does not permit phase start | Context | Inspect status and finish or reconcile the current operation |
| `OPERATION_IN_PROGRESS` | A conflicting/running operation cannot be replaced or cancelled safely | Later | Poll the active operation; do not assume rollback |
| `OPERATION_NOT_FOUND` | The project has no lifecycle record for that operation ID | No | Use the project and operation ID returned by submission |
| `OPERATION_CANCELLED` | A queued operation was cancelled before execution | New action if desired | Submit a fresh explicitly intended action |
| `RETRY_NOT_ALLOWED` | Current evidence does not prove a controlled retry is safe | No | Inspect the recorded error; do not rewind history |
| `COMPLETION_EXTENSION_PENDING` | Planned scope ended and contract review/extension authorization controls continuation | Human boundary | Inspect completion/proposal evidence; authorize only outside MCP |

## Safe diagnostic sequence

```bash
cw status
cw error
cw doctor
cw explain
```

Use `cw retry` only when the diagnostic marks the operation retryable. Use
`cw repair` for evidence-backed metadata reconciliation, and reserve
`cw repair --reopen PHASE` for an intentional invalidation of that phase and its
dependents.
