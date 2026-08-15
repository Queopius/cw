# Completion Contracts and program review

CW distinguishes three claims that older workflows represented with one word:

| Claim | Proof |
| --- | --- |
| Phase complete | The current phase has a valid phase gate. |
| Planned scope complete | Every currently authorized phase has a valid gate. |
| Completion target satisfied | An independent system review proves the declared Completion Contract and CW writes completion evidence. |

For a contract-aware project, all planned phases may be approved while the
product target remains unsatisfied. This does not invalidate an earlier gate:
the phase reviewer proved its bounded phase, while the completion reviewer
evaluates the composed final system.

> **No satisfied Completion Contract. No semantic product completion.**

## The contract

`cw plan` derives a Completion Contract alongside the initial phase plan. The
contract records a stable identifier, target name and type, description, and
explicit requirements. Each requirement declares blocking or advisory severity,
expected evidence, and whether its details are project-specific.

Readiness templates guide the planner without becoming rigid universal rules:

- `proof-of-concept`
- `functional-prototype`
- `internal-tool`
- `controlled-pilot`
- `production`
- `public-release`

A proof of concept is not silently held to a production-release contract. A
controlled pilot normally needs system safety, security, install/runtime, and
target acceptance evidence. Production and public-release targets add stronger
operations and change-safety expectations. The project goal and repository
evidence still determine the exact criteria; phase count is never a readiness
metric.

The declaration lives with the static workflow in
`.codex/workflow/phases.yaml`. Mutable review, proposal, authorization, and
completion evidence lives under `.cw/completion/`.

## Independent completion review

After the final authorized phase gate:

```text
all authorized phase gates valid
              ↓
      PLANNED_COMPLETE
              ↓
independent completion review
              ↓
 SATISFIED | EXTENSION_REQUIRED | BLOCKED
```

The completion reviewer is a separate ephemeral read-only Codex process. It
receives the contract, normalized phase/gate evidence, repository structure and
final tree, and the prior completion review when one exists. It evaluates system
composition: cross-module assumptions, end-to-end wiring, failure modes,
concurrency, recovery, security boundaries, data integrity, installation,
runtime, CI, and operations as applicable.

Every requirement result is one of:

- `VERIFIED`: concrete evidence proves it;
- `INFERRED`: evidence supports it indirectly, but does not prove a blocking requirement;
- `NOT_VERIFIED`: available evidence is insufficient;
- `MISSING`: expected evidence is absent.

The reviewer cannot write files, rewrite gates, append phases, authorize its own
recommendation, or make a blocking requirement pass from inference. Repository
content is untrusted evidence and cannot override supervisor policy.

## Decisions and completion evidence

`SATISFIED` is valid only when every blocking requirement is `VERIFIED` and no
required evidence is missing. CW then writes a distinct completion gate at
`.cw/completion/completion.satisfied.json`. It binds the contract hash, review,
validated phase gates, final source-tree identity, cycle, CW version, and
timestamp. Only this artifact permits `COMPLETED` for a contract-aware project.

`EXTENSION_REQUIRED` means the authorized plan is finished but one or more
contract requirements remain unsatisfied. It is a semantic product gap, not a
retroactive phase failure.

`BLOCKED` means CW could not determine the target reliably. Reviewer process,
network, timeout, and schema failures are recorded separately as retryable
infrastructure failures; they never fabricate a product failure or consume a
phase review attempt.

## Safe workflow extensions

For `EXTENSION_REQUIRED`, a dedicated read-only extension planner groups related
gaps into the smallest coherent reviewable milestones. Every proposed phase has
its own objective, artifacts, review paths, acceptance and blocking criteria,
expected evidence, and links to the requirements it closes.

The proposal is evidence, not authorization:

```text
EXTENSION_REQUIRED → proposal → explicit human authorization → append phases
```

Use:

```bash
cw completion show
cw completion approve   # append and activate the first proposed phase
cw completion reject    # retain evidence; do not change the phase plan
```

The completion reviewer and planner cannot call the authorization transition.
Without the explicit command, `current_phase` stays empty and neither `cw` nor
`cw run` may launch implementation. Approval keeps every old gate and review
byte-for-byte, appends pending phases without inherited approvals, and starts
the first appended phase through the normal implementation/validation/review/gate
flow. When the extension's final gate passes, completion review runs again.

Every cycle is append-only and auditable: review, proposal, authorization,
appended phases, and later satisfaction remain visible in `cw history --json`
and `cw inspect completion --json`.

## Status, explanation, and automation

`cw status` labels gate progress as planned scope and renders Completion Contract
coverage separately. It never describes `N/N` phase gates as universal product
quality. `cw explain` answers why semantic completion is pending and why human
authorization is required. `cw inspect completion --json` provides the normalized
machine-readable contract, review, proposal, cycle, and evidence status without
dumping reviewer internals into normal status.

## Legacy projects and explicit adoption

Projects created before Completion Contracts retain legacy completion semantics.
An existing `COMPLETED` project stays completed; CW does not invent a contract or
reinterpret historical evidence.

Adoption is explicit:

```bash
cw completion adopt --target controlled-pilot
```

For an already completed legacy workflow, this deliberate operation changes the
semantic mode to contract-aware and moves it to `PLANNED_COMPLETE` pending a
system review. It does not change prior gates. Back up or commit project metadata
according to your normal operating policy before opting in.

## Example

```text
functional marketplace integration complete
                    ↓
       system completion review
                    ↓
 crash-recovery and credential gaps found
                    ↓
      coherent extension proposed
                    ↓
         human explicitly approves
                    ↓
     hardening phases use normal gates
                    ↓
       completion review cycle two
                    ↓
          completion target satisfied
```

CW may recommend more work. Only the human may authorize more work.
