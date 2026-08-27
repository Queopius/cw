# Troubleshooting CW by symptom

## Reviewer or verification infrastructure failure

Run `cw explain --output=json`. When `retryable` is true and `recovery` is `cw
retry`, semantic counters are preserved. `cw retry` reuses receipt-bound
readiness or regenerates it through the Verification Executor; it does not run
the implementer for a review retry. A legitimate semantic `REVISE` is not
retryable. Use historical recovery only when CW can prove the original review
was exclusively infrastructure-derived.

## Plugin marketplace or installation fails

Verify the installed surface before retrying:

```text
codex plugin --help
codex plugin marketplace list
codex plugin list --available
```

The supported install command is `codex plugin add`; `plugin install`,
`plugin enable`, and `plugin disable` are not available in Codex CLI `0.148.0`.
For a repository source, confirm that `.agents/plugins/marketplace.json` and
`plugins/cw` are both present and that the marketplace path is exactly
`./plugins/cw`. For Git, use a full immutable SHA and include both sparse paths.

If a candidate ZIP fails validation, do not extract it with a generic unzip
command and do not reuse a partial destination. Verify the expected SHA-256 and
run `scripts/prepare_plugin_marketplace.py` into a new empty evaluation
directory. See [Plugin packaging and installation](plugin-installation.md).

Removing `cw@cw-development` and removing `cw-development` are separate
operations. Neither operation should delete a repository, `.cw`, or evidence.

## Independent approval cannot be satisfied

Run `cw governance diagnose --pr NUMBER`. If the author is the only authorized
reviewer, configure `solo-maintainer` and inspect `cw governance remote-plan
--pr NUMBER`. Do not self-approve or enable direct pushes. Neither command
changes repository settings.

For teams, missing, pending, changes-requested, invalid, and stale approvals are
reported separately; a new SHA requires a new approval.

## Governance authorization evidence is incomplete

**Symptom:** `Incomplete governance authorization evidence` or a legacy
authorization without `base_sha` blocks promotion.

Preserve the original and invalidate it through CW:

```bash
cw governance invalidate --pr NUMBER --head-sha SHA \
  --reason incomplete-base-sha-evidence
cw governance authorize --pr NUMBER
```

Invalidation and authorization are deliberately separate confirmations. Never
edit, delete, overwrite, or reconstruct `base_sha` from the current PR because
the current base may differ from the historical authorization state.

Start with local, read-only evidence:

```bash
cw status
cw doctor
cw error
```

`cw error` reads the independent diagnostic store even when workflow state is
corrupt. Add `--raw` only when you need the complete redacted diagnostic and
internal traceback.

## Installation or `cw` launcher fails on one platform

**Symptom:** `cw` is not found after installation, a managed runtime cannot be
selected, or the launcher works only from the source checkout.

**Safe diagnosis:**

```bash
cw version --verbose
cw doctor
```

