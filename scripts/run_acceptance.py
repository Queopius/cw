#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import contextvars
import hashlib
import hmac
import json
import os
import platform
import re
import signal
import stat
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

from cw.core.diagnostics import redact
from cw.core.platform import popen_process_group_kwargs, process_is_alive

FAKE_CODEX = ROOT / "tests/fixtures/fake_codex/fake_codex.py"
STATUSES = {"PASS", "FAIL", "SKIPPED", "NOT_CONFIGURED"}
REPORT_KEYS = {
    "schema_version", "cw_version", "source_commit", "generated_at", "os",
    "os_version", "architecture", "python_version", "install_method", "tests", "delegated",
}

_OPERATION_STAGE: contextvars.ContextVar[str] = contextvars.ContextVar(
    "acceptance_operation_stage", default="acceptance.operation.setup",
)


class AcceptanceFailure(RuntimeError):
    """A failed acceptance operation with safe metadata for the failure artifact."""

    def __init__(
        self,
        message: str,
        *,
        stage: str | None = None,
        executable: str = "unknown",
        command_name: str = "unknown",
        exit_code: int | None = None,
        executable_path: str | None = None,
        cwd: Path | None = None,
        environment: dict[str, str] | None = None,
        timed_out: bool = False,
        envelope_code: str | None = None,
        envelope_correlation: str | None = None,
        error_fingerprint: str | None = None,
    ) -> None:
        super().__init__(message)
        self.stage = stage or _OPERATION_STAGE.get()
        self.executable = executable
        self.command_name = command_name
        self.exit_code = exit_code
        self.executable_path = executable_path
        self.cwd = cwd
        self.environment = environment or {}
        self.timed_out = timed_out
        self.envelope_code = envelope_code
        self.envelope_correlation = envelope_correlation
        self.error_fingerprint = error_fingerprint


def _sanitize_detail(value: str, *, private_roots: tuple[Path, ...] = ()) -> str:
    """Preserve actionable failure evidence without publishing host identity."""

    clean = redact(value) or ""
    for root in sorted({str(path) for path in private_roots if str(path)}, key=len, reverse=True):
        clean = re.sub(re.escape(root), "<PRIVATE_ROOT>", clean, flags=re.IGNORECASE)
        clean = re.sub(re.escape(root.replace("/", "\\")), "<PRIVATE_ROOT>", clean, flags=re.IGNORECASE)
    clean = re.sub(
        r"(?i)\b[A-Z]:\\(?:Users|Documents and Settings)\\[^\r\n\"']+",
        "<PRIVATE_PATH>",
        clean,
    )
    clean = re.sub(
        r"(?i)(?:\b[A-Z]:)?[\\/]+(?:Users|Documents and Settings)[\\/]+[^\\/\r\n]+",
        "~",
        clean,
    )
    clean = re.sub(r"/(?:home|Users)/[^/\s\"']+", "~", clean)
    clean = re.sub(r"(?i)\b[A-Z]:\\[^\s\"']+", "<PRIVATE_PATH>", clean)
    clean = re.sub(r"(?i)\b[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}\b", "[REDACTED EMAIL]", clean)
    clean = re.sub(
        r"(?i)authorization\s*:\s*(?:bearer|basic)\s+\S+",
        "[REDACTED CREDENTIAL]",
        clean,
    )
    clean = re.sub(r"(?i)\b(?:bearer|basic)\s+\S+", "[REDACTED CREDENTIAL]", clean)
    clean = re.sub(
        r"(?i)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|token|password|secret)"
        r"\s*[=:]\s*\S+",
        "[REDACTED CREDENTIAL]",
        clean,
    )
    return clean


_MAX_DIAGNOSTIC_BYTES = 64 * 1024
_SAFE_STAGE = re.compile(r"^[a-z][a-z0-9_.-]{0,80}$")
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,120}$")
_SAFE_CORRELATION_ID = re.compile(r"^[0-9a-f]{16}$")
_SAFE_MESSAGES = {"Unexpected internal failure", "Verification Executor failed"}
_REDACTED_MESSAGE = "Internal exception captured; message redacted."


@contextlib.contextmanager
def _operation_stage(stage: str):
    """Attribute expected harness failures to one stable public substage."""

    if not (_SAFE_STAGE.fullmatch(stage) and stage.startswith(("acceptance.operation.", "interrupt."))):
        raise ValueError("invalid acceptance operation stage")
    token = _OPERATION_STAGE.set(stage)
    try:
        yield
    finally:
        _OPERATION_STAGE.reset(token)


