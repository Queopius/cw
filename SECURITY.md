# Security policy

Please report suspected vulnerabilities privately through
[GitHub Security Advisories](https://github.com/Queopius/cw/security/advisories/new).
Do not open a public issue for an unpatched vulnerability.

CW runs implementers with `workspace-write` and independent reviewers with
`read-only`. It never bypasses Codex hook trust or uses unrestricted sandbox mode
in normal operation. Validation commands come only from the approved workflow,
not from readiness manifests.
