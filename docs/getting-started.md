# Getting started

## Requirements

- Git
- Python 3.10 or newer
- Codex CLI for implementation and independent review

Install once with `./install.sh`. The installer is idempotent, copies the package
to `~/.local/share/cw`, creates `~/.local/bin/cw`, and adds that bin directory to
`.profile` and `.zshrc` only when the exact PATH line is absent.

From an application repository:

```bash
cw init
cw plan --goal "Describe the intended change"
cw plan show
cw plan approve
cw
```

`cw init` always resolves the repository with `git rev-parse --show-toplevel`.
It does not derive a project from the installation path. With no reliable goal,
`cw plan` fails closed and asks for `--goal` instead of inventing work.

Codex may ask you to trust the project Stop hook. Review it with `/hooks`; CW
does not bypass hook trust automatically.

`cw doctor --reviewer` performs a separate, ephemeral read-only connectivity
request without asking the reviewer to inspect repository content. Ordinary
tests and ordinary `cw doctor` runs make no network request.

Exit codes are stable: `0` success, `1` workflow/application failure, `2` usage
or configuration failure, and `3` human action required.
