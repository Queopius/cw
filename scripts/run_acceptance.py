#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import venv
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cw.core.platform import popen_process_group_kwargs, process_is_alive
from cw.core.diagnostics import redact


FAKE_CODEX = ROOT / "tests/fixtures/fake_codex/fake_codex.py"
STATUSES = {"PASS", "FAIL", "SKIPPED", "NOT_CONFIGURED"}
REPORT_KEYS = {
    "schema_version", "cw_version", "source_commit", "generated_at", "os",
    "os_version", "architecture", "python_version", "install_method", "tests", "delegated",
}


class AcceptanceFailure(RuntimeError):
    pass


def _sanitize_detail(value: str, *, private_roots: tuple[Path, ...] = ()) -> str:
    """Preserve actionable failure evidence without publishing host identity."""

    clean = redact(value) or ""
    for root in sorted({str(path) for path in private_roots if str(path)}, key=len, reverse=True):
        clean = re.sub(re.escape(root), "<PRIVATE_ROOT>", clean, flags=re.IGNORECASE)
        clean = re.sub(re.escape(root.replace("/", "\\")), "<PRIVATE_ROOT>", clean, flags=re.IGNORECASE)
    clean = re.sub(
        r"(?i)(?:\b[A-Z]:)?[\\/]+(?:Users|Documents and Settings)[\\/]+[^\\/\r\n]+",
        "~",
        clean,
    )
    clean = re.sub(r"/(?:home|Users)/[^/\s\"']+", "~", clean)
    clean = re.sub(
        r"(?i)authorization\s*:\s*(?:bearer|basic)\s+\S+",
        "[REDACTED CREDENTIAL]",
        clean,
    )
    clean = re.sub(
        r"(?i)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|password|secret)"
        r"\s*[=:]\s*\S+",
        "[REDACTED CREDENTIAL]",
        clean,
    )
    return clean


def _run(
    command: list[str], *, cwd: Path, environment: dict[str, str],
    expected: set[int] = frozenset({0}), timeout: int = 180,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command, cwd=cwd, env=environment, text=True, encoding="utf-8",
        errors="replace", capture_output=True, timeout=timeout, check=False,
    )
    if completed.returncode not in expected:
        executable = Path(command[0]).name
        raise AcceptanceFailure(
            f"{executable} exited {completed.returncode}\n"
            f"STDOUT\n{completed.stdout[-4000:]}\nSTDERR\n{completed.stderr[-4000:]}"
        )
    return completed


def _python_bin(venv_root: Path) -> Path:
    return venv_root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _cw_bin(venv_root: Path) -> Path:
    return venv_root / ("Scripts/cw.exe" if os.name == "nt" else "bin/cw")


def _venv_bin(venv_root: Path) -> Path:
    return venv_root / ("Scripts" if os.name == "nt" else "bin")


def _install_wheel(base: Path, environment: dict[str, str]) -> tuple[Path, Path]:
    wheelhouse = base / "wheelhouse"
    wheelhouse.mkdir()
    build_tools = base / "build-tools"
    venv.EnvBuilder(with_pip=True, clear=True).create(build_tools)
    build_python = _python_bin(build_tools)
    _run(
        [
            str(build_python), "-m", "pip", "install", "--disable-pip-version-check",
            "build==1.3.0", "setuptools==80.9.0", "wheel==0.45.1",
        ],
        cwd=base, environment=environment, timeout=300,
    )
    _run(
        [str(build_python), "-m", "build", "--wheel", "--no-isolation", "--outdir", str(wheelhouse)],
        cwd=ROOT, environment=environment, timeout=300,
    )
    wheels = sorted(wheelhouse.glob("*.whl"))
    if len(wheels) != 1:
        raise AcceptanceFailure(f"expected one wheel, found {len(wheels)}")
    runtime = base / "runtime"
    venv.EnvBuilder(with_pip=True, clear=True).create(runtime)
    python = _python_bin(runtime)
    _run(
        [str(python), "-m", "pip", "install", "--no-deps", str(wheels[0])],
        cwd=base, environment=environment, timeout=300,
    )
    return runtime, wheels[0]


