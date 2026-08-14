# Getting started with CW

This guide takes a new Git repository from installation to its first controlled
implementation phase.

## Requirements

- Git;
- Python 3.10 or newer;
- the Codex CLI, installed and authenticated for planning, implementation, and review;
- a POSIX-compatible shell for the current managed source installer.

CW resolves the active project with `git rev-parse --show-toplevel`. It never
derives application identity from the directory where CW itself is installed.

## Install CW

Clone the canonical repository and run the managed installer:

```bash
git clone https://github.com/Queopius/cw.git
cd cw
./install.sh
```

The installer stages and smoke-tests a versioned runtime under
`~/.local/share/cw/versions/`, switches the `current` pointer atomically, and
creates `~/.local/bin/cw`. It does not require the source checkout after
installation.

If the command is not immediately on `PATH`, restart the shell or run the exact
line reported by the installer.

!!! note "Native Windows installation"
    CW's execution core includes a Windows process abstraction, but the current
    managed source installer uses POSIX shell and symlink semantics. Do not infer
    a native Windows installation workflow from the Linux/macOS command above.

## Verify the installation

```bash
cw version --verbose
cw doctor
```

`cw version --verbose` identifies the executable, managed runtime, build
fingerprint, source fingerprint, and whether a source checkout differs from the
installed build. `cw doctor` performs local environment, project, and integrity
checks; it does not contact a reviewer unless you explicitly add `--reviewer`.

## Initialize a project

Change into the application repository—not the CW source repository—and
initialize project-local metadata:

```bash
cd ~/code/my-project
cw init
```

CW creates its `.cw/` workflow area and repository-local Codex integration. It
does not change global Codex configuration.

## Create a development plan

Give the planner a concrete goal:

```bash
cw plan --goal "Implement subscription billing"
cw plan show
```

The planner reads bounded repository evidence in a read-only Codex session. If
the goal cannot be inferred safely, CW asks for `--goal` rather than inventing
work. A planner timeout or transport failure writes no partial plan and can be
retried with `cw retry`.

## Approve the plan

Review phase order, dependencies, acceptance criteria, artifacts, required
commands, human gates, and integrations before approval:

```bash
cw plan approve
```

Approval freezes the implementation contract. The implementer cannot rewrite
its own phase criteria merely to pass review.

## Run the first phase

```bash
cw
```

`cw` is the conservative single-phase command. It starts or resumes the
canonical current phase, streams truthful activity, runs deterministic
validation, invokes a separate read-only reviewer, and advances only after a
valid gate exists.

Codex may ask you to trust the repository Stop hook. Inspect it through Codex's
hook interface before trusting it; CW does not bypass hook trust.

## Inspect progress

```bash
cw status
cw history
```

`cw status` derives progress from validated contiguous gates. `cw history`
shows retained audit evidence. If state and evidence contradict each other,
status fails closed and directs you to a safe diagnosis instead of rendering a
misleading timeline.

## What happens next

- `APPROVE` plus valid evidence creates a gate and advances to the next phase.
- `REVISE` keeps the same phase and consumes one semantic review attempt.
- an infrastructure failure preserves progress and may be handled by `cw retry`.
- a human gate stops until `cw review --human-approve` verifies explicit approval.
- the final valid gate sets `COMPLETED`, clears the current phase, and launches no successor.

Use [`cw run`](batch-execution.md) only when you intentionally authorize a
bounded multi-phase batch.

## Common first-run issues

### CW is not found

Restart the shell or add `~/.local/bin` to `PATH`, then run `cw version` again.

### Codex is missing or cannot start

```bash
cw doctor
cw error
```

Install or authenticate Codex through its own tooling. CW does not store Codex
credentials.

### The managed build is stale

`cw version --verbose` reports `Source match NO` when the installed runtime and
current checkout differ. Re-run `./install.sh` from the intended source build
before testing undocumented source behavior.

### An optional integration reports an error

```bash
cw integrations check
```

Optional authentication or startup diagnostics do not block an unrelated
phase. A phase-declared required integration is preflighted and does block when
unavailable.

### Workflow state is inconsistent

```bash
cw explain
cw doctor
cw repair
cw status
```

Repair is backup-first and evidence-driven. It does not create approval gates or
modify application code. See [Troubleshooting](troubleshooting.md).

## Exit codes

| Code | Meaning |
| ---: | --- |
| `0` | Requested operation completed successfully. |
| `1` | Workflow or operational failure. |
| `2` | Invalid CLI usage or configuration. |
| `3` | Explicit human action is required. |
| `130` | Foreground operation interrupted by the user. |

For full syntax and options, use the [CLI reference](cli-reference.md).
