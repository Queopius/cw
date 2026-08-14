# Platform support and acceptance evidence

CW treats compatibility as an execution result, not a source-code inference.
The authoritative evidence for a candidate commit is the set of sanitized
`compatibility-report.json` artifacts produced by the **Platform Acceptance**
workflow.

## Candidate validation matrix

| Concern | Environment | Python |
| --- | --- | --- |
| Language compatibility | Ubuntu, x86_64 | 3.10–3.14 in the main CI matrix |
| Linux acceptance | Ubuntu 24.04, x86_64 | 3.13 |
| Windows acceptance | Windows Server 2025, x86_64 | 3.13 |
| macOS acceptance | macOS 15, Apple Silicon | 3.13 |
| macOS Intel acceptance | macOS 15 Intel | 3.13 |

The explicit macOS labels avoid treating one architecture as evidence for the
other. Intel coverage remains conditional on GitHub continuing to provide a
supported Intel hosted runner.

## Evidence status for this hardening branch

| Platform | Current evidence | Status safe to claim |
| --- | --- | --- |
| Linux x86_64 | Local installed-wheel E2E and recovery pass | Candidate evidence; CI still required before release |
| Windows x86_64 | Native installer and tests implemented | Experimental; hosted execution pending |
| macOS arm64 | POSIX installer and tests configured | Experimental; hosted execution pending |
| macOS Intel | Dedicated runner configured | Not tested until hosted execution completes |

Never convert `CI REQUIRED`, `PENDING`, `SKIPPED`, or `NOT_CONFIGURED` into
`PASS` in release notes or public support claims.

## Certification policy

**Supported** requires all of the following on the named OS and architecture:

- clean package or managed installation passes;
- the installed `cw` command passes CLI smoke checks;
- deterministic fake-Codex E2E reaches a verified contiguous gate chain;
- inconsistent state and invalid-gate recovery fail closed correctly.

**Verified** additionally requires:

- staged update and rollback transactions pass;
- native process launch, streaming, timeout, interruption, and termination pass;
- a current real-Codex acceptance has completed through the normal planner,
  implementer, independent reviewer, gate, and completion path.

**Experimental** means infrastructure exists but one or more required native
executions are pending. **Blocked** means a known installation, state-safety,
gate, or recovery defect exists.

## What deterministic acceptance executes

`python scripts/run_acceptance.py`:

1. builds a wheel for the exact source version;
2. installs it into a clean virtual environment outside the checkout;
3. creates an isolated home/config/temp context;
4. creates a Git repository under a path containing spaces and Unicode;
5. executes the installed `cw` command as a subprocess;
6. places an external fake `codex` executable on `PATH`;
7. runs planning, approval, implementation, validation, independent review,
   gate verification, history, logs, inspect, doctor, and completion;
8. runs a three-phase contiguous chain with `cw run 3`;
9. proves stale state repair, invalid-gate rejection, semantic revision, and
   reviewer-infrastructure recovery;
10. emits a versioned compatibility report without private paths or secrets.

The fake executable emulates only supported process-level inputs and outputs.
It does not patch CW internals, expose reasoning, or count as real Codex.

## Portability audit

| Area | Initial classification | Hardening decision |
| --- | --- | --- |
| `pathlib`, `tempfile`, argv subprocesses | Portable | Retained |
| JSON persistence | Requires test | Explicit UTF-8/LF and native tests |
| global config paths | POSIX-specific | Central per-user resolver; AppData on Windows |
| PID liveness via `os.kill(pid, 0)` | POSIX-specific | Central native liveness abstraction |
| Codex interruption | Requires test | Managed process groups and native termination |
| directory `fsync` | POSIX-specific | POSIX durability; safe Windows no-op |
| active runtime symlink | Windows blocker | Atomic regular version marker on Windows |
| generated `python3 $(git …)` hook | Windows blocker | Platform-neutral `cw review --hook` command |
| deterministic command environment | Windows blocker | Preserve required Windows process variables |
| shell command construction | Portable/security-sensitive | Continue using argv lists and `shell=False` |
| update archive containment/hashes | Portable/security-sensitive | Preserved unchanged and retested |

## Real Codex acceptance

The **Real Codex Acceptance** workflow is manual. It requires an exact Codex CLI
version and the repository secret `CODEX_ACCESS_TOKEN`, authenticates via stdin,
and runs the existing real hero recorder against a disposable repository. It
uploads only the recorder's sanitized public artifact.

If credentials are absent, the workflow fails with **NOT CONFIGURED**. A fake
reviewer result must never be presented as real-Codex certification.

## Local command

```bash
make acceptance-local
```

This runs source tests, documentation validation, the installed-wheel E2E,
native host process/filesystem checks, and deterministic update/rollback tests.
On Linux it explicitly delegates Windows and macOS to CI.