def _install_fake_codex(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        launcher = directory / "codex.cmd"
        launcher.write_text(
            f'@echo off\r\n"{sys.executable}" "{FAKE_CODEX}" %*\r\n', encoding="utf-8",
        )
    else:
        launcher = directory / "codex"
        launcher.write_text(
            f'#!/usr/bin/env sh\nexec "{sys.executable}" "{FAKE_CODEX}" "$@"\n', encoding="utf-8",
        )
        launcher.chmod(0o755)
    return launcher


def _environment(base: Path, runtime: Path, fake_bin: Path) -> dict[str, str]:
    inherited = {
        key: value for key, value in os.environ.items()
        if key in {
            "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC",
            "LANG", "LC_ALL", "TERM", "TMP", "TEMP", "TMPDIR",
        }
    }
    home = base / "isolated home"
    local = base / "isolated appdata"
    config = base / "isolated config"
    for path in (home, local, config):
        path.mkdir(parents=True, exist_ok=True)
    inherited.update({
        "HOME": str(home),
        "USERPROFILE": str(home),
        "LOCALAPPDATA": str(local),
        "APPDATA": str(local / "Roaming"),
        "XDG_CONFIG_HOME": str(config),
        "XDG_DATA_HOME": str(base / "isolated data"),
        "CW_NO_UPDATE_CHECK": "1",
        "NO_COLOR": "1",
        "CI": "1",
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "PATH": os.pathsep.join((str(fake_bin), str(_venv_bin(runtime)), inherited.get("PATH", ""))),
    })
    inherited.pop("PYTHONPATH", None)
    inherited.pop("CODEX_HOME", None)
    return inherited


def _repository(base: Path, name: str, environment: dict[str, str]) -> Path:
    root = base / "CW Acceptance" / "Projeto São Paulo" / name
    root.mkdir(parents=True)
    _run(["git", "init", "--initial-branch=acceptance"], cwd=root, environment=environment)
    _run(["git", "config", "--local", "user.name", "CW Acceptance"], cwd=root, environment=environment)
    _run(["git", "config", "--local", "user.email", "acceptance@example.invalid"], cwd=root, environment=environment)
    (root / "README.md").write_text(
        "# Deterministic CW acceptance\n\nDisposable cross-platform fixture.\n", encoding="utf-8", newline="\n",
    )
    _run(["git", "add", "README.md"], cwd=root, environment=environment)
    _run(["git", "commit", "-m", "test: initial fixture"], cwd=root, environment=environment)
    return root


def _prepare_plan(cw: Path, root: Path, environment: dict[str, str], phases: int) -> None:
    environment["CW_FAKE_CODEX_PHASES"] = str(phases)
    _run([str(cw), "init", "--json"], cwd=root, environment=environment)
    _run([
        str(cw), "plan", "--goal", "Implement greeting behavior for José", "--json",
    ], cwd=root, environment=environment)
    _run([str(cw), "plan", "show", "--json"], cwd=root, environment=environment)
    _run([str(cw), "plan", "approve", "--json"], cwd=root, environment=environment)


def _state(root: Path) -> dict[str, Any]:
    return json.loads((root / ".cw/state.json").read_text(encoding="utf-8"))


def _single_phase(cw: Path, base: Path, environment: dict[str, str]) -> tuple[Path, str]:
    root = _repository(base, "single phase", environment)
    _prepare_plan(cw, root, environment, 1)
    environment["CW_FAKE_CODEX_SCENARIO"] = "success"
    _run([str(cw)], cwd=root, environment=environment)
    state = _state(root)
    if state.get("status") != "COMPLETED" or state.get("current_phase") is not None:
        raise AcceptanceFailure(f"single-phase state is not complete: {state}")
    gates = sorted((root / ".cw/gates").glob("*.approved.json"))
    if len(gates) != 1:
        raise AcceptanceFailure(f"single-phase gate count is {len(gates)}")
    status = json.loads(_run([str(cw), "status", "--json"], cwd=root, environment=environment).stdout)
    if status.get("state") != "COMPLETED":
        raise AcceptanceFailure("cw status did not derive COMPLETED")
    _run([str(cw), "history", "--json"], cwd=root, environment=environment)
    inspected = json.loads(_run([str(cw), "inspect", "run", "--json"], cwd=root, environment=environment).stdout)
    run_id = str(inspected["run"]["run_id"])
    _run([str(cw), "logs", "--run", run_id, "--json"], cwd=root, environment=environment)
    _run([str(cw), "doctor", "--json"], cwd=root, environment=environment)
    return root, run_id


def _multi_phase(cw: Path, base: Path, environment: dict[str, str]) -> None:
    root = _repository(base, "multi phase", environment)
    _prepare_plan(cw, root, environment, 3)
    environment["CW_FAKE_CODEX_SCENARIO"] = "success"
    _run(
        [str(cw), "run", "3", "--yes", "--non-interactive", "--no-color"],
        cwd=root, environment=environment, timeout=240,
    )
    state = _state(root)
    gates = sorted((root / ".cw/gates").glob("*.approved.json"))
    if state.get("status") != "COMPLETED" or state.get("current_phase") is not None or len(gates) != 3:
        raise AcceptanceFailure("multi-phase workflow did not complete its contiguous gate chain")
    before = len(list((root / ".cw/logs/runs").glob("*.json")))
    _run([str(cw)], cwd=root, environment=environment)
    after = len(list((root / ".cw/logs/runs").glob("*.json")))
    if before != after:
        raise AcceptanceFailure("completed workflow launched another implementation run")

    bounded = _repository(base, "multi phase until", environment)
    _prepare_plan(cw, bounded, environment, 3)
    _run(
        [str(cw), "run", "--until", "02-acceptance-2", "--yes", "--non-interactive", "--no-color"],
        cwd=bounded, environment=environment, timeout=240,
    )
    bounded_state = _state(bounded)
    bounded_gates = sorted((bounded / ".cw/gates").glob("*.approved.json"))
    if bounded_state.get("current_phase") != "03-acceptance-3" or len(bounded_gates) != 2:
        raise AcceptanceFailure("cw run --until did not stop at the requested verified gate")


def _recovery(cw: Path, root: Path, environment: dict[str, str]) -> None:
    gate = next((root / ".cw/gates").glob("*.approved.json"))
    gate_before = gate.read_bytes()
    state = _state(root)
    phase = json.loads((root / ".codex/workflow/phases.yaml").read_text(encoding="utf-8"))["phases"][0]["id"]
    state.update({"status": "IN_PROGRESS", "current_phase": phase, "last_gate": None, "attempt": 2})
    (root / ".cw/state.json").write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    _run([str(cw), "status", "--json"], cwd=root, environment=environment, expected={1})
    _run([str(cw), "repair", "--json"], cwd=root, environment=environment)
    repaired = _state(root)
    if repaired.get("status") != "COMPLETED" or repaired.get("current_phase") is not None:
        raise AcceptanceFailure("repair did not reconcile all-approved workflow to completion")
    if gate.read_bytes() != gate_before:
        raise AcceptanceFailure("repair modified a valid approval gate")
    gate_payload = json.loads(gate.read_text(encoding="utf-8"))
    gate_payload["artifact_hashes"][next(iter(gate_payload["artifact_hashes"]))] = "sha256:" + "0" * 64
    gate.write_text(json.dumps(gate_payload, indent=2) + "\n", encoding="utf-8")
    _run([str(cw), "status", "--json"], cwd=root, environment=environment, expected={1})


def _review_failures(cw: Path, base: Path, environment: dict[str, str]) -> None:
    revision = _repository(base, "semantic revision", environment)
    _prepare_plan(cw, revision, environment, 1)
    environment["CW_FAKE_CODEX_SCENARIO"] = "semantic_revision"
    _run([str(cw)], cwd=revision, environment=environment)
    revised = _state(revision)
    if revised.get("status") != "REVISION_REQUIRED" or revised.get("attempt") != 1:
        raise AcceptanceFailure("semantic REVISE did not consume exactly one attempt")
    infrastructure = _repository(base, "review transport failure", environment)
    _prepare_plan(cw, infrastructure, environment, 1)
    environment["CW_FAKE_CODEX_SCENARIO"] = "reviewer_infrastructure_failure"
    _run([str(cw)], cwd=infrastructure, environment=environment, expected={1})
    failed = _state(infrastructure)
    if failed.get("status") != "ERROR" or failed.get("attempt") != 0:
        raise AcceptanceFailure("reviewer infrastructure failure consumed a semantic attempt")
    if not (infrastructure / ".cw/runtime/READY_FOR_REVIEW.json").is_file():
        raise AcceptanceFailure("reviewer infrastructure failure did not preserve readiness")
    environment["CW_FAKE_CODEX_SCENARIO"] = "success"
    _run([str(cw), "retry", "--json"], cwd=infrastructure, environment=environment)
    recovered = _state(infrastructure)
    if recovered.get("status") != "COMPLETED" or recovered.get("attempt") != 0:
        raise AcceptanceFailure("cw retry did not resume the preserved reviewer boundary")


def _interrupt(cw: Path, base: Path, environment: dict[str, str]) -> None:
    root = _repository(base, "interrupt recovery", environment)
    _prepare_plan(cw, root, environment, 1)
    interrupted_environment = {**environment, "CW_FAKE_CODEX_SCENARIO": "implementer_timeout"}
    process = subprocess.Popen(
        [str(cw)], cwd=root, env=interrupted_environment,
        text=True, encoding="utf-8", errors="replace", stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        **popen_process_group_kwargs(),
    )
    active_path = root / ".cw/runtime/active-run.json"
    child_pid = 0
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and process.poll() is None:
            try:
                child_pid = int(json.loads(active_path.read_text(encoding="utf-8")).get("process_pid") or 0)
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                child_pid = 0
            if child_pid and process_is_alive(child_pid):
                break
            time.sleep(0.1)
        if not child_pid or not process_is_alive(child_pid):
            if process.poll() is None:
                process.kill()
            stdout, stderr = process.communicate(timeout=5)
            raise AcceptanceFailure(
                f"interrupt fixture did not start its managed child\n{stdout[-1000:]}\n{stderr[-1000:]}"
            )
        if os.name == "nt":
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(process.pid, signal.SIGINT)
        stdout, stderr = process.communicate(timeout=20)
        if process.returncode != 130:
            raise AcceptanceFailure(
                f"interrupted CW exited {process.returncode}, expected 130\n{stdout[-1000:]}\n{stderr[-1000:]}"
            )
        child_deadline = time.monotonic() + 5
        while process_is_alive(child_pid) and time.monotonic() < child_deadline:
            time.sleep(0.05)
        if process_is_alive(child_pid):
            raise AcceptanceFailure("interrupted CW left its managed Codex child running")
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
        if child_pid and process_is_alive(child_pid):
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(child_pid), "/T", "/F"],
                    text=True, capture_output=True, timeout=10, check=False,
                )
            else:
                try:
                    os.kill(child_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
    if list((root / ".cw/gates").glob("*.approved.json")):
        raise AcceptanceFailure("interrupted CW created a partial approval gate")
    recovered_environment = {**environment, "CW_FAKE_CODEX_SCENARIO": "success"}
    _run([str(cw), "retry", "--json"], cwd=root, environment=recovered_environment)
    if _state(root).get("status") != "COMPLETED":
        raise AcceptanceFailure("interrupted workflow was not recoverable through cw retry")


def _result(status: str, detail: str = "") -> dict[str, str]:
    if status not in STATUSES:
        raise ValueError(status)
    return {"status": status, "detail": detail}


def _source_commit() -> str:
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, encoding="utf-8",
        errors="replace", capture_output=True, check=False,
    ).stdout.strip() or "unknown"
    dirty = subprocess.run(
        ["git", "status", "--porcelain"], cwd=ROOT, text=True, encoding="utf-8",
        errors="replace", capture_output=True, check=False,
    )
    return f"{commit}-dirty" if dirty.returncode == 0 and dirty.stdout.strip() else commit


