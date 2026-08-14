from __future__ import annotations

import json
import os
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from cw.core.errors import CwError, ErrorCode
from cw.core.utils import atomic_json, utc_now
from cw.core.platform import process_is_alive


BATCH_FILE = ".cw/runtime/batch.json"
BATCH_HISTORY_DIR = ".cw/logs/batches"


def batch_path(root: Path) -> Path:
    return root / BATCH_FILE


def load_batch(root: Path) -> dict[str, Any] | None:
    path = batch_path(root)
    if not path.is_file() or path.is_symlink():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CwError("Batch session metadata is invalid", ErrorCode.INVALID_STATE, "Run: cw status", details=str(exc)) from exc
    return value if isinstance(value, dict) else None


def new_batch(start_phase: str, requested: int, max_time: int, version: str) -> dict[str, Any]:
    return {
        "schema_version": 1, "id": uuid.uuid4().hex, "pid": os.getpid(),
        "cw_version": version, "started_at": utc_now(), "start_phase": start_phase,
        "requested_phases": requested, "completed_phases": 0,
        "max_time_seconds": max_time, "elapsed_seconds": 0,
        "status": "RUNNING", "completed": [], "events": [], "stop_reason": None,
        "agent_runs": 0, "reviewer_runs": 0, "approvals": 0,
        "semantic_revisions": 0, "infrastructure_failures": 0,
    }


def save_batch(root: Path, session: dict[str, Any]) -> None:
    atomic_json(batch_path(root), session)


def active_batch(root: Path, *, own_pid: int | None = None) -> dict[str, Any] | None:
    """Return a live batch owned by another process, if one exists."""
    session = load_batch(root)
    if not session or session.get("status") != "RUNNING":
        return None
    pid = session.get("pid")
    if not isinstance(pid, int) or pid <= 0 or not process_is_alive(pid):
        return None
    if own_pid is not None and pid == own_pid:
        return None
    return session


def archive_batch(root: Path, session: dict[str, Any]) -> None:
    """Atomically retain one compact execution record per batch ID."""
    batch_id = session.get("id")
    if not isinstance(batch_id, str) or len(batch_id) != 32 or any(character not in "0123456789abcdef" for character in batch_id):
        raise CwError("Batch session identifier is invalid", ErrorCode.INVALID_STATE)
    path = root / BATCH_HISTORY_DIR / f"{batch_id}.json"
    record = {
        "schema_version": 1,
        "id": session.get("id"),
        "cw_version": session.get("cw_version"),
        "started_at": session.get("started_at"),
        "finished_at": session.get("finished_at"),
        "status": session.get("status"),
        "stop_reason": session.get("stop_reason"),
        "requested_phases": session.get("requested_phases"),
        "completed_phases": session.get("completed_phases"),
        "elapsed_seconds": session.get("elapsed_seconds"),
        "completed": session.get("completed", []),
        "reviewer_runs": session.get("reviewer_runs", 0),
        "approvals": session.get("approvals", 0),
        "semantic_revisions": session.get("semantic_revisions", 0),
        "infrastructure_failures": session.get("infrastructure_failures", 0),
    }
    atomic_json(path, record)


def completed_phase_durations(root: Path) -> list[int]:
    directory = root / BATCH_HISTORY_DIR
    if not directory.is_dir() or directory.is_symlink():
        return []
    durations: list[int] = []
    for path in sorted(directory.glob("*.json")):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for phase in record.get("completed", []) if isinstance(record, dict) else []:
            seconds = phase.get("duration_seconds") if isinstance(phase, dict) else None
            if isinstance(seconds, int) and 0 < seconds < 24 * 3600:
                durations.append(seconds)
    return durations


@contextmanager
def batch_lock(root: Path) -> Iterator[None]:
    lock = root / ".cw/locks/batch.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        try:
            data = json.loads(lock.read_text(encoding="utf-8")); pid = int(data.get("pid", 0))
        except Exception:
            pid = 0
        if pid and process_is_alive(pid):
            raise CwError("Workflow batch is already running", ErrorCode.LOCKED, "Run: cw status")
        lock.unlink(missing_ok=True)
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.write(descriptor, json.dumps({"pid": os.getpid(), "started_at": utc_now()}).encode())
    os.close(descriptor)
    try:
        yield
    finally:
        lock.unlink(missing_ok=True)
