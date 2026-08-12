# Security and privacy

CW applies least privilege to agent roles:

- implementer: `workspace-write`;
- reviewer: `read-only`, ephemeral, hooks disabled;
- unrestricted sandbox: never part of normal operation.

CW does not bypass Codex hook trust. Project paths reject absolute values,
`..` traversal, null bytes, and resolved symlink escapes. Readiness manifests
cannot introduce commands; only approved workflow commands execute. Deterministic
commands run as an argument vector without a shell. Shell interpreters, pipelines,
redirections, command substitution, and other shell control syntax are rejected
when the workflow loads. State, reviews, gates, and project identity use atomic
temp-write, fsync, and replace.

CW sends no telemetry. Repository content can be sent to Codex when an
implementer or reviewer runs. Planning selects only relevant local evidence, and
review prompts scope the phase and review paths. Do not place secrets in plans,
prompts, artifacts, or diagnostic logs.

CW inspects Git metadata but never automatically pushes, merges, rebases, cleans,
or resets a repository.