def _validate_report(report: dict[str, Any]) -> None:
    if set(report) != REPORT_KEYS or report.get("schema_version") != 1:
        raise AcceptanceFailure("compatibility report has an invalid top-level contract")
    tests = report.get("tests")
    if not isinstance(tests, dict) or not tests:
        raise AcceptanceFailure("compatibility report has no test evidence")
    for name, result in tests.items():
        if not isinstance(name, str) or not isinstance(result, dict) or set(result) != {"status", "detail"}:
            raise AcceptanceFailure("compatibility report contains malformed test evidence")
        if result["status"] not in STATUSES or not isinstance(result["detail"], str):
            raise AcceptanceFailure("compatibility report contains an invalid evidence status")
    serialized = json.dumps(report, ensure_ascii=False)
    forbidden = ("authorization:", "bearer ", "codex_access_token", "api_key", "\\users\\")
    if any(value in serialized.lower() for value in forbidden):
        raise AcceptanceFailure("compatibility report contains forbidden private data")


def run_acceptance(output: Path) -> tuple[dict[str, Any], int]:
    source_version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    tests: dict[str, dict[str, str]] = {}
    exit_code = 0
    with tempfile.TemporaryDirectory(prefix="cw-acceptance-") as temporary:
        base = Path(temporary)
        bootstrap = os.environ.copy()
        bootstrap.pop("PYTHONPATH", None)
        try:
            runtime, wheel = _install_wheel(base, bootstrap)
            tests["package_install"] = _result("PASS", wheel.name)
            fake_bin = base / "fake codex bin"
            _install_fake_codex(fake_bin)
            environment = _environment(base, runtime, fake_bin)
            cw = _cw_bin(runtime)
            version = json.loads(_run([str(cw), "version", "--json"], cwd=base, environment=environment).stdout)
            if version.get("version") != source_version:
                raise AcceptanceFailure(
                    f"source/install version mismatch: {source_version} != {version.get('version')}"
                )
            tests["cli_smoke"] = _result("PASS", f"installed CW {source_version}")
            installed_python = _python_bin(runtime)
            _run(
                [
                    str(installed_python), "-c",
                    "import importlib.util, sys; "
                    "import cw.core, cw.application, cw.adapters.mcp.runtime; "
                    "assert importlib.util.find_spec('mcp') is None; "
                    "assert 'mcp' not in sys.modules",
                ],
                cwd=base, environment=environment,
            )
            tests["mcp_package"] = _result(
                "PASS", "wheel includes MCP adapter; core and CLI need no MCP extra",
            )
            root, _ = _single_phase(cw, base, environment)
            tests["deterministic_e2e"] = _result("PASS", "external installed CLI; one verified gate")
            _multi_phase(cw, base, environment)
            tests["multi_phase"] = _result("PASS", "run N and --until preserve ordered contiguous gates")
            _recovery(cw, root, environment)
            tests["failure_recovery"] = _result("PASS", "stale state repaired; invalid gate rejected")
            _review_failures(cw, base, environment)
            tests["review_semantics"] = _result("PASS", "REVISE vs infrastructure failure preserved")
            _interrupt(cw, base, environment)
            tests["interrupts"] = _result("PASS", "foreground interrupt stopped child; retry completed")
            _run(
                [
                    sys.executable, "-m", "unittest",
                    "tests.test_fake_codex",
                    "tests.test_platform.NativeProcessTests",
                    "tests.test_platform.AtomicAndEncodingTests",
                ],
                cwd=ROOT, environment=bootstrap, timeout=120,
            )
            tests["fake_codex"] = _result("PASS", "success, failure and malformed external contracts detected")
            tests["termination"] = _result("PASS", f"native {platform.system()} process-group termination")
            tests["filesystem"] = _result("PASS", "UTF-8, CRLF, atomic replacement, spaces and Unicode")
            _run(
                [
                    sys.executable, "-m", "unittest",
                    "tests.test_persistence", "tests.test_sessions_and_hooks", "tests.test_integrations",
                ],
                cwd=ROOT, environment=bootstrap, timeout=120,
            )
            tests["locking"] = _result("PASS", "external concurrency rejection and stale-owner recovery")
            tests["session_recovery"] = _result("PASS", "stale session and readiness evidence preserved")
            tests["integrations"] = _result("PASS", "optional failure continues; required failure blocks")
            _run(
                [
                    sys.executable, "-m", "unittest",
                    "tests.test_update.UpdateServiceTests.test_valid_staged_install_and_atomic_switch",
                    "tests.test_update.UpdateServiceTests.test_checksum_mismatch_preserves_current",
                    "tests.test_update.UpdateServiceTests.test_smoke_failure_preserves_current",
                    "tests.test_update.UpdateServiceTests.test_rollback_success",
                    "tests.test_update.UpdateServiceTests.test_rollback_smoke_failure_preserves_new_version",
                ],
                cwd=ROOT, environment=bootstrap, timeout=120,
            )
            tests["update"] = _result("PASS", "staged activation, checksum and smoke failures")
            tests["rollback"] = _result("PASS", "manual and failed rollback preserve healthy runtime")
        except (AcceptanceFailure, OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
            exit_code = 1
            tests.setdefault("acceptance", _result(
                "FAIL",
                _sanitize_detail(str(exc), private_roots=(base, ROOT, Path.home())),
            ))
    tests.setdefault("interrupts", _result("SKIPPED", "native platform process suite did not complete"))
    tests.setdefault("update", _result("SKIPPED", "deterministic update transaction suite did not complete"))
    tests.setdefault("rollback", _result("SKIPPED", "deterministic rollback suite did not complete"))
    tests["real_codex"] = _result("NOT_CONFIGURED", "manual authenticated workflow only")
    report = {
        "schema_version": 1,
        "cw_version": source_version,
        "source_commit": _source_commit(),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "os": platform.system(),
        "os_version": platform.release(),
        "architecture": platform.machine(),
        "python_version": platform.python_version(),
        "install_method": "built-wheel-clean-venv",
        "tests": tests,
        "delegated": {
            "windows": "CI_REQUIRED" if os.name != "nt" else "CURRENT_HOST",
            "macos": "CI_REQUIRED" if platform.system() != "Darwin" else "CURRENT_HOST",
            "real_codex": "MANUAL_NOT_CONFIGURED",
        },
    }
    _validate_report(report)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary_output = output.with_suffix(output.suffix + ".tmp")
    temporary_output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary_output, output)
    return report, exit_code


def main() -> int:
    parser = argparse.ArgumentParser(description="Run deterministic CW acceptance on the current host")
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/compatibility-report.json")
    args = parser.parse_args()
    report, exit_code = run_acceptance(args.output.resolve())
    print("CW LOCAL ACCEPTANCE")
    print("===================")
    print(f"Version                 {report['cw_version']}")
    print(f"Host                    {report['os']} {report['architecture']}")
    for name, result in report["tests"].items():
        print(f"{name.replace('_', ' ').title():<24}{result['status']}")
    if report["delegated"]["windows"] == "CI_REQUIRED":
        print("Windows                 CI REQUIRED")
    if report["delegated"]["macos"] == "CI_REQUIRED":
        print("macOS                   CI REQUIRED")
    print(f"Report                  {args.output.resolve()}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
