# Release process

CW by Queopius uses four long-lived branches:

```text
dev → staging → release → prod
```

- `dev` receives ongoing implementation.
- `staging` receives integrated candidates for broader validation.
- `release` contains versioned release candidates and is the only branch from
  which version tags are created.
- `prod` is the default branch and represents the current production release.

Promote changes with reviewed pull requests in that order. Do not force-push
long-lived branches.

## Creating a release

1. Confirm CI passes on `release`.
2. Confirm the documentation quality gate passes:

   ```bash
   mkdocs build --strict
   ```

3. Confirm platform acceptance for the exact release candidate:

   ```bash
   make acceptance-local
   ```

   GitHub checks must also pass for Linux x86_64, Windows x86_64, macOS arm64,
   and macOS Intel while an official Intel runner remains available. A Linux
   local result is not evidence for another OS. Review the uploaded sanitized
   compatibility reports before promotion.

4. Update `VERSION`, `cw.__version__`, `pyproject.toml`, and `CHANGELOG.md` in the
   release candidate.
5. Create an annotated tag while checked out on `release`:

   ```bash
   git switch release
   git tag -a v0.2.0 -m "CW by Queopius 0.2.0"
   git push origin release v0.2.0
   ```

6. Promote the tagged release commit to `prod` through review.

The Release Check workflow rejects tags whose commit is not reachable from
`origin/release` or whose name does not match the repository `VERSION`. It builds
artifacts for inspection but does not publish them to PyPI.

## Platform release gate

The deterministic platform workflow builds and installs the wheel outside the
checkout, executes `cw` as an external command, runs a real subprocess fake for
the public Codex contract, and exercises completion and recovery. A stable
release requires installation, CLI smoke, deterministic E2E, and core recovery
to pass on each claimed supported OS.

Update/rollback and native process/interrupt tests are required for the stronger
**verified** status. The manual `Real Codex Acceptance` workflow is an additional
attestation: it is never replaced by fake-Codex evidence and reports
`NOT CONFIGURED` when its explicit credential is absent. See [Platform support
and certification](testing/platform-support.md).

## Documentation validation

Documentation dependencies are isolated from the CW runtime. Install them and
preview the site locally with:

```bash
python -m pip install -r docs/requirements.txt
mkdocs serve
```

Run the same strict validation used by CI with:

```bash
make docs-check
```

This validates the public CLI snapshot/reference, complete error-code coverage,
local documentation links/anchors, and the strict MkDocs build. Warnings fail
the documentation job and block release promotion.

## Read the Docs publishing

The canonical public documentation URL is <https://docs.cwcli.dev>. Read the
Docs should build the production documentation from `prod`; `dev` may be
activated separately as a non-default development version. Repository changes
continue through the normal `dev → staging → release → prod` promotion path.
The intended Read the Docs project name is `CW — Codex Workflow`, with the
suggested slug `cw-codex-workflow`.

The repository configuration prepares a reproducible strict MkDocs build, but
domain attachment remains an external operation. After the first successful
Read the Docs build:

1. add `docs.cwcli.dev` in the Read the Docs project domain settings;
2. obtain the exact DNS target assigned by Read the Docs;
3. create only that supplied DNS record and verify HTTPS.

Do not publish an interim `readthedocs.io` hostname as CW's canonical public
documentation identity, and do not guess the custom-domain DNS target.

Before promotion, run the offline installation/isolation demonstration:

```bash
make demo
```

It installs CW by copy into a temporary HOME, runs `cw init` in two independent
Git repositories, generates repository-specific plans without network access,
approves a phase in repository A through a simulated independent reviewer, and
asserts that repository B's identity, plan, state, and gates are byte-for-byte
unchanged. The underlying reproducible runner is `scripts/demo_isolation.py`.

## Hero demo artifact

The landing-page hero contract is a sanitized structured recording of a real CW
workflow, not a hand-authored transcript. Validate the committed artifact on
every release with:

```bash
make demo-check
```

Re-record it only when the visible lifecycle changes materially. Recording is a
maintainer operation that uses a disposable Git repository, the current
installed CW build, real Codex planning/implementation/review, and a verified
approval gate:

```bash
python scripts/record_hero_demo.py --dry-run
python scripts/record_hero_demo.py
```

The real recording needs Codex authentication and network access; CI never
regenerates it. A failed recording must preserve the last-known-good
`demo/hero/hero-demo.json`.
