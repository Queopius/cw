# Reviewer infrastructure isolation

This page is for operators diagnosing deterministic verification, Semantic
Reviewer failures, or an old review that was incorrectly persisted as
`REVISE`. Start with `cw explain --output=json`; the safe next action is always
explicit.

## Two independent responsibilities

The Verification Executor runs only workflow-authorized deterministic commands.
It uses canonical argv without a shell, closed stdin, timeouts, the canonical
repository root, and a fresh owner-only runtime. Temp and cache variables point
inside that runtime. Preflight proves type, ownership, containment, absence of
symlink/hardlink hazards, write, fsync, rename, and delete before execution.

Successful execution creates an append-only `cw.verification-receipt.v1`
receipt. It binds workflow, workflow digest, prior state digest, plan revision,
phase, semantic-attempt position, artifacts, review paths, Completion Contract,
ordered argv, duration, exit status, redacted stream digests, and runtime
preflight. Its canonical digest and file digest are both checked. The receipt
does not replace artifact validation and cannot be reused across identities.

The Semantic Reviewer never runs required commands, test tools, package
managers, or installers. It receives a validated receipt and reviews acceptance
semantics, scope, Completion Contract, artifact/evidence coherence, integrity,
and risk. Repository and artifact instructions are untrusted prompt-injection
inputs. Sandbox is read-only; hooks and web are disabled. A detected command
event discards the whole reviewer result as infrastructure.

## Attempts, retry, and explain

A semantic `REVISE` consumes the normal semantic and revision attempt. A
verification or reviewer infrastructure failure consumes neither, creates no
gate, starts no implementation, and preserves compatible readiness. `cw retry`
revalidates under the operation lock. It reuses compatible receipt-bound
readiness or regenerates verification evidence, then invokes only the reviewer.
Retries are explicit and audited; CW does not loop silently.

`cw explain` reports classification, operation, phase, retryability, readiness,
preserved attempts, redacted reason, correlation ID through `cw.output.v1`, and
the next public command. It never offers retry for a legitimate semantic
`REVISE`.

## Historical recovery

Recovery repairs a past accounting/evidence error; retry performs a future
operation. They are intentionally separate. Preview requires the live phase,
active review, review digest, workflow/state CAS, reason, and proof from both a
validated deterministic receipt and the last reviewer run log. Ambiguous prose
or timestamps alone are insufficient.

```bash
cw review recover-infrastructure \
  --phase 02-active \
  --review-ref .cw/reviews/02-active-attempt-01.json \
  --expected-review-sha256 sha256:<review-hash> \
  --expected-workflow-sha256 sha256:<workflow-hash> \
  --expected-state-sha256 sha256:<state-hash> \
  --reason "Reviewer cache failure was misclassified" \
  --dry-run --output=json
```

The angle-bracket values are placeholders and must be replaced with the exact
live digests. Apply the identical request with `--apply` instead of `--dry-run`.
The operation is locked, backup-first, journaled, rollback-safe, append-only,
and idempotent. It preserves the original review, restores exactly one wrongly
consumed attempt, writes a supersession and receipt, and records a retryable
infrastructure error. It does not fabricate readiness, execute commands, invoke
an agent, approve, create a gate, alter workflow/Completion Contract, or start a
phase. Run `cw retry --json` separately afterward.

Recovery fails closed for stale CAS, altered/duplicate/superseded/cross-identity
reviews or receipts, missing infrastructure proof, active runs/locks, pending
journals, an approved/gated phase, unsafe paths, or conflicting replay.

## Doctor

`cw doctor --reviewer` reports separate dimensions for reviewer connectivity,
structured output, read-only sandbox, hooks/web isolation, prohibited command
events, Verification Executor runtime, temp/cache preflight, redaction, and
cleanup. It does not execute workflow commands.
