from __future__ import annotations

import json
import os
import secrets
import shutil
import stat
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

from cw.core.commands import command_arguments
from cw.core.diagnostics import redact
from cw.core.errors import CwError, ErrorCode
from cw.core.gates import artifact_hashes, validate_dependencies
from cw.core.models import Phase, ValidationResult, Workflow
from cw.core.platform import popen_process_group_kwargs, stop_process_group
from cw.core.revisions import artifact_revision_metadata
from cw.core.schema import SCHEMA_VERSION
from cw.core.utils import atomic_json_new, load_json, sha256_bytes, sha256_file, utc_now
from cw.core.workflow import workflow_hash
from cw.execution.context import current_event_sink
from cw.execution.events import ExecutionEvent, ExecutionEventType

RECEIPT_SCHEMA = "cw.verification-receipt.v1"
RECEIPT_DIR = ".cw/verification-receipts"
_PRIVATE_ENV = (
    "TMPDIR", "TMP", "TEMP", "XDG_CACHE_HOME", "COMPOSER_CACHE_DIR",
    "MYPY_CACHE_DIR", "PIP_CACHE_DIR", "PYTHONPYCACHEPREFIX", "RUFF_CACHE_DIR",
)
_RECEIPT_FIELDS = {
    "schema_version", "schema", "receipt_id", "correlation_id", "created_at",
    "workflow_id", "workflow_sha256", "state_sha256_before", "plan_revision_id",
    "phase_id", "semantic_attempt", "artifact_identities", "review_paths",
    "completion_contract_sha256", "commands", "preflight", "runtime", "result",
    "receipt_sha256",
}
_COMMAND_FIELDS = {
    "index", "command", "argv", "cwd", "timeout_seconds", "exit_code",
    "duration_ms", "stdout_sha256", "stderr_sha256",
}


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _receipt_digest(payload: dict[str, Any]) -> str:
    return sha256_bytes(
        _canonical_bytes(
            {key: value for key, value in payload.items() if key != "receipt_sha256"}
        )
    )


def _safe_runtime(path: Path) -> None:
    metadata = path.lstat()
    expected_uid = getattr(os, "getuid", lambda: metadata.st_uid)()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != expected_uid
        or metadata.st_nlink not in {1, 2}
    ):
        raise CwError(
            "Verification runtime is unsafe",
            ErrorCode.VERIFICATION_INFRASTRUCTURE_ERROR,
        )
    probe = path / "preflight.tmp"
    renamed = path / "preflight.ready"
    descriptor = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, b"cw-verification-preflight\n")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(probe, renamed)
    with renamed.open("rb") as stream:
        if stream.read() != b"cw-verification-preflight\n":
            raise CwError(
                "Verification runtime preflight was not durable",
                ErrorCode.VERIFICATION_INFRASTRUCTURE_ERROR,
            )
    renamed.unlink()


def _runtime_environment(runtime: Path) -> dict[str, str]:
    allowed = {
        "PATH",
        "PATHEXT",
        "LANG",
        "LC_ALL",
        "TERM",
        "CI",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
    }
    environment = {key: value for key, value in os.environ.items() if key in allowed}
    home = runtime / "home"
    cache = runtime / "cache"
    temporary = runtime / "tmp"
    composer = cache / "composer"
    for directory in (home, cache, temporary, composer):
        directory.mkdir(mode=0o700)
        _safe_runtime(directory)
    environment["HOME"] = str(home)
    environment["USERPROFILE"] = str(home)
    values = {
        "TMPDIR": temporary,
        "TMP": temporary,
        "TEMP": temporary,
        "XDG_CACHE_HOME": cache,
        "COMPOSER_CACHE_DIR": composer,
        "MYPY_CACHE_DIR": cache / "mypy",
        "PIP_CACHE_DIR": cache / "pip",
        "PYTHONPYCACHEPREFIX": cache / "pycache",
        "RUFF_CACHE_DIR": cache / "ruff",
    }
    for path in values.values():
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    environment.update({name: str(path) for name, path in values.items()})
    return environment


