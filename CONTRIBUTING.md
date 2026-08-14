# Contributing to CW

CW requires Python 3.10 or newer and uses the standard library for its runtime.

Run the full local check before proposing a change:

```bash
make check
```

Build or preview the documentation with isolated documentation dependencies:

```bash
python -m pip install -r docs/requirements.txt
mkdocs serve
```

Run the release-equivalent documentation gate with:

```bash
mkdocs build --strict
```

Keep workflow domain logic out of shell launchers and UI modules. New state
transitions must be explicit and tested. Never add project-specific plans,
reviews, gates, identities, or mutable state to `cw/templates/`.

Ongoing work targets `dev` and is promoted through `staging`, `release`, and
`prod`. Version tags are created only from `release`; see
[`docs/release-process.md`](docs/release-process.md).
