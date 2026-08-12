# Gates and reviews

Review records are append-only. Each semantic or infrastructure result receives
a unique timestamped name and is created atomically without replacing an existing
file. Reopening a phase may restart its semantic attempt counter, but it never
rewrites evidence from the earlier review cycle.

Validation order is fixed:

1. readiness structure and state;
2. dependency gates;
3. artifact declaration and existence;
4. repository containment and symlink safety;
5. approved workflow commands;
6. SHA-256 artifact capture;
7. independent semantic review.

The reviewer uses a separate ephemeral `codex exec` process with `read-only`,
approval policy `never`, hooks disabled, and a JSON output schema. It reviews
only current-phase paths and must evaluate every configured criterion exactly
once with evidence.

Approval fails closed for missing, duplicated, or invented criteria; unknown or
ambiguous evidence; any failed blocking criterion; or remaining blocking issues.

Semantic `REVISE` results increment the phase attempt. Timeouts, network errors,
transport errors, process crashes, and invalid reviewer transport output set
`ERROR` without consuming a semantic attempt. `cw retry` reuses the existing
session-bound readiness manifest and does not restart implementation. If the
implementer itself exits after writing readiness but before the Stop hook
finishes, retry also proceeds directly to review.

Approval writes `.cw/gates/<phase>.approved.json` with workflow/version, review
reference, timestamp, optional Git commit, CW version, and artifact hashes.
Gate validation requires the referenced semantic review, an exact criterion set,
a consistent decision, the complete declared artifact set, and the required
human-approval marker. Changed approved artifacts or review evidence invalidate
the gate; CW never recreates it silently.
After semantic approval of a human-gated phase, `cw review --human-approve` is
the explicit local action that creates its gate.
