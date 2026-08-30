# Governance exceptions

Governance exceptions are append-only factual incident records. They do not
authorize a past action retroactively, replace SHA-bound CW authorization, or
permit future promotion. Each record identifies the exact commit, consequence,
validation evidence, human decision, and control remediation.

## 2026-08-30 planner transport prerequisite

Commit `0b8f0d7a46341629d1167c995ab27400dcc0ea95` reached `dev` through a
maintainer bypass of classic branch protection. The validated change fixes
planner prompt transport and pre-plan recovery. It was not recreated or
rewritten after the incident.

Because the existing Render staging service tracked `dev`, that push also
deployed the commit to public staging before a governed `dev → staging`
promotion. Health and readiness passed with Core `0.18.3`, Plugin `0.1.0`,
protocol `cw.remote.v1`, environment `staging`, and SQLite schema 1. Production
was unaffected. The incident is therefore classified
`GOVERNANCE_ORDERING_EXCEPTION`, not a product rollback event.

The maintainer accepted the validated commit as a one-time exception only on
condition that the control gaps are remediated before feature work resumes.
GitHub `dev` protection now applies to administrators. The canonical staging
Blueprint source is `staging`; the live Render service must be changed and
verified separately in Render before operator-revocation work resumes.

The machine-readable evidence is
[`governance-exception-2026-08-30.json`](../acceptance/governance-exception-2026-08-30.json).