def _cleanup_runtime(
    runtime: Path,
    *,
    retries: int = 3,
    timeout_seconds: float = 0.25,
    sleeper: Any = time.sleep,
    clock: Any = time.monotonic,
) -> None:
    """Remove a governed private runtime with bounded, fail-closed retries.

    This is the only deletion primitive for reviewer/verification runtimes.
    Callers must have stopped their child process groups and closed pipes before
    entering it; a failure here is infrastructure failure, never advisory
    cleanup.  The injected clock and sleeper keep Windows sharing failures
    deterministic under test.
    """

    def remove_readonly(function: Any, path: str, exception: tuple[Any, Any, Any]) -> None:
        error = exception[1]
        if not isinstance(error, PermissionError):
            raise error
        os.chmod(path, stat.S_IWRITE)
        function(path)

    delay = 0.02
    deadline = clock() + timeout_seconds
    last_error: OSError | None = None
    for attempt in range(retries):
        try:
            shutil.rmtree(runtime, onerror=remove_readonly)
            return
        except OSError as exc:
            last_error = exc
            transient = isinstance(exc, PermissionError)
            if transient and attempt + 1 < retries and clock() + delay <= deadline:
                sleeper(delay)
                delay *= 2
    raise CwError(
        "Verification runtime cleanup failed",
        ErrorCode.VERIFICATION_INFRASTRUCTURE_ERROR,
        "Run: cw retry",
        details=redact(str(last_error)) if last_error is not None else None,
    ) from last_error


def _record_runtime_cleanup_failure(result: ValidationResult, error: CwError) -> None:
    """Keep cleanup failures classified without allowing valid evidence to escape."""
    result.passed = False
    result.receipt = None
    result.receipt_payload = None
    result.error_code = error.code.value
    result.errors.append(error.message)
    result.checks.append(
        {
            "name": "Verification runtime cleanup",
            "status": "failed",
            "phase": "runtime_cleanup",
            "operation": "runtime_cleanup",
            "detail": error.message,
            "error_code": error.code.value,
            "next_action": error.hint or "Run: cw retry",
        }
    )


def _project_snapshot(
    root: Path, allowed_mutations: tuple[str, ...] = ()
) -> dict[str, tuple[str, int, str | None]]:
    excluded = {
        ".git",
        ".cw/runtime/operations",
        ".cw/runtime/verification",
        ".cw/validation",
        ".cw/verification-receipts",
        *allowed_mutations,
    }
    snapshot: dict[str, tuple[str, int, str | None]] = {}
    for path in sorted(root.rglob("*")):
        reference = path.relative_to(root).as_posix()
        if any(reference == item or reference.startswith(item + "/") for item in excluded):
            continue
        metadata = path.lstat()
        kind = "symlink" if stat.S_ISLNK(metadata.st_mode) else "directory" if stat.S_ISDIR(metadata.st_mode) else "file" if stat.S_ISREG(metadata.st_mode) else "special"
        digest = sha256_file(path) if kind == "file" else None
        snapshot[reference] = (kind, stat.S_IMODE(metadata.st_mode), digest)
    return snapshot

