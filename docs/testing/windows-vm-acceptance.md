# Native Windows 11 VM acceptance

Use this procedure to supplement hosted CI with a clean native Windows 11 VM.
Running the test in WSL, Wine, a Linux container, or with a mocked
`platform.system()` is not native Windows evidence.

## Lab boundary

On an Ubuntu host, use an existing KVM/QEMU/libvirt installation and a properly
licensed Windows 11 installation source. CW does not distribute or prescribe a
Windows disk image. Configure a disposable snapshot before testing; do not
share the developer's home, Codex credentials, or production repositories into
the VM.

The VM should provide:

- Windows 11 x86_64 with current updates;
- PowerShell 7 or Windows PowerShell 5.1;
- Git for Windows;
- a supported Python (3.10 or newer; use 3.13 for the acceptance matrix);
- Codex CLI only for the optional real-service section.

## Clean installation

Open a normal, non-Administrator PowerShell window:

```powershell
git clone https://github.com/Queopius/cw.git
Set-Location cw
git switch hardening/cross-platform-acceptance
Set-ExecutionPolicy -Scope Process Bypass
.\install.ps1
```

Close PowerShell, open a new normal PowerShell window, then run:

```powershell
Get-Command cw
cw version --verbose
cw doctor
```

`cw doctor` outside a project may report that no repository is active; the
launcher, Python, Git, and Codex rows must still be interpreted separately.

## Deterministic native acceptance

From the source checkout:

```powershell
python -m unittest tests.test_platform tests.test_fake_codex
python scripts/run_acceptance.py --output artifacts\windows-vm-report.json
```

The runner creates its own local Git identity and isolated user directories. It
must not write to the operator's normal CW or Codex configuration. Preserve the
sanitized JSON report with the source commit under test.

## Installer lifecycle

Run `.\install.ps1` again to verify same-version idempotency. For update and
rollback transaction fixtures:

```powershell
python -m unittest tests.test_update
```

Verify that checksum mismatch, corrupt package, smoke-test failure, and failed
rollback leave the previously active runtime usable. Do not point deterministic
tests at production release infrastructure.

## Interrupt and recovery

Run a controlled long fake-Codex scenario from the platform test suite, press
Ctrl+C in the foreground CW process, and verify:

- CW exits with the documented interrupt status;
- the managed child/process group stops;
- no partial approval gate exists;
- `cw error`, `cw doctor`, `cw repair`, and `cw retry` describe a recoverable
  current phase;
- a second mutating CW operation is rejected while the first owns the lock;
- a genuinely stale lock is recovered only after its owner is proven dead.

## Optional real Codex acceptance

Authenticate Codex using its supported tooling, then run the disposable real
workflow recorder without changing the committed public artifact:

```powershell
$cw = (Get-Command cw).Source
python scripts/record_hero_demo.py --yes --cw-executable $cw --output $env:TEMP\cw-real-acceptance.json
```

Record `NOT CONFIGURED` if service credentials are unavailable. Never substitute
the fake executable and label the result real.

## Manual checklist

Record exactly `PASS`, `FAIL`, or `NOT TESTED` for every row.

| Check | Result | Evidence / notes |
| --- | --- | --- |
| PowerShell installation works | NOT TESTED | |
| Administrator is not required | NOT TESTED | |
| User PATH persists without duplicates | NOT TESTED | |
| New terminal finds `cw` | NOT TESTED | |
| `cw version --verbose` works | NOT TESTED | |
| `cw doctor` works | NOT TESTED | |
| Unicode paths work | NOT TESTED | |
| Paths containing spaces work | NOT TESTED | |
| `cw init` works | NOT TESTED | |
| Planner subprocess works | NOT TESTED | |
| Implementation works | NOT TESTED | |
| Deterministic validation works | NOT TESTED | |
| Independent reviewer works | NOT TESTED | |
| Gate verifies | NOT TESTED | |
| Final completion has no current phase | NOT TESTED | |
| Ctrl+C is safe | NOT TESTED | |
| Retry preserves semantic attempts | NOT TESTED | |
| Repair preserves valid gates | NOT TESTED | |
| Update succeeds atomically | NOT TESTED | |
| Failed update preserves current runtime | NOT TESTED | |
| Rollback restores previous healthy runtime | NOT TESTED | |
| Reinstall is idempotent | NOT TESTED | |

Any gate-safety, installation, state-corruption, or active-runtime destruction
failure is a release blocker. Keep Windows **experimental** until required rows
and hosted acceptance pass for the release candidate.
