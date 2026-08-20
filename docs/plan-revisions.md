# Plan revisions and review supersession

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