On Linux/macOS, inspect the installer-reported user bin directory and the
versioned runtime under `~/.local/share/cw`. On native Windows, inspect the user
`PATH`, `%LOCALAPPDATA%\Queopius\CW\bin\cw.cmd`, the regular `current` version
marker, and `versions\`.

Re-run the appropriate installer from the intended source commit. Both
installers stage and smoke-test before activation; failed activation must leave
the prior runtime usable.

**Do not:** run the PowerShell installer as Administrator to work around a user
`PATH` problem, replace the Windows marker with a Developer Mode symlink, or
test only `python -m cw` and call the installed command healthy.

## Workflow state invalid

**Symptom:** `STATE_INCONSISTENT`, a current phase that appears behind valid
gates, or a completed workflow that still has an active phase.

**Likely cause:** cached `.cw/state.json`, readiness, `last_gate`, or history no
longer agrees with the validated contiguous approval chain.

**Safe diagnosis and recovery:**

```bash
cw explain
cw doctor
cw repair
cw status
```

CW validates phase order, dependencies, every relevant gate, linked reviews,
and artifacts before repair. It creates a backup, reconciles operational state,
archives stale readiness/errors, and preserves valid gates. If all configured
gates validate, repair sets `COMPLETED` with no current phase.

**Do not:** delete gate files, edit `state.json`, or reopen an approved phase to
make counters line up. Normal `cw repair` fabricates no approval and changes no
application file.

## Protected workflow metadata changed

**Symptom:** `PROTECTED_PATH_MODIFIED` after an implementation session.

**Likely cause:** the implementer changed the phase contract or CW-owned
evidence, or trusted CW metadata was changed without a coherent baseline update.

**Safe diagnosis:**

```bash
cw error
cw explain
cw doctor --verbose
```

The phase contract includes phase definition, acceptance criteria, dependencies,
policy, required commands, and human-gate requirements. CW-managed operational
metadata is updated only through supervisor transactions.

**Do not:** remove protected paths from policy or copy metadata from another
repository. Run normal `cw repair` only when its explanation identifies a
reconcilable CW-managed metadata mismatch.

## Starting Codex appears stuck

**Symptom:** no visible progress after session startup.

**Safe diagnosis:**

```bash
cw inspect session
cw doctor --processes
cw doctor --performance
```

CW transitions away from startup when the child exists and the first structured
event arrives. A quiet live process is not a dead process; an active child
command is reported separately. Heartbeats and inactivity warnings are
non-destructive.

**Do not:** launch a second `cw` process in the same project. The project lock and
run identity prevent duplicate implementers.

## Codex process stopped unexpectedly

**Symptom:** `IMPLEMENTER_PROCESS_ERROR` or an interrupted run.

```bash
cw error
cw inspect session
cw logs
cw retry
```

CW preserves the current phase. If readiness is valid, retry proceeds to review;
otherwise it starts a new implementer session without consuming a semantic
review attempt.

**Do not:** hand-create `READY_FOR_REVIEW.json` or reuse a manifest from another
session.

## Codex configuration is invalid

**Symptom:** `CODEX_CONFIG_ERROR`, often before session initialization.

```bash
cw error
cw doctor --codex --verbose
cw version --verbose
```

The diagnostic shows sanitized argv and build identity. CW does not create MCP
`transport` keys, inject partial `mcp_servers.*` overrides, or edit global Codex
configuration. This deterministic error is not presented as blindly retryable.

## Reviewer unavailable

**Symptom:** `REVIEW_TIMEOUT`, `REVIEWER_NETWORK_ERROR`, or
`REVIEWER_PROCESS_ERROR` after implementation is ready.

```bash
cw error
cw retry
```

CW preserves session-bound readiness and reruns only the reviewer. Infrastructure
failure does not increment the semantic attempt. Legacy reviewer records may
require backup-first `cw repair` before retry.

**Do not:** rerun implementation merely to clear a reviewer transport failure.

## Optional MCP integration failed

**Symptom:** authentication, HTTP, startup, or transport diagnostics for an
integration not required by the current phase.

```bash
cw integrations check
cw integrations info vercel
```

Optional diagnostics have impact `NONE` when Codex exits successfully with the
expected result. Repeated noise is deduplicated and raw HTML is not shown.

**Do not:** add `mcp_servers.<name>.enabled=false`, invent a transport, or edit
global Codex configuration solely for CW.

## Required integration unavailable

**Symptom:** `MCP_REQUIRED_UNAVAILABLE`, `MCP_AUTH_REQUIRED`, `MCP_DISABLED`, or
`MCP_NOT_CONFIGURED` before agent launch.

```bash
cw integrations
cw integrations info <name>
cw doctor --integrations
```

Authenticate or configure the integration through Codex/provider tooling. CW
preflights required capabilities and fails closed before implementation.

## Plan cannot be created

**Symptom:** `PLAN_REQUIRED`, `PLAN_UNCLEAR`, planner timeout, transport,
network, process, or schema failure.

```bash
cw plan --goal "Describe the intended change precisely"
cw error
cw retry
```

CW preserves a pending goal after retryable planner infrastructure failure and
writes no partial plan. Improve repository documentation or provide `--goal` for
an unclear objective.

## Workflow already complete

**Symptom:** `cw`, `cw retry`, or `cw run` reports no pending phase.

This is a safety boundary, not an error:

```bash
cw status
cw history
```

When all configured gates validate, state is `COMPLETED`, current phase is none,
and no implementer launches.

**Do not:** reopen the first phase or remove the final gate merely to make `cw`
run again. Create and approve a new plan through the supported planning lifecycle
when new work is intentionally defined.

## Approval gate invalidated

**Symptom:** `INVALID_GATE` after an approved artifact or linked review changed.

Inspect the evidence first:

```bash
cw error
cw history --phase <phase>
```

Only when that phase must intentionally be implemented and reviewed again:

```bash
cw repair --reopen <phase>
```

!!! warning
    Reopen backs up metadata and invalidates dependent gates. It is not a generic
    repair flag and must never be used blindly.

## Project identity or schema mismatch

- `WORKFLOW_PROJECT_MISMATCH`: `cw repair` may rebind a renamed copy only when
  repository evidence proves identity; foreign metadata is quarantined.
- known older schema: repair migrates backup-first and atomically.
- newer unsupported schema: upgrade CW; repair does not downgrade future data.
- managed path is a symlink: replace it with a real repository-local path; CW
  will not follow or repair through it.

## Hook trust or readiness mismatch

- `HOOK_UNTRUSTED`: inspect and trust the repository hook through Codex's hook UI.
- readiness/session mismatch: run `cw repair` to back up and remove the corrupt
  runtime pair; never copy readiness between sessions or repositories.
- stale session without readiness: `cw repair` archives the orphan lease.
- stale session with valid readiness: use `cw review` so completed work is not
  restarted.

## Update failure

- `UPDATE_CHECK_ERROR`: normal project work can continue; retry the check later.
- checksum, manifest, install, or smoke-test error: the active version was not
  switched. Inspect `cw error`; project data was not touched.
- rollback error: inspect the managed runtime and retained version through
  `cw version --verbose` before another update action.

Detailed redacted diagnostics live under `.cw/logs/`. They may still contain
private project information and should be handled as sensitive local metadata.
See the [Error reference](errors.md) for code-by-code classification.

For OS-specific evidence and manual Windows VM execution, see [Platform
support](testing/platform-support.md) and [Windows VM
acceptance](testing/windows-vm-acceptance.md).
