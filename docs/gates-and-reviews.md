# Understanding gates and independent review

CW deliberately gives implementation, review, and state transition to different
actors:

```text
                         CW SUPERVISOR
                    policy · state · evidence
                         /           \
                        /             \
             IMPLEMENTER             REVIEWER
             workspace-write          read-only
             current phase only       separate process
                        \             /
                         \           /
                  validation → verified gate
```

The implementer and reviewer are sibling Codex processes supervised by CW. The
reviewer is not a continuation of the implementation session and cannot modify
the workspace to make its own findings pass.

## What a gate means

A gate is durable evidence that one configured phase passed its complete trust
boundary. It is not a status label and is never inferred from a successful
process exit.

Approval writes `.cw/gates/<phase>.approved.json` with workflow/version, review
reference, timestamp, optional Git commit, CW version, and artifact hashes. Gate
validation requires:

- the configured phase identity and dependencies;
- the referenced semantic review;
- an exact and complete acceptance-criterion set;
- a consistent approval decision and no blocking issues;
- the complete declared artifact set and current SHA-256 values;
- explicit human approval when the phase requires it.
- matching plan revision, canonical workflow hash, candidate SHA, and embedded
  validation context for current-format revision-bound evidence.

Changed artifacts, review evidence, or dependencies invalidate the gate. CW
never recreates it silently.

> **No valid gate. No next phase.**

## Validation before review

The order is fixed:

1. readiness structure and state;
2. dependency gates;
3. artifact declaration and existence;
4. repository containment and symlink safety;
5. approved workflow commands in a private Verification Executor runtime;
6. dependency gate revalidation;
7. final SHA-256 artifact capture and append-only Verification Receipt;
8. receipt integrity validation;
9. bounded Semantic Review Evidence Bundle construction;
10. independent semantic review.

Commands run before authoritative hashes are captured. CW then revalidates
dependencies, preventing a test or formatter from silently changing current
artifacts or previously approved evidence.

The Verification Executor uses canonical argv with `shell=False`, closed stdin,
bounded timeouts and the canonical repository root. Each execution receives a
new owner-only temp/cache root via `TMPDIR`, `TMP`, `TEMP`, `XDG_CACHE_HOME`, and
`COMPOSER_CACHE_DIR`. A write/fsync/rename/delete preflight must pass before a
command starts. Only redacted stream digests—not secrets, host paths, or full
output—enter the receipt.

## Independent reviewer contract

The reviewer uses a separate ephemeral `codex exec` process with:

| Property | Reviewer behavior |
| --- | --- |
| Sandbox | `read-only` |
| Approval policy | `never` |
| Hooks | disabled |
| Shell tool | disabled when the provider supports per-session tool control |
| Output | validated structured schema |
| Scope | declared current-phase artifacts included in the evidence bundle |
| Commands | prohibited; any observed command event discards the result |
| Deterministic evidence | structured results from the validated Verification Receipt |

CW reads each declared artifact before reviewer launch using project-root
containment, traversal and symlink rejection, regular-file checks, per-file and
global size limits, exact receipt-hash matching, strict UTF-8 decoding, and
deterministic LF normalization. Undeclared files are never included. If that
evidence cannot be prepared, `REVIEW_EVIDENCE_UNAVAILABLE` stops the operation
before the reviewer starts.

The reviewer evaluates every acceptance criterion and every blocking criterion
exactly once. Each evidence entry must begin with a bundled artifact path. It
must not calculate hashes, explore the filesystem, reconstruct readiness, or
request commands; it returns only the existing structured semantic result.

Approval fails closed for missing, duplicated, or invented criteria; unknown or
ambiguous evidence; failed blocking criteria; or unresolved blocking issues. An
advisory criterion still requires evaluation and evidence, but its failure alone
does not block approval. Criterion severity comes from the approved plan, never
from reviewer-controlled output.

## Semantic decisions and infrastructure failures

These outcomes have different accounting:

| Outcome | Meaning | Semantic attempt consumed? | Normal recovery |
| --- | --- | ---: | --- |
| `APPROVE` | Criteria pass | No additional revision | CW verifies and creates the gate |
| `REVISE` | Implementation needs semantic correction | Yes | Same phase runs again |
| `HUMAN_REVIEW_REQUIRED` | Policy requires a person | No | Explicit human approval |
| Verification infrastructure/timeout | Executor did not produce authoritative evidence | No | `cw retry` |
| Reviewer timeout/network/process/invalid output/command event | Review could not produce a semantic decision | No | `cw retry` |

Infrastructure failures preserve valid session-bound readiness so retry can run
only the reviewer. If the implementer exits after writing readiness but before
the Stop hook completes, retry also proceeds directly to review.

Bundled repository content is hostile input to the reviewer. Instructions in
artifact text cannot alter its mandate. The reviewer
evaluates acceptance semantics, scope, Completion Contract, artifacts,
coherence, integrity, and risk; it neither reruns deterministic checks nor
approves merely because they passed.

Historical misclassification is corrected only with [`cw review
recover-infrastructure`](reviewer-infrastructure.md). Recovery and retry are
separate: recovery repairs append-only evidence and accounting; retry performs
the later fresh verification/review operation.

!!! note "Optional integrations"
    Optional MCP startup or authentication diagnostics cannot turn an otherwise
    successful structured reviewer result into a semantic failure. Required
    integrations are preflighted before the agent starts.

## Human gates

After technical approval of a human-gated phase:

```bash
cw review --human-approve
```

CW revalidates review identity, decision, complete criteria, blocking issues,
and artifact hashes before accepting the human action. This command cannot
bypass invalid evidence or approve a different phase.

## Append-only audit evidence

Review records are append-only. Each semantic or infrastructure result receives
a unique timestamped file created atomically without replacing earlier records.
Reopening a phase may restart its semantic counter, but it never rewrites the
previous review cycle.

Use `cw history` for the phase audit view and `cw history --phase PHASE` to
focus on one phase.

## Correcting a reviewed plan

A `REVISE` may expose a defect in the plan contract itself rather than in the
implementation. Generic rebuild is unsafe because an old review cannot be
reinterpreted against new criteria. Use [Plan revisions and review
supersession](plan-revisions.md). Supersession does not delete, edit, reverse,
or approve the old review. It selects a new active plan revision, returns the
phase to `READY`, and requires a new validation/review/gate cycle.

## Phase gate versus completion evidence

A phase gate proves one bounded milestone. It never claims that the composed
product meets its declared readiness target. Contract-aware projects therefore
use a separate read-only [completion review](completion-contracts.md) and a
distinct completion evidence artifact. Later system findings do not rewrite or
invalidate an honestly earned phase gate.
