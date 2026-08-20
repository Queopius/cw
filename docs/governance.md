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
base/head branches and their exact SHAs, authenticated owner, timestamp,
required and observed checks, unresolved conversations, policy fingerprint,
mode, and outcome. It is not a GitHub review. Changing either SHA, checks, or
policy requires a new authorization. Tokens and secrets are never recorded.

Schema 1 evidence without `base_sha` remains readable historical evidence but
is never merge-valid and is never reused. Do not edit, overwrite, delete, or
guess a historical base SHA. Invalidate it explicitly, preserving the original:

```bash
cw governance invalidate --pr 37 \
  --head-sha 8bb0b24094df80ce2d048b558f3a493b143e8d16 \
  --reason incomplete-base-sha-evidence
```

Non-interactive invalidation additionally requires `--yes --non-interactive`.
CW writes append-only invalidation evidence containing a hash of the original,
then requires a separate fresh human `cw governance authorize` operation.

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
and complete current-schema authorization are idempotent only while head, base,
checks, policy, and live PR state match. Legacy incomplete authorization is
classified fail-closed and requires supported invalidation. No migration changes gates, reviews,
Completion Contracts, Plugin metadata, or remote protocol state.
