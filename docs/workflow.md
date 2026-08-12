# Workflow

CW uses explicit states and rejects transitions outside the state graph:

```text
UNINITIALIZED → PLANNING → PLAN_PROPOSED → READY → IN_PROGRESS
IN_PROGRESS → READY_FOR_REVIEW → REVIEWING
REVIEWING → REVISION_REQUIRED | APPROVED | HUMAN_REVIEW_REQUIRED | ERROR
APPROVED → IN_PROGRESS | COMPLETED
```

The implementer cannot update state. It works only on the current phase, creates
`.cw/runtime/READY_FOR_REVIEW.json`, and stops. The Stop hook delegates to the
installed `cw review --hook`; it writes mutable data only under `.cw`.
CW snapshots protected workflow metadata around that session and admits only the
precise state, history, review, and gate delta produced by a valid current-phase
review. Any other protected mutation stops the workflow in `ERROR`.

Each `cw start` creates an atomic `.cw/runtime/implementer-session.json` and
passes its random session ID to the implementer and Stop hook. Readiness must
contain that exact ID, so a manifest from an older invocation cannot be replayed.
The hook is inert in unrelated Codex sessions and reviewer sessions. A semantic
decision consumes both runtime files; an infrastructure failure preserves them
so `cw retry` can rerun only the reviewer.

CW follows the official [Codex Stop hook contract](https://learn.chatgpt.com/docs/hooks):
terminal review outcomes return `continue: false`, while `decision: block` is
avoided because it asks Codex to create a continuation turn. A repeated event
with `stop_hook_active` is stopped explicitly.

`APPROVED` does not by itself erase evidence or silently rebuild anything. The
next `cw start` validates the gate and its dependency artifact hashes before
selecting the next phase. The last approved phase transitions to `COMPLETED`.

Only one mutating operation can hold `.cw/locks/operation.lock`. A dead process
lock is recognized as stale and safely replaced.

Every retained review, gate, and history event remains part of the workflow's
audit surface. `cw doctor` checks the entire surface, including records from
earlier phases, so tampering with old evidence cannot remain hidden behind a
healthy current state.