def _canonical_root(root: Path) -> Path:
    """Return the existing runtime root in its host canonical spelling."""
    resolved = root.resolve(strict=True)
    if os.name != "nt":
        return resolved
    try:
        import ctypes

        windll = getattr(ctypes, "windll", None)
        kernel32 = getattr(windll, "kernel32", None)
        get_long_path = getattr(kernel32, "GetLongPathNameW", None)
        if get_long_path is None:
            return resolved
        size = get_long_path(str(resolved), None, 0)
        if size:
            buffer = ctypes.create_unicode_buffer(size + 1)
            if get_long_path(str(resolved), buffer, len(buffer)):
                long_path = Path(buffer.value)
                if os.path.samefile(resolved, long_path):
                    return long_path.resolve(strict=True)
    except (AttributeError, OSError):
        pass
    return resolved


def _safe_message(value: Any) -> str:
    """Free text is never exported; only fixed diagnostic messages are allowed."""
    return value if isinstance(value, str) and value in _SAFE_MESSAGES else _REDACTED_MESSAGE


def _safe_regular_text(path: Path, *, maximum: int = _MAX_DIAGNOSTIC_BYTES) -> str | None:
    """Read a small, regular, single-link file without following symlinks."""
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > maximum:
            return None
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or opened.st_size > maximum:
                return None
            return os.read(descriptor, maximum + 1).decode("utf-8")
        finally:
            os.close(descriptor)
    except (OSError, UnicodeDecodeError):
        return None


def _safe_json(path: Path) -> dict[str, Any] | None:
    text = _safe_regular_text(path)
    if text is None:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _safe_fingerprint(path: Path) -> str | None:
    text = _safe_regular_text(path)
    return hashlib.sha256(text.encode("utf-8")).hexdigest() if text is not None else None


def _failure_envelope(stdout: str) -> tuple[str | None, str | None]:
    """Read only code and correlation from one in-memory CW JSON envelope."""
    payload = _json_payload(stdout)
    if not isinstance(payload, dict):
        return None, None
    code = payload.get("code")
    correlation = _correlation_id(payload)
    return (code if isinstance(code, str) and _SAFE_IDENTIFIER.fullmatch(code) else None, correlation)


