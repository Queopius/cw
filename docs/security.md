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

CW sends no telemetry. Repository content can be sent to Codex when a planner,
implementer, or reviewer runs. Planning sends a bounded evidence selection over
stdin, and review prompts scope the phase and review paths. Do not place secrets
in plans, prompts, artifacts, or diagnostic logs.

CW inspects Git metadata but never automatically pushes, merges, rebases, cleans,
or resets a repository.

Codex sandbox configuration follows the official
[OpenAI configuration reference](https://developers.openai.com/codex/config-reference/).