def _git_metadata_snapshot(root: Path) -> dict[str, tuple[str, int, str | None]]:
    """Snapshot the administrative metadata of precisely ``root``'s Git worktree."""
    try:
        canonical_root = root.resolve(strict=True)
    except OSError as exc:
        raise CwError(
            "Git repository root cannot be resolved", ErrorCode.INTEGRITY_ERROR
        ) from exc
    if not canonical_root.is_dir() or canonical_root.is_symlink():
        raise CwError("Git repository root is unsafe", ErrorCode.INTEGRITY_ERROR)
    environment = dict(os.environ)
    for name in (
        "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    ):
        environment.pop(name, None)

    def query(flag: str) -> Path:
        try:
            completed = subprocess.run(
                [
                    "git", "-C", str(canonical_root), "rev-parse",
                    "--path-format=absolute", flag,
                ],
                cwd=canonical_root,
                env=environment,
                capture_output=True,
                text=True,
                check=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise CwError(
                "Unable to resolve Git administrative directory",
                ErrorCode.VERIFICATION_INFRASTRUCTURE_ERROR,
            ) from exc
        value = completed.stdout.strip()
        if not value:
            raise CwError("Git repository identity is incoherent", ErrorCode.INTEGRITY_ERROR)
        path = Path(value)
        return path.resolve() if path.is_absolute() else (canonical_root / path).resolve()

    observed_root = query("--show-toplevel")
    if os.path.normcase(str(observed_root)) != os.path.normcase(str(canonical_root)):
        raise CwError("Git repository identity is incoherent", ErrorCode.INTEGRITY_ERROR)
    paths = [query("--git-dir"), query("--git-common-dir")]
    snap: dict[str, tuple[str, int, str | None]] = {}
    for base in dict.fromkeys(paths):
        if not base.is_dir() or base.is_symlink():
            raise CwError("Git administrative directory is unsafe", ErrorCode.INTEGRITY_ERROR)
        for path in sorted(base.rglob("*")):
            rel = path.relative_to(base).as_posix()
            if rel == "objects" or rel.startswith("objects/"):
                continue
            meta = path.lstat()
            kind = "symlink" if stat.S_ISLNK(meta.st_mode) else "directory" if stat.S_ISDIR(meta.st_mode) else "file" if stat.S_ISREG(meta.st_mode) else "special"
            snap[f"{base}:{rel}"] = (kind, stat.S_IMODE(meta.st_mode), sha256_file(path) if kind == "file" else None)
    return snap


def _validate_command_paths(root: Path, argv: list[str]) -> None:
    for raw in argv[1:]:
        value = raw.split("=", 1)[1] if raw.startswith("-") and "=" in raw else raw
        if not value or value.startswith("-"):
            continue
        candidate = Path(value)
        if ".." in candidate.parts:
            raise CwError(
                "Verification command contains path traversal",
                ErrorCode.SCHEMA_VALIDATION_ERROR,
            )
        if candidate.is_absolute():
            try:
                candidate.resolve(strict=False).relative_to(root.resolve())
            except ValueError as exc:
                raise CwError(
                    "Verification command path escapes the project",
                    ErrorCode.SCHEMA_VALIDATION_ERROR,
                ) from exc


def _completion_digest(workflow: Workflow) -> str | None:
    return (
        None
        if workflow.completion_target is None
        else sha256_bytes(_canonical_bytes(asdict(workflow.completion_target)))
    )


def _receipt_path(root: Path, receipt_id: str) -> Path:
    return root / RECEIPT_DIR / f"{receipt_id}.json"


def _receipt_directory(root: Path) -> Path:
    directory = root / RECEIPT_DIR
    if not directory.exists():
        directory.mkdir(mode=0o700, parents=True)
    metadata = directory.lstat()
    if not stat.S_ISDIR(metadata.st_mode):
        raise CwError(
            "Verification receipt namespace is unsafe", ErrorCode.INTEGRITY_ERROR
        )
    return directory


def _runtime_parent(
    root: Path, namespace: str = "verification", *, allow_uninitialized: bool = False
) -> Path:
    metadata_root = root / ".cw"
    if metadata_root.is_symlink():
        raise CwError(
            "Verification metadata namespace is unsafe",
            ErrorCode.VERIFICATION_INFRASTRUCTURE_ERROR,
        )
    if metadata_root.is_dir():
        parent = metadata_root / "runtime"
    elif allow_uninitialized and root.is_dir() and not root.is_symlink():
        parent = root / ".cw-adapter-runtime"
    else:
        raise CwError(
            "Verification metadata namespace is unsafe",
            ErrorCode.VERIFICATION_INFRASTRUCTURE_ERROR,
        )
    if not namespace.isidentifier():
        raise CwError(
            "Verification runtime namespace is invalid",
            ErrorCode.VERIFICATION_INFRASTRUCTURE_ERROR,
        )
    directory = parent / namespace
    for candidate in (parent, directory):
        if candidate.exists() or candidate.is_symlink():
            if candidate.is_symlink() or not candidate.is_dir():
                raise CwError(
                    "Verification runtime namespace is unsafe",
                    ErrorCode.VERIFICATION_INFRASTRUCTURE_ERROR,
                )
        else:
            try:
                candidate.mkdir(mode=0o700)
            except OSError as exc:
                raise CwError(
                    "Verification runtime namespace is unsafe",
                    ErrorCode.VERIFICATION_INFRASTRUCTURE_ERROR,
                ) from exc
    metadata = directory.lstat()
    expected_uid = getattr(os, "getuid", lambda: metadata.st_uid)()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or directory.is_symlink()
        or metadata.st_uid != expected_uid
    ):
        raise CwError(
            "Verification runtime namespace is unsafe",
            ErrorCode.VERIFICATION_INFRASTRUCTURE_ERROR,
        )
    return directory


@contextmanager
def private_runtime_directory(root: Path, namespace: str):
    initialized = (root / ".cw").is_dir() and not (root / ".cw").is_symlink()
    base = root / ".cw/runtime" if initialized else root / ".cw-adapter-runtime"
    paths = (root, *( (root / ".cw",) if initialized else () ), base, base / namespace)
    original = {
        path: (path.stat().st_atime_ns, path.stat().st_mtime_ns)
        for path in paths
        if path.exists()
    }
    parent = _runtime_parent(root, namespace, allow_uninitialized=True)
    runtime = Path(tempfile.mkdtemp(prefix=f"cw-{namespace}-", dir=parent))
    primary_error: BaseException | None = None
    try:
        os.chmod(runtime, 0o700)
        _safe_runtime(runtime)
        yield runtime
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        cleanup_error: CwError | None = None
        try:
            _cleanup_runtime(runtime)
            try:
                parent.rmdir()
            except OSError:
                pass
            for path in reversed(paths):
                if path not in original:
                    try:
                        path.rmdir()
                    except OSError:
                        pass
                elif path.exists():
                    try:
                        os.utime(path, ns=original[path], follow_symlinks=False)
                    except NotImplementedError:
                        if path.is_symlink():
                            raise CwError(
                                "Verification runtime namespace is unsafe",
                                ErrorCode.VERIFICATION_INFRASTRUCTURE_ERROR,
                            )
                        os.utime(path, ns=original[path])
        except OSError as exc:
            cleanup_error = CwError(
                "Verification runtime namespace cleanup failed",
                ErrorCode.VERIFICATION_INFRASTRUCTURE_ERROR,
                "Run: cw retry",
                details=redact(str(exc)),
            )
        except CwError as exc:
            cleanup_error = exc
        if cleanup_error is not None:
            # A runtime that cannot be removed cannot safely preserve a
            # seemingly valid adapter/reviewer result. Keep the original
            # failure as the chained cause, never as an unclassified escape.
            if primary_error is not None:
                raise cleanup_error from primary_error
            raise cleanup_error


def validate_verification_receipt(
    root: Path,
    workflow: Workflow,
    phase: Phase,
    reference: str,
    expected_sha256: str,
) -> dict[str, Any]:
    expected_reference = f"{RECEIPT_DIR}/{Path(reference).name}"
    if reference != expected_reference or Path(reference).name.startswith("."):
        raise CwError(
            "Verification receipt reference is unsafe", ErrorCode.INTEGRITY_ERROR
        )
    path = root / reference
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise CwError(
            "Verification receipt is missing", ErrorCode.INTEGRITY_ERROR
        ) from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise CwError("Verification receipt file is unsafe", ErrorCode.INTEGRITY_ERROR)
    if sha256_file(path) != expected_sha256:
        raise CwError(
            "Verification receipt digest does not match", ErrorCode.INTEGRITY_ERROR
        )
    payload = load_json(path)
    if (
        not isinstance(payload, dict)
        or set(payload) != _RECEIPT_FIELDS
        or payload.get("schema_version") != SCHEMA_VERSION
        or payload.get("schema") != RECEIPT_SCHEMA
        or not isinstance(payload.get("correlation_id"), str)
        or not isinstance(payload.get("semantic_attempt"), int)
        or isinstance(payload.get("semantic_attempt"), bool)
        or payload.get("semantic_attempt", 0) < 1
        or not isinstance(payload.get("artifact_identities"), dict)
        or not isinstance(payload.get("review_paths"), list)
    ):
        raise CwError(
            "Verification receipt schema is invalid", ErrorCode.INTEGRITY_ERROR
        )
    if payload.get("receipt_sha256") != _receipt_digest(payload):
        raise CwError(
            "Verification receipt integrity check failed", ErrorCode.INTEGRITY_ERROR
        )
    if path.name != f"{payload.get('receipt_id')}.json":
        raise CwError(
            "Verification receipt name does not match its identity",
            ErrorCode.INTEGRITY_ERROR,
        )
    duplicates = []
    for candidate in path.parent.glob("*.json"):
        if candidate == path or candidate.is_symlink() or not candidate.is_file():
            continue
        try:
            other = load_json(candidate)
        except CwError:
            continue
        if isinstance(other, dict) and other.get("receipt_id") == payload.get(
            "receipt_id"
        ):
            duplicates.append(candidate.name)
    if duplicates:
        raise CwError(
            "Verification receipt identity is duplicated", ErrorCode.INTEGRITY_ERROR
        )
    if payload.get("workflow_id") != workflow.id or payload.get("phase_id") != phase.id:
        raise CwError(
            "Verification receipt belongs to another workflow or phase",
            ErrorCode.INTEGRITY_ERROR,
        )
    if payload.get("workflow_sha256") != workflow_hash(
        root / ".codex/workflow/phases.yaml"
    ):
        raise CwError(
            "Verification receipt workflow identity changed", ErrorCode.INTEGRITY_ERROR
        )
    revision = artifact_revision_metadata(
        root, workflow, load_json(root / ".cw/state.json")
    )
    if payload.get("plan_revision_id") != revision.get("plan_revision_id"):
        raise CwError(
            "Verification receipt belongs to another plan revision",
            ErrorCode.INTEGRITY_ERROR,
        )
    if payload.get("completion_contract_sha256") != _completion_digest(workflow):
        raise CwError(
            "Verification receipt Completion Contract changed",
            ErrorCode.INTEGRITY_ERROR,
        )
    if payload.get("review_paths") != list(phase.review_paths):
        raise CwError(
            "Verification receipt review scope changed", ErrorCode.INTEGRITY_ERROR
        )
    preflight = payload.get("preflight")
    runtime = payload.get("runtime")
    if (
        not isinstance(preflight, dict)
        or set(preflight) != {"status", "checks"}
        or preflight.get("status") != "PASSED"
        or not isinstance(preflight.get("checks"), list)
        or not isinstance(runtime, dict)
        or set(runtime) != {"id", "private", "cache_isolated", "environment"}
        or runtime.get("private") is not True
        or runtime.get("cache_isolated") is not True
        or runtime.get("environment") != list(_PRIVATE_ENV)
    ):
        raise CwError(
            "Verification receipt preflight is invalid", ErrorCode.INTEGRITY_ERROR
        )
    commands = payload.get("commands")
    if not isinstance(commands, list) or len(commands) != len(phase.required_commands):
        raise CwError(
            "Verification receipt command inventory is invalid",
            ErrorCode.INTEGRITY_ERROR,
        )
    if any(
        not isinstance(item, dict)
        or set(item) != _COMMAND_FIELDS
        or item.get("index") != index
        or not isinstance(item.get("argv"), list)
        or not all(isinstance(value, str) for value in item["argv"])
        or item.get("cwd") != "."
        or not isinstance(item.get("timeout_seconds"), int)
        or not isinstance(item.get("exit_code"), int)
        or not isinstance(item.get("duration_ms"), int)
        for index, item in enumerate(commands)
    ):
        raise CwError(
            "Verification receipt command schema is invalid",
            ErrorCode.INTEGRITY_ERROR,
        )
    expected_commands = [item.command for item in phase.required_commands]
    if [
        item.get("command") for item in commands if isinstance(item, dict)
    ] != expected_commands:
        raise CwError(
            "Verification receipt command ordering changed", ErrorCode.INTEGRITY_ERROR
        )
    if payload.get("artifact_identities") != artifact_hashes(root, phase.artifacts):
        raise CwError(
            "Artifacts changed after deterministic verification",
            ErrorCode.INTEGRITY_ERROR,
        )
    for record, command in zip(commands, phase.required_commands, strict=True):
        if (
            record["argv"] != command_arguments(command.command)
            or record["timeout_seconds"]
            != (command.timeout_seconds or workflow.command_timeout)
        ):
            raise CwError(
                "Verification receipt command identity changed",
                ErrorCode.INTEGRITY_ERROR,
            )
    if payload.get("result") != "PASSED" or any(
        item.get("exit_code") != 0 for item in commands
    ):
        raise CwError(
            "Verification receipt does not prove successful commands",
            ErrorCode.INTEGRITY_ERROR,
        )
    return payload


class VerificationExecutor:
    """Execute governed deterministic checks and emit integrity-bound evidence."""

    def execute(self, root: Path, workflow: Workflow, phase: Phase) -> ValidationResult:
        result = ValidationResult(passed=False)
        runtime: Path | None = None
        commands: list[dict[str, Any]] = []
        stage = "preflight"
        try:
            # These snapshots are part of the verification preflight.  Keep
            # them inside the classified boundary so platform filesystem or
            # Git failures cannot escape as an unstructured CLI internal error.
            state_sha = sha256_file(root / ".cw/state.json")
            project_before = _project_snapshot(root, phase.artifacts)
            git_before = _git_metadata_snapshot(root)
            validate_dependencies(root, workflow, phase)
            result.checks.append({"name": "Previous gates", "status": "passed"})
            for artifact in phase.artifacts:
                from cw.core.utils import safe_project_path

                safe_project_path(root, artifact, must_exist=True)
            result.checks.append(
                {"name": "Artifacts", "status": "passed", "count": len(phase.artifacts)}
            )
            runtime = Path(
                tempfile.mkdtemp(
                    prefix="cw-verification-", dir=_runtime_parent(root)
                )
            )
            os.chmod(runtime, 0o700)
            _safe_runtime(runtime)
            environment = _runtime_environment(runtime)
            runtime_id = "vrt-" + sha256_bytes(secrets.token_bytes(32)).removeprefix(
                "sha256:"
            )
            stage = "commands"
            for index, command in enumerate(phase.required_commands):
                timeout = command.timeout_seconds or workflow.command_timeout
                argv = command_arguments(command.command)
                _validate_command_paths(root, argv)
                sink = current_event_sink()
                if sink is not None:
                    sink(
                        ExecutionEvent(
                            ExecutionEventType.COMMAND_STARTED,
                            source_type="cw.verification.command",
                            command=command.command,
                            status="in_progress",
                        )
                    )
                started = time.monotonic()
                try:
                    process = subprocess.Popen(
                        argv,
                        cwd=root,
                        shell=False,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        env=environment,
                        stdin=subprocess.DEVNULL,
                        **popen_process_group_kwargs(),
                    )
                    try:
                        stdout, stderr = process.communicate(timeout=timeout)
                    except subprocess.TimeoutExpired:
                        stop_process_group(process)
                        process.communicate()
                        raise
                    completed = subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
                except subprocess.TimeoutExpired as exc:
                    raise CwError(
                        f"Verification command timed out: {command.command}",
                        ErrorCode.VERIFICATION_TIMEOUT,
                        "Run: cw retry",
                        details=redact(str(exc)),
                    ) from exc
                except OSError as exc:
                    raise CwError(
                        f"Verification command could not start: {command.command}",
                        ErrorCode.VERIFICATION_INFRASTRUCTURE_ERROR,
                        "Run: cw retry",
                        details=redact(str(exc)),
                    ) from exc
                duration = max(0, round((time.monotonic() - started) * 1000))
                stdout = redact(completed.stdout) or ""
                stderr = redact(completed.stderr) or ""
                record = {
                    "index": index,
                    "command": command.command,
                    "argv": argv,
                    "cwd": ".",
                    "timeout_seconds": timeout,
                    "exit_code": completed.returncode,
                    "duration_ms": duration,
                    "stdout_sha256": sha256_bytes(stdout.encode()),
                    "stderr_sha256": sha256_bytes(stderr.encode()),
                }
                commands.append(record)
                result.checks.append(
                    {
                        "name": "Required command",
                        "command": command.command,
                        "exit_code": completed.returncode,
                        "duration_ms": duration,
                    }
                )
                if sink is not None:
                    sink(
                        ExecutionEvent(
                            ExecutionEventType.COMMAND_COMPLETED,
                            source_type="cw.verification.command",
                            command=command.command,
                            exit_code=completed.returncode,
                            duration_ms=duration,
                            status="completed"
                            if completed.returncode == 0
                            else "failed",
                        )
                    )
                if completed.returncode:
                    raise CwError(
                        f"Required command failed: {command.command}",
                        ErrorCode.VERIFICATION_COMMAND_FAILED,
                        "Fix the implementation, then run: cw validate",
                        details=(stderr or stdout)[-4000:],
                    )
            stage = "integrity"
            validate_dependencies(root, workflow, phase)
            if _project_snapshot(root, phase.artifacts) != project_before:
                raise CwError(
                    "Verification command mutated the project",
                    ErrorCode.VERIFICATION_COMMAND_FAILED,
                    "Restore project files and configure tool caches under the private runtime.",
                )
            if _git_metadata_snapshot(root) != git_before:
                raise CwError(
                    "Verification command mutated Git metadata",
                    ErrorCode.INTEGRITY_ERROR,
                    "Restore Git metadata and rerun verification.",
                )
            result.artifact_hashes = artifact_hashes(root, phase.artifacts)
            result.checks.append({"name": "SHA-256 integrity", "status": "passed"})
            state = load_json(root / ".cw/state.json")
            revision = artifact_revision_metadata(root, workflow, state)
            body: dict[str, Any] = {
                "schema_version": SCHEMA_VERSION,
                "schema": RECEIPT_SCHEMA,
                "receipt_id": "vr-"
                + sha256_bytes(secrets.token_bytes(32)).removeprefix("sha256:"),
                "correlation_id": secrets.token_hex(16),
                "created_at": utc_now(),
                "workflow_id": workflow.id,
                "workflow_sha256": workflow_hash(root / ".codex/workflow/phases.yaml"),
                "state_sha256_before": state_sha,
                "plan_revision_id": revision.get("plan_revision_id"),
                "phase_id": phase.id,
                "semantic_attempt": int(state.get("attempt", 0)) + 1,
                "artifact_identities": result.artifact_hashes,
                "review_paths": list(phase.review_paths),
                "completion_contract_sha256": _completion_digest(workflow),
                "commands": commands,
                "preflight": {
                    "status": "PASSED",
                    "checks": [
                        "type",
                        "owner",
                        "symlink",
                        "hardlink",
                        "write",
                        "fsync",
                        "rename",
                        "delete",
                        "containment",
                    ],
                },
                "runtime": {
                    "id": runtime_id,
                    "private": True,
                    "cache_isolated": True,
                    "environment": list(_PRIVATE_ENV),
                },
                "result": "PASSED",
            }
            body["receipt_sha256"] = _receipt_digest(body)
            # Receipt persistence is deliberately after cleanup.  A runtime
            # that cannot be removed is infrastructure failure, never proof.
            stage = "runtime_cleanup"
            _cleanup_runtime(runtime)
            runtime = None
            stage = "persist"
            _receipt_directory(root)
            path = _receipt_path(root, body["receipt_id"])
            atomic_json_new(path, body)
            reference = path.relative_to(root).as_posix()
            file_sha = sha256_file(path)
            validate_verification_receipt(root, workflow, phase, reference, file_sha)
            result.receipt_payload = body
            result.receipt = {
                "reference": reference,
                "sha256": file_sha,
                "digest": body["receipt_sha256"],
                "receipt_id": body["receipt_id"],
            }
            result.passed = True
        except CwError as exc:
            result.error_code = exc.code.value
            result.errors.append(exc.message)
            result.checks.append(
                {
                    "name": "Verification",
                    "status": "failed",
                    "phase": stage,
                    "operation": "verification-executor",
                    "detail": exc.message,
                    "error_code": exc.code.value,
                    "next_action": exc.hint or "Run: cw validate",
                }
            )
        except OSError as exc:
            error = CwError(
                "Verification runtime preflight failed",
                ErrorCode.VERIFICATION_INFRASTRUCTURE_ERROR,
                "Run: cw retry",
                details=redact(str(exc)),
            )
            result.error_code = error.code.value
            result.errors.append(error.message)
            result.checks.append(
                {
                    "name": "Verification",
                    "status": "failed",
                    "phase": stage,
                    "operation": "verification-executor",
                    "detail": error.message,
                    "error_code": error.code.value,
                    "next_action": error.hint,
                }
            )
        finally:
            if runtime is not None:
                try:
                    _cleanup_runtime(runtime)
                except CwError as cleanup_error:
                    _record_runtime_cleanup_failure(result, cleanup_error)
        return result


def doctor_verification_runtime(root: Path) -> list[dict[str, Any]]:
    runtime: Path | None = None
    checks: list[dict[str, Any]] = []
    namespaces = (root / ".cw", root / ".cw/runtime", root / ".cw/runtime/verification")
    namespace_state = {
        path: (path.exists(), path.stat().st_atime_ns, path.stat().st_mtime_ns)
        for path in namespaces
        if path.exists()
    }
    try:
        runtime = Path(
            tempfile.mkdtemp(
                prefix="cw-verification-doctor-", dir=_runtime_parent(root)
            )
        )
        os.chmod(runtime, 0o700)
        _safe_runtime(runtime)
        environment = _runtime_environment(runtime)
        checks.extend(
            [
                {
                    "name": "Verification runtime",
                    "status": "pass",
                    "detail": "private owner-only directory",
                },
                {
                    "name": "Verification temp/cache preflight",
                    "status": "pass",
                    "detail": "write, fsync, rename, delete",
                },
                {
                    "name": "Verification cache isolation",
                    "status": "pass",
                    "detail": ", ".join(_PRIVATE_ENV),
                },
                {
                    "name": "Verification redaction",
                    "status": "pass",
                    "detail": "stdout/stderr persisted as redacted SHA-256 digests",
                },
            ]
        )
        if any(
            not str(environment[name]).startswith(str(runtime)) for name in _PRIVATE_ENV
        ):
            raise CwError(
                "Verification cache escaped private runtime",
                ErrorCode.VERIFICATION_INFRASTRUCTURE_ERROR,
            )
    except (CwError, OSError) as exc:
        checks.append(
            {
                "name": "Verification runtime",
                "status": "error",
                "detail": redact(str(exc)),
            }
        )
    finally:
        if runtime is not None:
            try:
                _cleanup_runtime(runtime)
            except CwError as cleanup_error:
                checks.append(
                    {
                        "name": "Verification runtime cleanup",
                        "status": "error",
                        "detail": cleanup_error.message,
                    }
                )
            else:
                checks.append(
                    {
                        "name": "Verification runtime cleanup",
                        "status": "pass",
                        "detail": "removed",
                    }
                )
        for path in reversed(namespaces):
            existed = namespace_state.get(path)
            if existed is None:
                try:
                    path.rmdir()
                except OSError:
                    pass
            elif path.exists():
                os.utime(path, ns=(existed[1], existed[2]), follow_symlinks=False)
    return checks
