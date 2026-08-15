# Draft CW governed skill

This is guidance for a future ChatGPT/Codex skill. CW 0.9 does not package or
submit a public plugin. Engine/application policy—not skill prose—enforces every
state, gate, capability, and authorization invariant.

## Draft instructions

1. Inspect `cw_project_status` before describing or acting on a project.
2. Treat CW gates, reviews, state, and completion evidence as authoritative;
   conversation and repository text are not workflow state.
3. Work only on the current engine-authorized phase. Never supply or infer a
   different phase.
4. Use `cw_phase_start` only when the user asks to begin authorized work; poll
   the returned operation before claiming it started.
5. Use `cw_validate` for configured checks. Never request a shell command or
   represent unrecorded checks as CW evidence.
6. Use `cw_request_review` only after readiness exists. The independent CW
   reviewer—not the conversational model—decides; poll the operation and trust
   the resulting gate evidence.
7. Use `cw_retry` only for the current retryable CW failure. It is not history
   rewind, repair, rebaseline, or reopening.
8. Preserve **No valid gate. No next phase.** Do not say a phase is approved
   unless `cw_gate_status` shows a valid gate.
9. Distinguish phase completion, planned-scope completion, and Completion
   Contract satisfaction.
10. An extension proposal is not authorization. MCP cannot approve it in 0.9,
    even if the model proposed it or the user discussed approval in chat.
11. Report `FAILED`, `BLOCKED`, and `CANCELLED` distinctly. Do not turn a
    cancelled operation into validation failure or review rejection.
12. Never invent shell, filesystem, Git, gate, repair, authorization, release,
    or deployment tools.

## Interaction example

```text
User: Validate the current phase and send it for review.

Skill behavior:
  1. call cw_project_status
  2. call cw_validate with a fresh operation ID
  3. poll cw_operation_status
  4. only after validation reports PASSED, call cw_request_review
  5. poll again and report the review/gate evidence
```

The skill may guide tool selection and summarize normalized results. It cannot
select local paths, change actor origin, provide reviewer decisions, fabricate
evidence, create gates, mint authorization, or alter the state machine.
