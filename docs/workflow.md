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

`APPROVED` does not by itself erase evidence or silently rebuild anything. The
next `cw start` validates the gate and its dependency artifact hashes before
selecting the next phase. The last approved phase transitions to `COMPLETED`.

Only one mutating operation can hold `.cw/locks/operation.lock`. A dead process
lock is recognized as stale and safely replaced.
