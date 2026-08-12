# Security policy

Please report suspected vulnerabilities privately to the repository owner rather
than opening a public issue. A dedicated disclosure address must be chosen before
public release.

CW runs implementers with `workspace-write` and independent reviewers with
`read-only`. It never bypasses Codex hook trust or uses unrestricted sandbox mode
in normal operation. Validation commands come only from the approved workflow,
not from readiness manifests.
