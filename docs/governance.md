# Release governance

CW distinguishes an individual maintainer from a team that can provide an
independent review. It never silently assumes another reviewer exists and never
changes GitHub settings during diagnosis.

## Choose a mode

```bash
cw governance configure --pr 34
```

CW offers individual maintainer, team with independent review, or GitHub
detection. Detection considers current write, maintain, or admin permission;
historical contributors are not reviewers.

For CI, choose explicitly:

```bash
cw governance configure --mode solo-maintainer --non-interactive
cw governance configure --mode team-reviewed --non-interactive
cw governance configure --mode detect --pr 34 --non-interactive
```

If GitHub is unavailable, explicit configuration remains available. An existing
explicit policy is preserved; changing it requires `--replace`.

## Individual maintainer

Solo mode retains pull requests, protected branches, required checks,
mergeability, and explicit human authorization. It replaces only an impossible
independent review with SHA-bound CW evidence.

```bash
cw governance diagnose --pr 34
cw governance authorize --pr 34
```

For CI, `--yes --non-interactive` is the only prompt-free authorization form.
Evidence under `.cw/governance/authorizations/` records repository, PR,
base/head branches, SHA, authenticated owner, timestamp, checks, mode, and
outcome. It is not a GitHub review and becomes stale after a SHA or branch
change. Tokens and secrets are never recorded.

If GitHub still requires another approval, generate a plan:

```bash
cw governance remote-plan --pr 34
```

The plan never applies changes. Solo policy keeps PRs and all checks, sets the
approval count to zero, blocks force pushes/deletion, and does not enable a
general administrator bypass. An administrator must review and apply only that
approval-count change in GitHub Settings.

## Team reviewed

Team mode requires a current approval from an authorized account other than the
author. CW distinguishes missing, pending, changes requested, invalid, stale,
and valid reviews. A prior-SHA approval is stale. CW never impersonates a
reviewer or presents authorization evidence as a GitHub review.

## Existing projects

Projects without `.cw/governance/policy.json` remain valid and are diagnosed as
unconfigured. Existing explicit policies are unchanged. Repeated configuration
and authorization are idempotent. No migration changes gates, reviews,
Completion Contracts, Plugin metadata, or remote protocol state.
