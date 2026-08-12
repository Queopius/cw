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
2. Update `VERSION`, `cw.__version__`, `pyproject.toml`, and `CHANGELOG.md` in the
   release candidate.
3. Create an annotated tag while checked out on `release`:

   ```bash
   git switch release
   git tag -a v0.2.0 -m "CW by Queopius 0.2.0"
   git push origin release v0.2.0
   ```

4. Promote the tagged release commit to `prod` through review.

The Release Check workflow rejects tags whose commit is not reachable from
`origin/release` or whose name does not match the repository `VERSION`. It builds
artifacts for inspection but does not publish them to PyPI.
Before promotion, run the offline installation/isolation demonstration:

```bash
make demo
```

It installs CW by copy into a temporary HOME, runs `cw init` in two independent
Git repositories, generates repository-specific plans without network access,
approves a phase in repository A through a simulated independent reviewer, and
asserts that repository B's identity, plan, state, and gates are byte-for-byte
unchanged. The underlying reproducible runner is `scripts/demo_isolation.py`.
