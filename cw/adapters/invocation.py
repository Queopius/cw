from __future__ import annotations

import hashlib
import json
import os
import shlex
from pathlib import Path
from typing import Mapping, Sequence

from cw.core.diagnostics import redact
from cw.core.utils import utc_now


_INHERITED_PROCESS_CONTEXT = {
    "CODEX_CI",
    "CODEX_MANAGED_BY_NPM",
    "CODEX_MANAGED_PACKAGE_ROOT",
    "CODEX_PERMISSION_PROFILE",
    "CODEX_THREAD_ID",
}
_DIAGNOSTIC_ENVIRONMENT = (
    "HOME",
    "CODEX_HOME",
    "XDG_CONFIG_HOME",
    "PATH",
    "CW_IMPLEMENTER_ACTIVE",
    "CW_IMPLEMENTER_SESSION",
    "CW_PLANNER_ACTIVE",
    "CW_REVIEWER_ACTIVE",
    "CW_COMPLETION_REVIEWER_ACTIVE",
    "CW_EXTENSION_PLANNER_ACTIVE",
)


def managed_codex_environment(role: str, *, session_id: str | None = None) -> dict[str, str]:
    """Preserve user authentication/config while dropping parent-process identity.

    CW deliberately does not create a private CODEX_HOME.  The removed values
    describe the supervising Codex process, not the user's durable credentials.
    """

    environment = os.environ.copy()
    for name in _INHERITED_PROCESS_CONTEXT:
        environment.pop(name, None)
    environment[f"CW_{role.upper()}_ACTIVE"] = "1"
    if role == "implementer" and session_id:
        environment["CW_IMPLEMENTER_SESSION"] = session_id
    return environment


def sanitized_invocation(
    command: Sequence[str], environment: Mapping[str, str], *, prompt: str | None = None
) -> dict[str, object]:
    argv = list(command)
    if prompt is not None and argv and argv[-1] == prompt:
        digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12]
        argv[-1] = f"[PROMPT sha256:{digest}]"
    clean_argv = [redact(value) or "" for value in argv]
    clean_environment = {
        name: redact(environment[name])
        for name in _DIAGNOSTIC_ENVIRONMENT
        if name in environment
    }
    return {
        "argv": clean_argv,
        "command": shlex.join(clean_argv),
        "environment": clean_environment,
    }


def record_invocation(
    root: Path,
    role: str,
    command: Sequence[str],
    environment: Mapping[str, str],
    *,
    prompt: str | None = None,
) -> dict[str, object]:
    record = {
        "timestamp": utc_now(),
        "role": role,
        **sanitized_invocation(command, environment, prompt=prompt),
    }
    logs = root / ".cw" / "logs"
    if logs.is_dir() and not logs.is_symlink():
        path = logs / "codex-invocations.jsonl"
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
        try:
            os.write(descriptor, (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode())
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return record


def invocation_details(record: Mapping[str, object]) -> str:
    environment = json.dumps(record.get("environment", {}), ensure_ascii=False, sort_keys=True)
    return f"Codex argv: {record.get('command', '')}\nSanitized environment: {environment}"


def record_run_result(
    root: Path,
    role: str,
    *,
    exit_code: int,
    stdout: str,
    stderr: str,
    diagnostics: Sequence[Mapping[str, object]],
) -> None:
    """Retain redacted child diagnostics without streaming them to normal UI."""

    logs = root / ".cw" / "logs"
    if not logs.is_dir() or logs.is_symlink():
        return
    record = {
        "timestamp": utc_now(),
        "role": role,
        "exit_code": exit_code,
        "stdout": redact(stdout),
        "stderr": redact(stderr),
        "integration_diagnostics": list(diagnostics),
    }
    path = logs / "codex-runs.jsonl"
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        os.write(descriptor, (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode())
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def latest_invocation(root: Path) -> dict[str, object] | None:
    path = root / ".cw" / "logs" / "codex-invocations.jsonl"
    if not path.is_file() or path.is_symlink():
        return None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        value = json.loads(lines[-1]) if lines else None
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None
