## CW by Queopius · Codex Workflow

This repository uses a gated CW workflow.

- Work only on the current CW phase identified by `.cw/state.json`.
- Read `.codex/workflow/phases.yaml` before implementation.
- Do not self-approve.
- Do not advance workflow state.
- Do not modify approval gates or previous reviews.
- Do not change acceptance criteria while implementing.
- Keep mutable workflow data under `.cw/`; `.codex/` is static integration.
- When the phase is complete, create `.cw/runtime/READY_FOR_REVIEW.json` with the exact active `session_id` supplied by CW, then stop normally.

No valid gate. No next phase.
