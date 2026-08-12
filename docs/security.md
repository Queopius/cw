# Security and privacy

CW applies least privilege to agent roles:

- planner: `read-only`, ephemeral, hooks and web search disabled;
- implementer: `workspace-write`;
- reviewer: `read-only`, ephemeral, hooks disabled;
- unrestricted sandbox: never part of normal operation.

Implementer network access is denied by default through Codex's documented
`sandbox_workspace_write.network_access` override. CW also disables web search
for that invocation unless project policy explicitly allows network access. The
reviewer remains read-only and has web search disabled so its decision is based
on repository evidence. Planner and reviewer also set `project_doc_max_bytes=0`
so repository `AGENTS.md` content is treated as scoped evidence rather than
ambient instructions; the implementer still loads the managed workflow section.

CW does not bypass Codex hook trust. Project paths reject absolute values,
`..` traversal, null bytes, and resolved symlink escapes. Readiness manifests
cannot introduce commands; only approved workflow commands execute. Deterministic
commands run as an argument vector without a shell. Shell interpreters, pipelines,
redirections, command substitution, and other shell control syntax are rejected
when the workflow loads. State, reviews, gates, and project identity use atomic
temp-write, fsync, and replace.

Before acquiring a project lock or installing integration files, CW validates
the repository topology. `.cw`, `.codex`, their managed subdirectories,
`AGENTS.md`, project identity/state/config, runtime manifests, hooks, schemas,
and the workflow plan cannot be symlinks or special files. This prevents a
hostile or accidental repository layout from redirecting CW writes outside the
Git worktree. Repair backups preflight every included metadata tree and refuse
nested symlinks before creating the backup destination.

Before each implementer session, CW snapshots mandatory workflow metadata and
all configured `protected_paths`. After Codex exits, existing protected content
must be unchanged. The only accepted additions are one current-phase review and,
when approved, one gate produced through a consistent hook transition. CW checks
the state/history delta, every configured criterion, the gate-to-review link,
the required human-approval type, and the complete artifact hash set. A mismatch
sets `PROTECTED_PATH_MODIFIED` and fails closed; it is not automatically retryable.

Readiness manifests are bound to the random ID of the current `cw start`
invocation. This prevents accidental reuse or replay across sessions. The Stop
hook checks the implementer and session environment before invoking review, so
ordinary Codex sessions in an initialized repository do not trigger CW.
The session carries an owner-process lease to prevent concurrent implementers
from replacing each other's identity; no unrestricted sandbox is used to
enforce this coordination.

Repository repair never treats a matching basename as proof of identity. A
foreign repository fingerprint causes project-specific metadata to be retained
only in the repair backup and removed from the active workflow. This prevents
`cw repair` from becoming a cross-project plan or approval import mechanism.

CW applies best-effort redaction for common credential forms before persisting
workflow errors or diagnostic records. Diagnostic files remain local under
`.cw/logs/`, are never approval evidence, and should still be treated as
sensitive. Raw mode means complete redacted diagnostic, not secret bypass.

CW sends no telemetry. Repository content can be sent to Codex when a planner,
implementer, or reviewer runs. Planning sends a bounded evidence selection over
stdin, and review prompts scope the phase and review paths. Do not place secrets
in plans, prompts, artifacts, or diagnostic logs.

CW inspects Git metadata but never automatically pushes, merges, rebases, cleans,
or resets a repository.

Codex sandbox configuration follows the official
[OpenAI configuration reference](https://developers.openai.com/codex/config-reference/).
