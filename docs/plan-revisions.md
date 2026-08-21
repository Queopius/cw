# Plan revisions and review supersession

## Governed plan amendment

`cw plan amend` has two mutually exclusive modes. It can replace a complete,
unapproved `PLAN_PROPOSED` document as described below, or it can add existing
declarative artifacts to the current started phase without rerunning the
planner or editing `.cw`.

Inspect both public CAS values first:

```bash
cw plan show --json
cw plan amend \
  --phase 01-example \
  --add-artifact tests/fixtures/example.graphql \
  --expected-workflow-sha256 sha256:<workflow-hash> \
  --expected-state-sha256 sha256:<state-hash> \
  --reason "Declare an existing artifact omitted from the plan" \
  --dry-run --json
```

After reviewing the structured diff, apply the exact request:

```bash
cw plan amend \
  --phase 01-example \
  --add-artifact tests/fixtures/example.graphql \
  --expected-workflow-sha256 sha256:<workflow-hash> \
  --expected-state-sha256 sha256:<state-hash> \
  --reason "Declare an existing artifact omitted from the plan" \
  --apply --yes --non-interactive --json
```

`--add-artifact` is repeatable. Active mode permits no removal or other plan
change. Each path must be an existing, regular, non-linked repository-relative
POSIX path covered by the current phase's `review_paths`. The current phase
must be started, incomplete and ungated, with no live implementer, reviewer,
batch or managed run. A completed or non-current phase cannot be amended.

Apply holds the project mutation lock and rechecks both hashes and artifact
file identities immediately before its journal becomes authoritative. It
creates the canonical metadata backup plus a hashed restore inventory,
immutable old/new plan-revision snapshots, and append-only evidence
supersessions. Current-phase readiness, reviews, validations and a verified
stale session leave the active namespace but remain byte-identical in the
backup. Earlier-phase reviews and gates remain active and byte-identical.

The Completion Contract canonical payload and SHA-256 remain identical. The
result is always workflow `PROPOSED` and state `PLAN_PROPOSED`, with no active
readiness, approval, gate, session, validation or review for the amended phase.
No planner, implementer, reviewer, hook or agent runs. A human must separately
run `cw plan approve` before execution may continue. An exact replay is
idempotent; changed hashes, artifacts, operator or reason are a conflict.

Do not manually edit `.cw`, and do not repeat `cw repair` or `cw start` to
reinterpret evidence against a changed phase contract. If an amendment journal
remains after interruption, preserve it and its backup and rerun the exact
supported operation; `cw doctor` verifies the recovered history.

This public CLI addition is released as Core `0.15.0` under SemVer. It changes
neither Plugin `0.1.0` nor `cw.remote.v1` and remains unavailable through MCP
and Remote.

### Amending an unapproved proposal

Before approval or any implementation starts, a human may replace a planner's
schema-valid but semantically incomplete proposal through the public,
transactional command:

```bash
cw plan show --json
cw plan amend \
  --file corrected-phases.yaml \
  --expected-workflow-sha256 sha256:<current-hash> \
  --json
```

This operation exists only in `PLAN_PROPOSED`. The expected physical SHA-256
is a mandatory compare-and-swap guard. CW accepts either 64 hexadecimal
characters or the canonical `sha256:<64-hex>` form, normalizes both to the
canonical form, and rejects whitespace or other algorithm prefixes. JSON and
single-document YAML use safe parsing; unsafe YAML tags and multiple documents
fail closed. CW validates the proposed workflow and
its dependency graph in memory and requires the canonical Completion Contract
payload and hash to remain identical. It then creates the official `.cw`
backup, records a recovery journal, writes workflow and state atomically, and
reloads their derived consistency. Any ordinary failure restores the old
workflow and state byte-for-byte; a process interruption leaves a journal that
the same command recovers before retrying.

Successful amendment retains `PLAN_PROPOSED`, zero approved phases and the
existing completion cycle. It appends only a `plan_amended` history event with
old/new workflow hashes, input hash, Contract hash and backup path. It does not
run a planner, implementer or reviewer and cannot create a revision, review,
gate, readiness manifest or authorization. `cw plan approve` remains a
separate human decision.

Artifact-only amendment is also distinct from rebaseline. Rebaseline permits a
review-driven phase-contract correction after `REVISE`; active amendment only
adds existing artifact declarations and always returns to `PLAN_PROPOSED`.

