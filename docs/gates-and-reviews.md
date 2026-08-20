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

Changed artifacts, review evidence, or dependencies invalidate the gate. CW
never recreates it silently.

> **No valid gate. No next phase.**

## Validation before review

The order is fixed:

1. readiness structure and state;
2. dependency gates;
3. artifact declaration and existence;
4. repository containment and symlink safety;
5. approved workflow commands;
6. dependency gate revalidation;
7. final SHA-256 artifact capture;
8. independent semantic review.

Commands run before authoritative hashes are captured. CW then revalidates
dependencies, preventing a test or formatter from silently changing current
artifacts or previously approved evidence.

## Independent reviewer contract

The reviewer uses a separate ephemeral `codex exec` process with:

| Property | Reviewer behavior |
| --- | --- |
| Sandbox | `read-only` |
| Approval policy | `never` |
| Hooks | disabled |
| Output | validated structured schema |
| Scope | current-phase artifacts and configured review paths |

It evaluates every acceptance criterion and every blocking criterion exactly
once. Each evidence entry must begin with an existing repository-relative file
inside the allowed review scope.

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
| Timeout/network/process/schema failure | Review could not produce a decision | No | `cw retry` when classified retryable |

Infrastructure failures preserve valid session-bound readiness so retry can run
only the reviewer. If the implementer exits after writing readiness but before
the Stop hook completes, retry also proceeds directly to review.

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

## Phase gate versus completion evidence

A phase gate proves one bounded milestone. It never claims that the composed
product meets its declared readiness target. Contract-aware projects therefore
use a separate read-only [completion review](completion-contracts.md) and a
distinct completion evidence artifact. Later system findings do not rewrite or
invalidate an honestly earned phase gate.
