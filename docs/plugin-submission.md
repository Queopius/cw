# Plugin submission readiness

CW has not been submitted or published. This document is the reviewer package
plan for a future explicitly authorized submission.

## Candidate listing

- **Name:** CW — Codex Workflow
- **Developer:** Queopius; final Fantomid LLC relationship requires human/legal approval
- **Category:** Coding
- **Positioning:** Govern AI-assisted development with phases, validation,
  independent review, and evidence-backed gates.
- **Core invariant:** No valid gate. No next phase.
- **Supported candidate capabilities:** current reads and narrow controlled
  actions only, subject to actual surface and OAuth scope.
- **Unavailable:** shell, generic filesystem/Git, direct gate approval,
  extension authorization, repair/rebaseline, release, and deployment.

## Reviewer notes and test cases

Reviewers should verify status, active phase, gates, history, Completion
Contract, and blocker explanation; then exercise only platform-available
controlled actions. Negative tests must cover unknown/unauthorized projects,
path and handle substitution, arbitrary commands, injected review decisions,
gate approval, human approval, extension authorization, replay conflicts, and
disconnect recovery. The full matrix is in
[the 0.12 acceptance record](acceptance/chatgpt-plugin-0.12.md).

## Checklist

| Requirement | Status |
| --- | --- |
| Current plugin manifest, skill, assets, deterministic archive | READY |
| Accurate tool schemas and permission annotations | READY locally |
| Public streamable-HTTPS MCP endpoint | MISSING |
| OAuth 2.1 protected resource and authorization metadata | MISSING |
| Domain verification challenge | NOT RUN |
| Production tenant/project isolation acceptance | NOT RUN |
| Public privacy policy and retention/deletion commitments | HUMAN/LEGAL INPUT |
| Terms of service | HUMAN/LEGAL INPUT |
| Final publisher/business identity and support contact | HUMAN/BUSINESS INPUT |
| Countries/regions, policy attestations, screenshots if requested | HUMAN/BUSINESS INPUT |
| Apps Management write permission and submission approval | HUMAN AUTHORIZATION REQUIRED |

The manifest intentionally does not invent a terms URL, registered `.app.json`
connection, remote server URL, or screenshots. Add them only when the actual
public artifacts exist and validate.

## Decision

**PLUGIN SUBMISSION READINESS: BLOCKED.** No submission action is authorized by
this milestone.
