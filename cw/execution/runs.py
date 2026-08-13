from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

from cw import __version__
from cw.core.diagnostics import redact
from cw.core.errors import CwError, ErrorCode
from cw.core.utils import atomic_json, load_json, utc_now

from .events import ExecutionEvent, ExecutionEventType, ExecutionState, StartupProfile


ACTIVE_RUN_FILE = ".cw/runtime/active-run.json"
RUN_LOG_DIRECTORY = ".cw/logs/runs"
RUN_SCHEMA_VERSION = 1


def new_run_id() -> str:
    return f"run_{uuid.uuid4().hex}"


def active_run_path(root: Path) -> Path:
    return root / ACTIVE_RUN_FILE


def run_log_directory(root: Path) -> Path:
    return root / RUN_LOG_DIRECTORY


def load_active_run(root: Path) -> dict[str, Any] | None:
    path = active_run_path(root)
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise CwError("Managed run metadata is unsafe", ErrorCode.INVALID_STATE, "Run: cw repair")
    data = load_json(path)
    if not isinstance(data, dict) or data.get("schema_version") != RUN_SCHEMA_VERSION:
        raise CwError("Managed run metadata is invalid", ErrorCode.INVALID_STATE, "Run: cw repair")
    run_id = data.get("run_id")
    if not isinstance(run_id, str) or not run_id.startswith("run_") or len(run_id) != 36:
        raise CwError("Managed run identifier is invalid", ErrorCode.INVALID_STATE, "Run: cw repair")
    return data


class RunRecorder:
    """Persist a compact run identity and a redacted, versioned event stream."""

    def __init__(
        self,
        root: Path,
        *,
        run_id: str,
        phase_id: str,
        role: str,
        session_id: str | None = None,
    ) -> None:
        self.root = root
        self.run_id = run_id
        self.phase_id = phase_id
        self.role = role
        self.path = active_run_path(root)
        self.events_path = run_log_directory(root) / f"{run_id}.jsonl"
        self.payload: dict[str, Any] = {
            "schema_version": RUN_SCHEMA_VERSION,
            "run_id": run_id,
            "cw_version": __version__,
            "role": role,
            "phase": phase_id,
            "session_id": session_id,
            "supervisor_pid": os.getpid(),
            "process_pid": None,
            "status": ExecutionState.STARTING.value,
            "started_at": utc_now(),
            "last_event_at": None,
            "finished_at": None,
            "profile": {},
            "usage": {},
            "last_activity": "Starting Codex session",
        }
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(self.path, self.payload)

    def record(self, event: ExecutionEvent, state: ExecutionState) -> None:
        record = {
            "schema_version": RUN_SCHEMA_VERSION,
            "run_id": self.run_id,
            "phase": self.phase_id,
            **event.to_dict(),
            "state": state.value,
        }
        # Command/path summaries have already been constrained by the parser;
        # redact again at the persistence boundary as defence in depth.
        serialized = redact(json.dumps(record, ensure_ascii=False, sort_keys=True)) or "{}"
        self._append(serialized)
        self.payload["status"] = state.value
        self.payload["last_event_at"] = event.timestamp
        if event.process_id is not None:
            self.payload["process_pid"] = event.process_id
        if event.session_id:
            self.payload["codex_session_id"] = event.session_id
        if event.command:
            self.payload["last_activity"] = f"Running {event.command}" if event.type.value == "COMMAND_STARTED" else event.command
        elif event.summary:
            self.payload["last_activity"] = event.summary
        elif event.type is ExecutionEventType.PROCESS_STARTED:
            self.payload["last_activity"] = "Codex process started"
        elif event.type is ExecutionEventType.SESSION_STARTED:
            self.payload["last_activity"] = "Codex session initialized"
        elif event.type is ExecutionEventType.TURN_STARTED:
            self.payload["last_activity"] = "Codex working"
        elif event.type is ExecutionEventType.FILE_CHANGED:
            self.payload["last_activity"] = "Updating project files"
        elif event.type is ExecutionEventType.TURN_COMPLETED:
            self.payload["last_activity"] = "Codex turn completed"
        if event.usage:
            self.payload["usage"] = event.usage
        atomic_json(self.path, self.payload)

    def profile(self, profile: StartupProfile) -> None:
        current = self.payload.get("profile") if isinstance(self.payload.get("profile"), dict) else {}
        self.payload["profile"] = {**current, **profile.to_dict()}
        atomic_json(self.path, self.payload)

    def finish(self, *, status: ExecutionState, elapsed_seconds: float) -> None:
        self.payload.update({
            "status": status.value,
            "finished_at": utc_now(),
            "elapsed_seconds": round(max(0.0, elapsed_seconds), 3),
        })
        summary = run_log_directory(self.root) / f"{self.run_id}.json"
        atomic_json(summary, self.payload)
        self.path.unlink(missing_ok=True)
        self._retain(20)

    def _append(self, line: str) -> None:
        if self.events_path.is_symlink():
            raise CwError("Managed run event log is unsafe", ErrorCode.INVALID_STATE)
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.events_path, flags, 0o600)
        try:
            os.write(descriptor, (line + "\n").encode("utf-8"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _retain(self, count: int) -> None:
        summaries = sorted(
            (path for path in run_log_directory(self.root).glob("run_*.json") if path.is_file() and not path.is_symlink()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for path in summaries[count:]:
            events = path.with_suffix(".jsonl")
            path.unlink(missing_ok=True)
            events.unlink(missing_ok=True)


def latest_run(root: Path) -> dict[str, Any] | None:
    active = load_active_run(root)
    if active is not None:
        return active
    directory = run_log_directory(root)
    if not directory.is_dir() or directory.is_symlink():
        return None
    candidates = sorted(
        (path for path in directory.glob("run_*.json") if path.is_file() and not path.is_symlink()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        return None
    try:
        data = load_json(candidates[0])
    except CwError:
        return None
    return data if isinstance(data, dict) else None


def archive_interrupted_run(root: Path, run: dict[str, Any]) -> Path:
    """Preserve crash diagnostics while removing a stale active-run blocker."""

    run_id = str(run.get("run_id", ""))
    if not run_id.startswith("run_") or len(run_id) != 36:
        raise CwError("Managed run identifier is invalid", ErrorCode.INVALID_STATE)
    record = {
        **run,
        "status": "INTERRUPTED",
        "finished_at": utc_now(),
        "last_activity": run.get("last_activity") or "CW supervisor interrupted",
    }
    path = run_log_directory(root) / f"{run_id}.json"
    atomic_json(path, record)
    active_run_path(root).unlink(missing_ok=True)
    return path


def load_run(root: Path, run_id: str) -> dict[str, Any]:
    if not run_id.startswith("run_") or len(run_id) != 36:
        raise CwError("Run identifier is invalid", ErrorCode.USAGE_ERROR, exit_code=2)
    active = load_active_run(root)
    if active and active.get("run_id") == run_id:
        return active
    path = run_log_directory(root) / f"{run_id}.json"
    if path.is_symlink() or not path.is_file():
        raise CwError("Managed run was not found", ErrorCode.INVALID_STATE, "Run: cw logs")
    data = load_json(path)
    if not isinstance(data, dict):
        raise CwError("Managed run record is invalid", ErrorCode.INVALID_STATE)
    return data


def load_run_events(root: Path, run_id: str) -> list[dict[str, Any]]:
    load_run(root, run_id)
    path = run_log_directory(root) / f"{run_id}.jsonl"
    if path.is_symlink() or not path.is_file():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append(value)
    return events
