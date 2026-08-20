# Getting started with CW

Before protected release promotion, select governance explicitly with
`cw governance configure --pr NUMBER`. Choose `solo-maintainer` when the owner
is the only authorized reviewer, or `team-reviewed` when another authorized
account can approve. See [Release governance](governance.md).

This guide takes a new Git repository from installation to its first controlled
implementation phase.

## Requirements

- Git;
- Python 3.10 or newer;
- the Codex CLI, installed and authenticated for planning, implementation, and review;
- a POSIX shell on Linux/macOS, or PowerShell for native Windows installation.

CW resolves the active project with `git rev-parse --show-toplevel`. It never
derives application identity from the directory where CW itself is installed.

## Install CW

Clone the canonical repository and run the managed installer:

### Linux and macOS

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

### Native Windows PowerShell

```powershell
git clone https://github.com/Queopius/cw.git
Set-Location cw
Set-ExecutionPolicy -Scope Process Bypass
.\install.ps1
```

The native installer requires no Administrator rights. It stages versioned
runtimes under `%LOCALAPPDATA%\Queopius\CW`, uses an atomically replaced version
marker instead of a privileged symlink, creates `cw.cmd`, and adds only its
user-local bin directory to the user `PATH`. Open a new PowerShell window after
the first installation.

!!! warning "Evidence status"
    The native Windows path is implemented and exercised by the Windows GitHub
    Actions job. Until that job has run successfully on the candidate commit,
    treat Windows support as **experimental**, not certified. See the current
    [platform support and evidence policy](testing/platform-support.md).

### Remove a managed installation

CW does not currently provide an uninstall command. To remove it, first confirm
the managed locations with `cw version --verbose`, close active CW processes,
then remove only the reported CW runtime and launcher. The default POSIX paths
are `~/.local/share/cw` and `~/.local/bin/cw`; the default Windows runtime and
launcher are both beneath `%LOCALAPPDATA%\Queopius\CW`. On Windows, also remove
that exact `bin` entry from the **user** PATH through Environment Variables.
Project-local `.cw/` evidence is separate and is not removed by uninstalling the
CLI.

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

Restart the shell or add the reported installer bin directory to `PATH`, then
run `cw version` again. On Windows, verify the user `PATH` contains
`%LOCALAPPDATA%\Queopius\CW\bin` exactly once.

### Codex is missing or cannot start

```bash
cw doctor
cw error
```

Install or authenticate Codex through its own tooling. CW does not store Codex
credentials.

### The managed build is stale

`cw version --verbose` reports `Source match NO` when the installed runtime and
current checkout differ. Re-run `./install.sh` (POSIX) or `install.ps1`
(PowerShell) from the intended source build before testing undocumented source
behavior.

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
