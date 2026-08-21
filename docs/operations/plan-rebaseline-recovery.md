# Plan rebaseline recovery

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
