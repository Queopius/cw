# Plan rebaseline recovery

This page covers two distinct recovery mechanisms. `cw plan rebaseline
recover` restores a proven state-machine transition after `repair --reopen`;
the transaction recovery described later rolls back an interrupted rebaseline
apply. Neither mechanism authorizes or applies a plan revision.

Every new reopen that invalidates a valid review writes an append-only
`.cw/repair-receipts/` record bound to the pre-reopen backup and review digest.
The explicit recovery apply writes a separate
`.cw/rebaseline-recoveries/` receipt and uses
`.cw/runtime/rebaseline-recovery-transaction.json`. Recovery requires an
independently SHA-bound reopen receipt in protected, append-only history. A
legacy project without that binding remains usable, but recovery fails closed:
a backup copy alone cannot prove phase, workflow, active revision, decision and
the `repair --reopen` transition. Missing or ambiguous provenance is not inferred.

## Public recovery ceremony

Use the exact review reference and hashes reported by the audited project. The
preview performs the complete preflight and writes nothing:

```bash
cw plan rebaseline recover \
  --phase 02-active \
  --review-ref .cw/reviews/02-active-attempt-01.json \
  --expected-review-sha256 sha256:<review-hash> \
  --expected-workflow-sha256 sha256:<workflow-hash> \
  --expected-state-sha256 sha256:<state-hash> \
  --expected-prior-gate-ref .cw/gates/01-previous.approved.json \
  --expected-prior-gate-sha256 sha256:<gate-hash> \
  --reason "Restore the proven REVISE transition" \
  --dry-run --llm
```

Apply is a separate explicit command with the same immutable inputs and
`--apply`. It changes only state plus append-only recovery evidence. The result
is `REVISION_REQUIRED`, with `last_review` restored to the selected REVISE
record and `last_gate` derived from the fully validated contiguous gate chain.
Both attempt counters remain `0`. No phase is started or approved.

The output contract defines `changed` as persistent mutation performed by the
current invocation. Preview reports `changed=false`; the first apply reports
`changed=true`; and an exact replay reports `changed=false` with
`idempotent_replay=true`, the same recovery ID, and no new writes. Human output
explicitly states that recovery is already applied and no project changes were
made. Compact LLM output retains the replay flag, `mutation: "none"`, phase,
review and CAS digests, resulting state, receipt/backup and next action.

Machine recovery output uses the shared JSON/JSONL field allowlist: `changed`,
`idempotent_replay`, `recovery_id`, `phase`, review and CAS fields,
`previous_status`, `resulting_status`, evidence references, and `next_action`.
Unknown or unavailable fields fail closed; envelope and integrity invariants
remain present.

Recovery does not create a proposal. After recovery, prepare the contract
change separately:

```bash
cw plan rebaseline --proposal corrected-plan.json \
  --reason "Expand only the active phase contract" --json
```

That proposal receives its own ID and canonical hash. Applying it remains a
different governed operation requiring independent human authorization.

The prior gate reference and digest are part of the externally authorized
request. When no prior gate exists, pass `--no-prior-gate`; omission is not
interpreted as authorization. Persisted requests, receipts, backups, history
and journals are local evidence only and cannot authorize replay when replaced
together.

## Fail-closed boundaries

CW rejects recovery when the selected evidence is not the unique, intact,
active-plan REVISE review for the phase and workflow; when either CAS changed;
when attempts occurred after reopen; or when a current gate, readiness file,
session, run, lock or transaction exists. Traversal, symlinks, hardlinks,
special files, malformed namespaces, altered gates and ambiguous review
identities are errors, never no-ops.

An existing artifact in a later rebaseline proposal remains subject to normal
evidence validation. A future artifact may be absent only as a canonical POSIX
plan declaration whose existing parent chain is safe and whose path is covered
by `review_paths`. Its absence never counts as materialized evidence.

Recovery persists and fsyncs a `PREPARED` journal before creating any recovery
backup. It then fsyncs the complete backup, records its digest in a
`BACKUP_READY` journal, and only then permits state/receipt mutation. Failure
injection and process-crash recovery restore the original state and remove
incomplete transaction backups and receipt namespaces.

### Trust model and deterministic replay

The exact operator request and its review, workflow, state CAS, prior-gate
reference/digest (or explicit absence), reason and schema version are the root
of trust. The recovery ID and receipt/backup paths are derived only from that
request. Receipts, history, live state, backups and journals are evidence to
verify; they are never independent authorities and never contribute stored
derived values to replay. Without an external request, receipt validation is
structural only and cannot claim authorized replay.

For an exact replay CW loads the deterministic backup, verifies that its state
matches the original state CAS, revalidates the workflow, selected `REVISE`
review and canonical prior gates, and reruns the transition as a pure
projection. It then compares the reconstructed receipt, history prefix, and
live state with their persisted forms. Missing original CAS values, a different
operator payload, or any mismatch fails closed. A different payload is an
operation conflict, not an idempotent replay or a `noop`.

This local model does not claim integrity against an attacker able to replace
the CW binary, the executed code, and the human authorization simultaneously.
That system-level compromise is outside CW's repository-local threat model.

The same backup-first rule applies to active artifact-only `cw plan amend`.
Its separate `.cw/runtime/plan-amend-transaction.json` binds both pre-change
hashes, the exact proposal, created append-only records and the closed list of
evidence paths recoverable from the backup. Recovery restores workflow, state,
readiness, reviews, validations and a stale session byte-for-byte and removes
only files created by the incomplete transaction. Do not edit or delete the
journal manually.

Rebaseline writes a verified backup before activation and records a transaction
journal at `.cw/runtime/plan-rebaseline-transaction.json`. Individual evidence
files are append-only; active workflow/state writes are atomic.

The journal is a protected Core path with a closed, integrity-hashed schema. It
binds the operation, proposal, old/new revision and supersession IDs, backup,
old workflow/state, and the only three append-only paths recovery may remove.
Unknown fields, altered hashes, unsafe targets, missing backups, symlinks, or
cross-project workflow identity fail with `TRANSACTION_RECOVERY_REQUIRED`
before any restore or deletion.

If apply fails in-process, CW restores the old workflow/state, removes only
files created by the uncommitted transaction, preserves the backup and original
review, and returns the stage-specific error. An exact replay of a committed
operation is idempotent; a reused operation ID or nonce with different intent
fails.

After a process crash:

1. Stop other CW operations and preserve the repository.
2. Inspect `cw status`, `cw explain`, the transaction journal and referenced backup.
3. Do not edit state, reviews, gates, revisions, supersessions or the journal.
4. Retry the same authorized rebaseline operation. Its mutation preflight runs
   deterministic recovery before evaluating the proposal.
5. Run `cw doctor`, `cw history`, `cw status` and gate validation.

If the journal or backup is corrupt, stop. `TRANSACTION_RECOVERY_REQUIRED` means
Core cannot prove rollback or commit and requires a Core maintainer. Restoring a
backup manually is an incident operation, not a review approval. No later phase
may start without the normal valid gate.