CW identifies every current-format approved plan by a `plan_revision_id`
derived from the full SHA-256 of its canonical JSON document. The immutable
snapshot retains the exact phases, criteria, manifest, Completion Contract,
goal, schema/CW version, actor, parent revision, and canonical hashes.

Legacy schema-1 projects remain readable without mutation. Until an authorized
write needs a snapshot, CW deterministically derives the legacy identity from
the current document. Read-only status, explain, history, and doctor never
silently migrate it.

## Ceremony

After an independent `REVISE`, create a proposal:

```bash
cw plan rebaseline \
  --proposal corrected-plan.json \
  --reason "Remove circular Phase 00 criteria" \
  --json
```

The JSON preview reports old/new revision IDs, the immutable proposal hash, and
the exact apply command. It does not change the active plan. After inspecting
that proposal, a human may authorize the exact hash:

```bash
cw plan rebaseline \
  --apply pp-<sha256> \
  --authorize \
  --operation-id operator-change-42 \
  --json
```

Apply requires `REVISION_REQUIRED`, a current-phase `REVISE`, no current gate,
changed criteria/manifest, matching actor, mandatory reason, unexpired grant,
unused nonce, stable proposal/review hashes, and the project operation lock.
Already approved earlier phases must remain byte-equivalent and retain their
gates.

Operation IDs are 1–128 ASCII letters, digits, `.`, `_`, `:`, or `-`. Preview
does not accept authorization/apply options; apply does not accept planning,
proposal-file, or reason options.

## Evidence and attempts

The original review is never edited. A separate `.cw/supersessions/*.json`
record binds its path and SHA-256 to revision A, proposal/revision B, reason,
human authorization, operation, nonce, timestamp, CW version, and resulting
state. Revision B activates as `READY` with no inherited current-phase gate.

Global phase and validation attempts remain monotonic. The first review under
revision B is global attempt 2 when revision A consumed attempt 1; it is
revision attempt 1. Validation records likewise expose both global and
revision-local attempt numbers.
Validation, review, and gate evidence for revision B must share revision hash,
phase, candidate SHA, and artifact hashes.

## Additive schema fields

Current-format plan revisions contain `plan_revision_id`, canonical and source
workflow hashes, parent revision, timestamps, CW/workflow schema versions,
workflow identity/goal/document, Completion Contract hash, actor/origin and an
optional authorization reference. Validation, review and gate evidence add the
revision identity/hash, phase, candidate SHA, attempts, artifact hashes and
writer/schema versions. Legacy records omit these fields and use deterministic
resolution rather than being rewritten.

Supersession records contain their own full-hash identity, old review path/hash,
old/new revision IDs and hashes, proposal ID/hash, reason, actor/origin, exact
authorization evidence, operation ID, timestamp, writer version, resulting
state and normalized result. Unknown kinds, unsafe paths, missing revisions,
hash mismatches, ambiguous operations/nonces and cross-project references fail
closed.

`plan.rebaseline` is classified centrally as a high-consequence authorization
capability. It is available only to trusted local host code and intentionally
excluded from MCP and remote capability profiles.

## Worked `cw-dashboard` case

The regression fixture records review attempt 1 at SHA-256
`cb995baf9e70709e37fd66e53c01f30ac81d2f92963f1381101b6473aa2bf1d4`.
The test uses an isolated repository, preserves its synthetic old review bytes,
activates corrected criteria, proves no gate exists before review attempt 2,
then verifies that only revision B can create the eventual gate. It never opens
or modifies the actual `cw-dashboard` worktree.

## Compatibility and migration

- Reviews without revision IDs bind to the active legacy contract unless a
  validated supersession deterministically binds them to its old snapshot.
- Existing gates remain valid when their phase contract is unchanged.
- Explicit revision metadata is introduced by current-format plan approval or
  an authorized plan/Completion Contract mutation. Legacy validation, review,
  and gate records remain byte-identical and use deterministic resolution.
- Missing or corrupt referenced revisions fail closed; CW never reconstructs
  historical criteria from reviewer prose.

This capability is backward-compatible in project schema 1 but materially adds
governance semantics and CLI surface. Under the current pre-1.0 policy, a new
minor release is the safer recommendation; maintainers must decide the version
and publish it through the normal release branches.