def _json_payload(text: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    if isinstance(payload.get("error"), dict):
        return payload["error"]
    return payload.get("data") if isinstance(payload.get("data"), dict) else payload


def _correlation_id(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    for key in ("correlation_id", "correlationId"):
        candidate = value.get(key)
        if isinstance(candidate, str) and _SAFE_CORRELATION_ID.fullmatch(candidate):
            return candidate
    for key in ("error", "data"):
        nested = value.get(key)
        if isinstance(nested, dict) and (correlation := _correlation_id(nested)) is not None:
            return correlation
    return None


def _relative_cw_path(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.replace("\\", "/")
    if "/cw/" not in normalized:
        return None
    candidate = "cw/" + normalized.rsplit("/cw/", 1)[1]
    if ".." in candidate.split("/") or not candidate.endswith(".py"):
        return None
    return candidate if _SAFE_IDENTIFIER.fullmatch(candidate.replace("/", ".")) else None


def _safe_frame(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    module = _relative_cw_path(value.get("path") or value.get("file") or value.get("filename"))
    function = value.get("function")
    line = value.get("line") or value.get("lineno")
    exception_type = value.get("exception_type") or value.get("type")
    if not (module and isinstance(function, str) and _SAFE_IDENTIFIER.fullmatch(function)
            and isinstance(line, int) and line >= 0 and isinstance(exception_type, str)
            and _SAFE_IDENTIFIER.fullmatch(exception_type)):
        return None
    return {"module": module, "function": function, "line": line,
            "exception_type": exception_type, "message": _safe_message(value.get("message"))}


def _text_traceback_frames(value: Any, exception_type: str | None) -> list[dict[str, Any]]:
    """Parse Python traceback metadata only; source and exception text never escape."""
    if not isinstance(value, str):
        return []
    frames: list[dict[str, Any]] = []
    pattern = re.compile(r'^\s*File "(?P<path>[^"]+)", line (?P<line>\d+), in (?P<function>[A-Za-z_][A-Za-z0-9_]*)\s*$')
    safe_type = exception_type if isinstance(exception_type, str) and _SAFE_IDENTIFIER.fullmatch(exception_type) else None
    if safe_type is None:
        type_pattern = re.compile(r"^\s*(?P<type>[A-Za-z_][A-Za-z0-9_]*(?:Error|Exception|Exit|Interrupt))(?::|$)")
        for line in reversed(value.splitlines()):
            match = type_pattern.match(line)
            if match is not None:
                safe_type = match.group("type")
                break
    safe_type = safe_type or "Exception"
    for line in value.splitlines():
        match = pattern.match(line)
        if match is None:
            continue
        module = _relative_cw_path(match.group("path"))
        if module is not None:
            frames.append({"module": module, "function": match.group("function"), "line": int(match.group("line")), "exception_type": safe_type, "message": _REDACTED_MESSAGE})
    return frames[-12:]


def _record_diagnostic(record: dict[str, Any], correlation: str, command: str) -> dict[str, Any] | None:
    nested_payload = record.get("data")
    payload: dict[str, Any] = nested_payload if isinstance(nested_payload, dict) else record
    nested_error = payload.get("error")
    error: dict[str, Any] = nested_error if isinstance(nested_error, dict) else payload
    code = error.get("code")
    message = error.get("message")
    source = record.get("source")
    if not (isinstance(code, str) and isinstance(message, str) and source == command):
        return None
    expected = hashlib.sha256(f"{command}\0{code}\0{message}".encode()).hexdigest()[:16]
    stored = _correlation_id(record)
    if not hmac.compare_digest(correlation, expected) and stored != correlation:
        return None
    structured = record.get("safe_traceback")
    if isinstance(structured, dict):
        error_type = structured.get("exception_type")
        frames = structured.get("frames")
        if structured.get("version") != 1 or not isinstance(error_type, str) or not _SAFE_IDENTIFIER.fullmatch(error_type) or not isinstance(frames, list):
            return None
        safe_frames = [
            {"module": frame.get("module"), "function": frame.get("function"), "line": frame.get("line"), "exception_type": error_type, "message": _REDACTED_MESSAGE}
            for frame in frames if isinstance(frame, dict) and isinstance(frame.get("module"), str) and frame["module"].startswith("cw.")
            and isinstance(frame.get("function"), str) and _SAFE_IDENTIFIER.fullmatch(frame["function"])
            and isinstance(frame.get("line"), int) and frame["line"] > 0
        ]
        if len(safe_frames) != len(frames) or not safe_frames:
            return None
    else:
        error_type = error.get("exception_type") or error.get("type")
        frames = error.get("traceback") or error.get("frames") or []
        safe_frames = [_safe_frame(frame) for frame in frames] if isinstance(frames, list) else _text_traceback_frames(frames, error_type)
    safe_frames = [frame for frame in safe_frames if frame is not None][:12]
    primary = safe_frames[-1] if safe_frames else None
    return {
        "cw_error_code": code if isinstance(code, str) and _SAFE_IDENTIFIER.fullmatch(code) else None,
        "correlation_id_sha256": hashlib.sha256(correlation.encode("utf-8")).hexdigest(),
        "exception_type": primary["exception_type"] if primary else error_type if isinstance(error_type, str) and _SAFE_IDENTIFIER.fullmatch(error_type) else None,
        "module": primary["module"] if primary else None,
        "function": primary["function"] if primary else None,
        "line": primary["line"] if primary else None,
        "message": _safe_message(message),
        "traceback": safe_frames,
    }


def _last_correlated_jsonl(path: Path, correlation: str) -> dict[str, Any] | None:
    text = _safe_regular_text(path)
    if text is None:
        return None
    for line in reversed(text.splitlines()):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            return None
        if isinstance(record, dict) and _correlation_id(record) == correlation:
            return record
    return None


def _capture_cw_diagnostic(failure: AcceptanceFailure) -> dict[str, Any]:
    """Capture only a correlation-bound CW record; no stale-error heuristics."""
    unavailable: dict[str, Any] = {
        "diagnostic_status": "unavailable", "diagnostic_source": "none",
        "canonical_root_available": False, "project_metadata_present": False,
        "envelope_code_present": failure.envelope_code is not None,
        "envelope_correlation_present": failure.envelope_correlation is not None,
        "last_error_changed": False, "last_error_safe_regular": False,
        "record_found": False, "correlation_match": False, "code_match": False,
        "traceback_frame_available": False, "binding_failure_reason": "project_metadata_missing",
    }
    root = failure.cwd
    if failure.executable != "cw" or root is None or not failure.executable_path:
        return unavailable
    try:
        root = _canonical_root(root)
    except OSError:
        return unavailable
    unavailable["canonical_root_available"] = True
    if not (root / ".cw").is_dir():
        return unavailable
    unavailable["project_metadata_present"] = True
    correlation = failure.envelope_correlation
    if failure.envelope_code is None:
        unavailable["binding_failure_reason"] = "envelope_missing"
        return unavailable
    if correlation is None:
        unavailable["binding_failure_reason"] = "envelope_correlation_missing"
        return unavailable
    current_fingerprint = _safe_fingerprint(root / ".cw/logs/last-error.json")
    unavailable["last_error_safe_regular"] = current_fingerprint is not None
    unavailable["last_error_changed"] = current_fingerprint is not None and current_fingerprint != failure.error_fingerprint
    if not unavailable["last_error_changed"]:
        unavailable["binding_failure_reason"] = "diagnostic_record_unchanged"
        return unavailable
    for source, record in (
        ("last_error", _safe_json(root / ".cw/logs/last-error.json")),
        ("errors_jsonl", _last_correlated_jsonl(root / ".cw/logs/errors.jsonl", correlation)),
    ):
        if record is not None:
            unavailable["record_found"] = True
            captured = _record_diagnostic(record, correlation, failure.command_name)
            if captured is None:
                unavailable["binding_failure_reason"] = "correlation_mismatch"
                continue
            unavailable["correlation_match"] = True
            if captured["cw_error_code"] != failure.envelope_code:
                unavailable["binding_failure_reason"] = "code_mismatch"
                continue
            unavailable["code_match"] = True
            unavailable["traceback_frame_available"] = bool(captured["traceback"])
            if not unavailable["traceback_frame_available"]:
                unavailable["binding_failure_reason"] = "traceback_unavailable"
                continue
            return {**unavailable, "diagnostic_status": "captured", "diagnostic_source": source, "binding_failure_reason": "none", **captured}
    if not unavailable["record_found"]:
        unavailable["binding_failure_reason"] = "diagnostic_record_missing"
    return unavailable


def _run(
    command: list[str], *, cwd: Path, environment: dict[str, str],
    expected: set[int] = frozenset({0}), timeout: int = 180,
    diagnostic_stage: str | None = None, diagnostic_executable: str = "unknown",
    diagnostic_command: str = "unknown",
) -> subprocess.CompletedProcess[str]:
    diagnostic_stage = diagnostic_stage or _OPERATION_STAGE.get()
    if not (_SAFE_STAGE.fullmatch(diagnostic_stage)
            and diagnostic_executable in {"cw", "python", "git", "unknown"}
            and _SAFE_STAGE.fullmatch(diagnostic_command)):
        raise ValueError("diagnostic operation must use declared allowlisted identifiers")
    before_error = _safe_fingerprint(cwd / ".cw/logs/last-error.json") if diagnostic_executable == "cw" else None
    try:
        completed = subprocess.run(
            command, cwd=cwd, env=environment, text=True, encoding="utf-8",
            errors="replace", capture_output=True, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AcceptanceFailure(
            "Acceptance command timed out.",
            stage=diagnostic_stage,
            executable=diagnostic_executable,
            command_name=diagnostic_command,
            exit_code=None,
            executable_path=command[0] if diagnostic_executable == "cw" and command else None,
            cwd=cwd,
            environment=environment,
            timed_out=True,
        ) from exc
    if completed.returncode not in expected:
        code, correlation = _failure_envelope(completed.stdout) if diagnostic_executable == "cw" else (None, None)
        raise AcceptanceFailure(
            "Acceptance command failed.",
            stage=diagnostic_stage,
            executable=diagnostic_executable,
            command_name=diagnostic_command,
            exit_code=completed.returncode,
            executable_path=command[0] if diagnostic_executable == "cw" and command else None,
            cwd=cwd,
            environment=environment,
            envelope_code=code,
            envelope_correlation=correlation,
            error_fingerprint=before_error,
        )
    return completed


def _python_bin(venv_root: Path) -> Path:
    return venv_root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _cw_bin(venv_root: Path) -> Path:
    return venv_root / ("Scripts/cw.exe" if os.name == "nt" else "bin/cw")


def _venv_bin(venv_root: Path) -> Path:
    return venv_root / ("Scripts" if os.name == "nt" else "bin")


def _install_wheel(base: Path, environment: dict[str, str]) -> tuple[Path, Path, Path]:
    with _operation_stage("acceptance.operation.build_tools"):
        base = _canonical_root(base)
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
    with _operation_stage("acceptance.operation.wheel_build"):
        _run(
            [str(build_python), "-m", "build", "--wheel", "--no-isolation", "--outdir", str(wheelhouse)],
            cwd=ROOT, environment=environment, timeout=300,
        )
    with _operation_stage("acceptance.operation.wheel_inventory"):
        wheels = sorted(wheelhouse.glob("*.whl"))
        if len(wheels) != 1:
            raise AcceptanceFailure(f"expected one wheel, found {len(wheels)}")
    with _operation_stage("acceptance.operation.no_deps_install"):
        no_dependencies = base / "runtime-no-deps"
        venv.EnvBuilder(with_pip=True, clear=True).create(no_dependencies)
        no_dependencies_python = _python_bin(no_dependencies)
        _run(
            [
                str(no_dependencies_python), "-m", "pip", "install", "--no-deps",
                str(wheels[0]),
            ],
            cwd=base, environment=environment, timeout=300,
        )
    with _operation_stage("acceptance.operation.no_deps_smoke"):
        _run(
            [str(_cw_bin(no_dependencies)), "version", "--json"],
            cwd=base, environment=environment,
        )
    with _operation_stage("acceptance.operation.runtime_install"):
        runtime = base / "runtime"
        venv.EnvBuilder(with_pip=True, clear=True).create(runtime)
        python = _python_bin(runtime)
        _run(
            [str(python), "-m", "pip", "install", str(wheels[0])],
            cwd=base, environment=environment, timeout=300,
        )
    return runtime, wheels[0], no_dependencies


def _install_fake_codex(directory: Path) -> Path:
    with _operation_stage("acceptance.operation.fake_codex_setup"):
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
    base = _canonical_root(base)
    runtime = _canonical_root(runtime)
    cw_executable = _cw_bin(runtime).resolve(strict=True)
    if not os.path.samefile(cw_executable.parent.parent, runtime):
        raise AcceptanceFailure("installed CW executable is outside the acceptance runtime")
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
        "CW_ACCEPTANCE_CW_EXECUTABLE": str(cw_executable),
        "CW_ACCEPTANCE_RUNTIME_ROOT": str(runtime),
        "PATH": os.pathsep.join((str(fake_bin), str(_venv_bin(runtime)), inherited.get("PATH", ""))),
    })
    inherited.pop("PYTHONPATH", None)
    inherited.pop("CODEX_HOME", None)
    return inherited


def _repository(base: Path, name: str, environment: dict[str, str]) -> Path:
    with _operation_stage("acceptance.operation.repository_setup"):
        base = _canonical_root(base)
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
    _run([str(cw), "init", "--json"], cwd=root, environment=environment,
         diagnostic_stage="plan.init", diagnostic_executable="cw", diagnostic_command="init")
    _run([
        str(cw), "plan", "--goal", "Implement greeting behavior for José", "--json",
    ], cwd=root, environment=environment,
        diagnostic_stage="plan.create", diagnostic_executable="cw", diagnostic_command="plan")
    _run([str(cw), "plan", "show", "--json"], cwd=root, environment=environment,
         diagnostic_stage="plan.show", diagnostic_executable="cw", diagnostic_command="plan")
    _run([str(cw), "plan", "approve", "--json"], cwd=root, environment=environment,
         diagnostic_stage="plan.approve", diagnostic_executable="cw", diagnostic_command="plan")


def _managed_child_is_running(pid: int, *, proc_root: Path = Path("/proc")) -> bool:
    """Treat a POSIX zombie as terminated without trusting PID existence alone."""
    if not process_is_alive(pid):
        return False
    if os.name == "nt":
        return True
    try:
        tail = (proc_root / str(pid) / "stat").read_text(encoding="utf-8").rpartition(")"
        )[2].lstrip()
    except FileNotFoundError:
        # The process may have exited after the first liveness probe.
        return process_is_alive(pid)
    except OSError:
        return True
    return not tail.startswith("Z")


def _state(root: Path) -> dict[str, Any]:
    with _operation_stage("acceptance.operation.state_load"):
        return json.loads((root / ".cw/state.json").read_text(encoding="utf-8"))


def _json_object(value: str, *, stage: str) -> dict[str, Any]:
    """Fail closed on any non-object or non-single JSON command response."""

    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise AcceptanceFailure("acceptance command returned invalid JSON", stage=stage) from exc
    if not isinstance(payload, dict):
        raise AcceptanceFailure("acceptance command returned a non-object JSON value", stage=stage)
    return payload


def _safe_fixed_file_present(root: Path, relative: Path) -> bool:
    """Check fixed acceptance evidence without following links or reading it."""

    allowed = {
        Path(".cw/completion/completion.satisfied.json"),
        Path(".cw/runtime/READY_FOR_REVIEW.json"),
    }
    if relative not in allowed:
        raise ValueError("invalid fixed acceptance evidence path")
    canonical_root = _canonical_root(root)
    candidate = canonical_root / relative
    try:
        candidate.relative_to(canonical_root)
        cursor = canonical_root
        for component in relative.parts[:-1]:
            cursor /= component
            if cursor.is_symlink():
                raise AcceptanceFailure(
                    "single-phase evidence path is not a safe regular file",
                    stage="acceptance.operation.single_state.other",
                )
        metadata = candidate.lstat()
    except FileNotFoundError:
        return False
    except (OSError, ValueError) as exc:
        raise AcceptanceFailure(
            "single-phase evidence path could not be verified",
            stage="acceptance.operation.single_state.other",
        ) from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise AcceptanceFailure(
            "single-phase evidence path is not a safe regular file",
            stage="acceptance.operation.single_state.other",
        )
    return True


def _single_state_failure_stage(root: Path, state: dict[str, Any]) -> str:
    status = state.get("status")
    current_phase = state.get("current_phase")
    prefix = "acceptance.operation.single_state."
    if status == "COMPLETED" and current_phase is not None:
        return prefix + "completed_phase_present"
    if status == "PLANNED_COMPLETE":
        present = _safe_fixed_file_present(
            root, Path(".cw/completion/completion.satisfied.json"),
        )
        return prefix + ("planned_complete_gate_present" if present else "planned_complete_gate_absent")
    if status == "IN_PROGRESS":
        present = _safe_fixed_file_present(root, Path(".cw/runtime/READY_FOR_REVIEW.json"))
        return prefix + ("in_progress_readiness_present" if present else "in_progress_readiness_absent")
    known = {
        "READY_FOR_REVIEW": "ready_for_review",
        "REVIEWING": "reviewing",
        "ERROR": "error",
    }
    suffix = known.get(status, "other") if isinstance(status, str) else "other"
    return prefix + suffix


def _single_phase_cycles() -> tuple[int | None, ...]:
    return (1, 2, 3) if os.name == "nt" else (None,)


def _single_phase(
    cw: Path,
    base: Path,
    environment: dict[str, str],
    *,
    cycle: int | None = None,
) -> tuple[Path, str]:
    if cycle is not None and cycle not in {1, 2, 3}:
        raise ValueError("invalid single-phase acceptance cycle")
    phase_environment = environment.copy()
    name = "single phase" if cycle is None else f"single phase {cycle}"
    root = _repository(base, name, phase_environment)
    _prepare_plan(cw, root, phase_environment, 1)
    phase_environment["CW_FAKE_CODEX_SCENARIO"] = "success"
    with _operation_stage("acceptance.operation.first_run"):
        _run([str(cw)], cwd=root, environment=phase_environment)
    with _operation_stage("acceptance.operation.single_state.other"):
        state = _state(root)
        if state.get("status") != "COMPLETED" or state.get("current_phase") is not None:
            raise AcceptanceFailure(
                "single-phase state contract was not satisfied",
                stage=_single_state_failure_stage(root, state),
            )
    with _operation_stage("acceptance.operation.single_gate"):
        gates = sorted((root / ".cw/gates").glob("*.approved.json"))
        if len(gates) != 1:
            raise AcceptanceFailure(f"single-phase gate count is {len(gates)}")
    status_response = _run(
        [str(cw), "status", "--json"], cwd=root, environment=phase_environment,
        diagnostic_stage="acceptance.operation.status_command", diagnostic_executable="cw",
        diagnostic_command="status",
    )
    status = _json_object(status_response.stdout, stage="acceptance.operation.status_json")
    with _operation_stage("acceptance.operation.status_contract"):
        if status.get("state") != "COMPLETED":
            raise AcceptanceFailure("cw status did not derive COMPLETED")
    _run(
        [str(cw), "history", "--json"], cwd=root, environment=phase_environment,
        diagnostic_stage="acceptance.operation.history_command", diagnostic_executable="cw",
        diagnostic_command="history",
    )
    inspected_response = _run(
        [str(cw), "inspect", "run", "--json"], cwd=root, environment=phase_environment,
        diagnostic_stage="acceptance.operation.inspect_command", diagnostic_executable="cw",
        diagnostic_command="inspect",
    )
    inspected = _json_object(inspected_response.stdout, stage="acceptance.operation.inspect_json")
    with _operation_stage("acceptance.operation.inspect_contract"):
        run = inspected.get("run")
        run_id = run.get("run_id") if isinstance(run, dict) else None
        if not isinstance(run_id, str) or not _SAFE_IDENTIFIER.fullmatch(run_id):
            raise AcceptanceFailure("cw inspect did not return a valid run identifier")
    _run(
        [str(cw), "logs", "--run", run_id, "--json"], cwd=root, environment=phase_environment,
        diagnostic_stage="acceptance.operation.logs_command", diagnostic_executable="cw",
        diagnostic_command="logs",
    )
    _run(
        [str(cw), "doctor", "--json"], cwd=root, environment=phase_environment,
        diagnostic_stage="acceptance.operation.doctor_command", diagnostic_executable="cw",
        diagnostic_command="doctor",
    )
    return root, run_id


def _multi_phase(cw: Path, base: Path, environment: dict[str, str]) -> None:
    root = _repository(base, "multi phase", environment)
    _prepare_plan(cw, root, environment, 3)
    environment["CW_FAKE_CODEX_SCENARIO"] = "success"
    with _operation_stage("acceptance.operation.second_run"):
        _run(
            [str(cw), "run", "3", "--yes", "--non-interactive", "--no-color"],
            cwd=root, environment=environment, timeout=240,
        )
    with _operation_stage("acceptance.operation.multi_state"):
        state = _state(root)
        if state.get("status") != "COMPLETED" or state.get("current_phase") is not None:
            raise AcceptanceFailure("multi-phase workflow did not complete")
    with _operation_stage("acceptance.operation.multi_gate"):
        gates = sorted((root / ".cw/gates").glob("*.approved.json"))
        if len(gates) != 3:
            raise AcceptanceFailure("multi-phase workflow did not create three gates")
    with _operation_stage("acceptance.operation.determinism"):
        before = len(list((root / ".cw/logs/runs").glob("*.json")))
        _run([str(cw)], cwd=root, environment=environment)
        after = len(list((root / ".cw/logs/runs").glob("*.json")))
        if before != after:
            raise AcceptanceFailure("completed workflow launched another implementation run")

    bounded = _repository(base, "multi phase until", environment)
    _prepare_plan(cw, bounded, environment, 3)
    with _operation_stage("acceptance.operation.second_run"):
        _run(
            [str(cw), "run", "--until", "02-acceptance-2", "--yes", "--non-interactive", "--no-color"],
            cwd=bounded, environment=environment, timeout=240,
        )
    with _operation_stage("acceptance.operation.until_state"):
        bounded_state = _state(bounded)
        if bounded_state.get("current_phase") != "03-acceptance-3":
            raise AcceptanceFailure("cw run --until did not stop at the requested phase")
    with _operation_stage("acceptance.operation.until_gate"):
        bounded_gates = sorted((bounded / ".cw/gates").glob("*.approved.json"))
        if len(bounded_gates) != 2:
            raise AcceptanceFailure("cw run --until did not create two gates")


def _recovery(cw: Path, root: Path, environment: dict[str, str]) -> None:
    with _operation_stage("acceptance.operation.recovery"):
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
    with _operation_stage("acceptance.operation.review_semantics"):
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
    try:
        with _operation_stage("interrupt.child_start"):
            process = subprocess.Popen(
                [str(cw)], cwd=root, env=interrupted_environment,
                text=True, encoding="utf-8", errors="replace", stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                **popen_process_group_kwargs(),
            )
    except OSError as exc:
        raise AcceptanceFailure(
            "interrupt fixture could not start its managed child",
            stage="interrupt.child_start",
        ) from exc
    active_path = root / ".cw/runtime/active-run.json"
    child_pid = 0
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and process.poll() is None:
            try:
                child_pid = int(json.loads(active_path.read_text(encoding="utf-8")).get("process_pid") or 0)
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                child_pid = 0
            if child_pid and _managed_child_is_running(child_pid):
                break
            time.sleep(0.1)
        if not child_pid or not _managed_child_is_running(child_pid):
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)
            raise AcceptanceFailure(
                "interrupt fixture did not start its managed child",
                stage="interrupt.child_start",
            )
        if os.name == "nt":
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(process.pid, signal.SIGINT)
        process.communicate(timeout=20)
        if process.returncode != 130:
            raise AcceptanceFailure(
                "interrupted CW did not exit with the expected signal status",
                stage="interrupt.parent_exit",
            )
        child_deadline = time.monotonic() + 5
        while _managed_child_is_running(child_pid) and time.monotonic() < child_deadline:
            time.sleep(0.05)
        if _managed_child_is_running(child_pid):
            raise AcceptanceFailure(
                "interrupted CW left its managed child active",
                stage="interrupt.child_cleanup",
            )
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
        if child_pid and _managed_child_is_running(child_pid):
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
        raise AcceptanceFailure(
            "interrupted CW created a partial approval gate",
            stage="interrupt.partial_gate",
        )
    recovered_environment = {**environment, "CW_FAKE_CODEX_SCENARIO": "success"}
    _run(
        [str(cw), "retry", "--json"], cwd=root, environment=recovered_environment,
        diagnostic_stage="interrupt.retry", diagnostic_executable="cw",
        diagnostic_command="retry",
    )
    if _state(root).get("status") != "COMPLETED":
        raise AcceptanceFailure(
            "interrupted workflow was not recoverable through cw retry",
            stage="interrupt.recovery",
        )


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


def _write_diagnostic(
    output: Path, exc: BaseException, *, base: Path, source_commit: str
) -> None:
    """Write a failure-only artifact from declared fields, never process payloads."""
    failure = exc if isinstance(exc, AcceptanceFailure) else AcceptanceFailure(
        "Acceptance harness exception.", stage="acceptance.harness",
        executable="unknown", command_name="unknown",
    )
    captured = _capture_cw_diagnostic(failure)
    diagnostic = {
        "schema": "cw.acceptance-diagnostic.v1",
        "platform": platform.system(),
        "python_version": platform.python_version(),
        "architecture": platform.machine(),
        "stage": failure.stage,
        "executable": failure.executable,
        "command": failure.command_name,
        "exit_code": failure.exit_code,
        "diagnostic_status": captured["diagnostic_status"],
        "diagnostic_source": captured["diagnostic_source"],
        "cw_error_code": captured.get("cw_error_code"),
        "correlation_id_sha256": captured.get("correlation_id_sha256"),
        "exception_type": captured.get("exception_type") or type(exc).__name__,
        "module": captured.get("module"),
        "function": captured.get("function"),
        "line": captured.get("line"),
        "message": captured.get("message", _REDACTED_MESSAGE),
        "traceback": captured.get("traceback", []),
        "next_action": "Inspect the sanitized acceptance diagnostic and repair the declared stage.",
        "redaction_status": "allowlist_only",
        **{key: captured[key] for key in (
            "canonical_root_available", "project_metadata_present", "envelope_code_present",
            "envelope_correlation_present", "last_error_changed", "last_error_safe_regular",
            "record_found", "correlation_match", "code_match", "traceback_frame_available",
            "binding_failure_reason",
        )},
    }
    serialized = json.dumps(diagnostic, ensure_ascii=False, indent=2) + "\n"
    _validate_diagnostic(diagnostic)
    destination = output.with_name("compatibility-diagnostic.json")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_output = destination.with_suffix(destination.suffix + ".tmp")
    temporary_output.write_text(serialized, encoding="utf-8")
    os.replace(temporary_output, destination)


def _validate_diagnostic(diagnostic: dict[str, Any]) -> None:
    required = {
        "schema", "platform", "python_version", "architecture", "stage", "executable",
        "command", "exit_code", "diagnostic_status", "diagnostic_source", "cw_error_code",
        "correlation_id_sha256", "exception_type", "module", "function", "line", "message",
        "traceback", "next_action", "redaction_status",
        "canonical_root_available", "project_metadata_present", "envelope_code_present",
        "envelope_correlation_present", "last_error_changed", "last_error_safe_regular",
        "record_found", "correlation_match", "code_match", "traceback_frame_available",
        "binding_failure_reason",
    }
    if set(diagnostic) != required or diagnostic["schema"] != "cw.acceptance-diagnostic.v1":
        raise AcceptanceFailure("acceptance diagnostic has an invalid contract")
    serialized = json.dumps(diagnostic, ensure_ascii=False).lower()
    forbidden = ("\\users\\", "/home/", "bearer ", "api_key", "password=", "secret=", "--goal")
    if any(value in serialized for value in forbidden):
        raise AcceptanceFailure("acceptance diagnostic contains private data")
    booleans = {
        key
        for key in required
        if key.endswith(("_present", "_available", "_changed", "_regular", "_found", "_match"))
    }
    if any(not isinstance(diagnostic[key], bool) for key in booleans):
        raise AcceptanceFailure("acceptance diagnostic has non-boolean binding metadata")
    if diagnostic["binding_failure_reason"] not in {"project_metadata_missing", "envelope_missing", "envelope_correlation_missing", "diagnostic_record_unchanged", "diagnostic_record_missing", "correlation_mismatch", "code_mismatch", "traceback_unavailable", "none"}:
        raise AcceptanceFailure("acceptance diagnostic has an invalid binding reason")


def run_acceptance(output: Path) -> tuple[dict[str, Any], int]:
    source_version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    tests: dict[str, dict[str, str]] = {}
    exit_code = 0
    with tempfile.TemporaryDirectory(prefix="cw-acceptance-") as temporary:
        base = _canonical_root(Path(temporary))
        bootstrap = os.environ.copy()
        bootstrap.pop("PYTHONPATH", None)
        try:
            runtime, wheel, no_dependencies = _install_wheel(base, bootstrap)
            tests["package_install"] = _result(
                "PASS", f"dependency-resolved {wheel.name}"
            )
            tests["package_no_deps_smoke"] = _result(
                "PASS", f"{_cw_bin(no_dependencies).name} version from --no-deps wheel"
            )
            fake_bin = base / "fake codex bin"
            _install_fake_codex(fake_bin)
            environment = _environment(base, runtime, fake_bin)
            cw = _cw_bin(runtime)
            version = json.loads(_run([str(cw), "version", "--json"], cwd=base, environment=environment).stdout)
            if version.get("version") != source_version:
                raise AcceptanceFailure(
                    f"source/install version mismatch: {source_version} != {version.get('version')}"
                )
            version_flag = _run([str(cw), "--version"], cwd=base, environment=environment).stdout.strip()
            if version_flag != f"CW {source_version}":
                raise AcceptanceFailure(
                    f"installed --version mismatch: CW {source_version} != {version_flag}"
                )
            tests["cli_smoke"] = _result("PASS", f"installed CW {source_version}; both version surfaces")
            installed_python = _python_bin(runtime)
            _run(
                [
                    str(installed_python),
                    "-c",
                    (
                        "from cw.core.workflow import workflow_document_from_text; "
                        "value=workflow_document_from_text('schema_version: 1\\nworkflow: {}\\n'); "
                        "assert value['schema_version'] == 1 and value['workflow'] == {}"
                    ),
                ],
                cwd=base,
                environment=environment,
            )
            tests["native_yaml"] = _result(
                "PASS", "installed wheel safely parses native single-document YAML"
            )
            _run(
                [
                    str(installed_python),
                    "-c",
                    (
                        "import importlib.util, sys; "
                        "import cw.core, cw.application, cw.adapters.mcp.runtime; "
                        "assert importlib.util.find_spec('mcp') is None; "
                        "assert 'mcp' not in sys.modules"
                    ),
                ],
                cwd=base, environment=environment,
            )
            tests["mcp_package"] = _result(
                "PASS", "wheel includes MCP adapter; core and CLI need no MCP extra",
            )
            single_roots = [
                _single_phase(cw, base, environment, cycle=cycle)[0]
                for cycle in _single_phase_cycles()
            ]
            root = single_roots[0]
            tests["deterministic_e2e"] = _result(
                "PASS", "external installed CLI; independent verified single-phase gates",
            )
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
            _write_diagnostic(output, exc, base=base, source_commit=_source_commit())
            tests.setdefault("acceptance", _result(
                "FAIL",
                "Acceptance failed; see the sanitized compatibility diagnostic.",
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
