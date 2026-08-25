# CW Early Adopter Program

The CW Early Adopter Program is a small, invitation-based pilot for technical
teams that want to evaluate gate-driven AI-assisted development in real work
without placing critical systems at risk.

The initial cohort is intentionally limited to 5–10 organizations in Spain,
Portugal, and Latin America. Working communication for the pilot is available
in Spanish and Portuguese.

## Purpose

The program exists to collect evidence from real, bounded use of CW. It focuses
on:

- installation and update friction;
- configuration and first-run clarity;
- the usefulness of `cw doctor`, `cw status`, and `cw history`;
- planning, validation, independent review, and gate behavior;
- safeguards that block too much or too little;
- recovery guidance and diagnostic quality;
- machine-readable output through `--llm`; and
- documentation gaps.

Participation does not change CW's evidence requirements or security model.
Feedback may inform future work, but it does not bypass review, validation, or
release governance.

## Who the pilot is for

The pilot is designed for technical decision-makers and hands-on engineering
leaders, including:

- technical founders;
- CTOs and Heads of Engineering;
- Engineering Managers;
- innovation or AI engineering leaders;
- Developer Experience owners; and
- open-source maintainers.

A suitable participant can assign a bounded test project, provide technically
specific feedback, and distinguish product friction from project-specific
behavior.

## Safe pilot scope

Use CW only in an isolated, recoverable, non-critical repository during the
pilot. The repository should have:

- version control and a known-good baseline;
- no live production dependency;
- no required access to production credentials or customer data;
- a reviewable validation command set; and
- a person responsible for approving the experiment.

Do not use the pilot as the sole control for production deployment, financial
side effects, destructive migrations, credential rotation, cryptographic
changes, or other high-impact operations. Keep normal backups, branch
protection, code review, and deployment controls in place.

## Getting started

1. Select an isolated, non-critical repository.
2. Read the [Getting Started](getting-started.md) and
   [Workflow](workflow.md) guides.
3. Install the latest stable CW release using the documented installation path.
4. Record the environment and `cw version --verbose` output.
5. Run `cw doctor` before initializing the pilot repository.
6. Define a bounded goal and review the generated plan before approval.
7. Exercise the normal plan, implement, validate, review, and gate flow.
8. Record friction and unexpected behavior as it occurs.

Do not paste proprietary source code, credentials, personal data, or unredacted
logs into public reports.

## Reporting feedback

Use the repository's structured issue forms:

- open a **Bug report** for incorrect or unexpected reproducible behavior;
- open a **Feature request** for a concrete workflow or documentation
  improvement; and
- search existing issues before submitting a new report.

A useful report includes the CW version, installation method, environment,
command, expected behavior, actual behavior, minimal reproduction steps, and
sanitized diagnostics.

### Security reports

Never open a public issue for a suspected unpatched vulnerability. Report it
privately through
[GitHub Security Advisories](https://github.com/Queopius/cw/security/advisories/new)
and follow the [security policy](https://github.com/Queopius/cw/security/policy).

## Participation boundaries

The initial program is invitation-based and manually reviewed. Joining the
pilot does not grant permission to publish another organization's source code,
logs, credentials, internal documentation, or security findings.

CW remains an Apache-2.0 project. The pilot provides an evaluation channel, not
a guarantee that every request will be accepted or included in a particular
release.
