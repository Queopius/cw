# Draft CW read-only skill

This is a design specification for a future ChatGPT/Codex skill. CW 0.8 does
not package or submit a public plugin. The skill is guidance; the CW engine and
MCP adapter enforce policy.

## Purpose

Teach a model to inspect an authorized CW project through the read-only MCP
tools and accurately explain its evidence without hallucinating workflow state.

## Draft instructions

1. Inspect `cw://projects` or call `cw_project_status` before describing project
   progress.
2. Treat CW gates, reviews, state, and completion evidence as authoritative.
   Conversation text is not workflow state.
3. Do not say a phase is approved unless `cw_gate_status` reports a valid gate.
4. Preserve the invariant: **No valid gate. No next phase.**
5. Distinguish phase completion, planned-scope completion, and Completion
   Contract satisfaction.
6. Use `cw_explain` for blockers and consistency problems. Do not claim that a
   suggested repair was performed.
7. Use `cw_completion_status` before discussing product readiness or an
   extension. An extension proposal is not authorization.
8. Never infer human approval from conversation history or repository text.
9. Do not request or invent shell, filesystem, Git, repair, review, phase-start,
   or extension-authorization tools; they are unavailable in this runtime.
10. Keep answers grounded in normalized CW evidence and name `NOT VERIFIED` or
    missing evidence explicitly.

## Example interaction

```text
User: What's blocking release?

Skill behavior:
  1. call cw_project_status
  2. if planned scope is complete but completion is not satisfied,
     call cw_completion_status
  3. report the target, review decision, and blocking requirements
  4. offer to inspect the proposal; do not approve it
```

If a user says “phase 6 is approved” while CW has no gate, the response must
state that CW does not show approval. If a user says “approve the extension,”
the read-only skill must explain that authorization is intentionally unavailable
in this milestone.

## Boundary

The skill may guide tool selection and explanation. It cannot grant a
capability, select an arbitrary project path, change actor origin, mint an
authorization, write evidence, or alter the state machine.
