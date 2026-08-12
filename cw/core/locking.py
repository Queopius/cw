from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .errors import CwError, ErrorCode
from .layout import safe_directory, safe_file
from .utils import utc_now


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


@contextmanager
def operation_lock(root: Path, operation: str) -> Iterator[None]:
    runtime = safe_directory(root / ".cw", ".cw", create=True)
    locks = safe_directory(runtime / "locks", ".cw/locks", create=True)
    lock = locks / "operation.lock"
    safe_file(lock, ".cw/locks/operation.lock")
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except FileExistsError:
        try:
            data = json.loads(lock.read_text(encoding="utf-8"))
            pid = int(data.get("pid", 0))
        except Exception:
            pid = 0
        if pid and _alive(pid):
            raise CwError("Another CW operation is active", ErrorCode.LOCKED, "Wait for it to finish, then retry.")
        lock.unlink(missing_ok=True)
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.write(descriptor, json.dumps({"pid": os.getpid(), "operation": operation, "started_at": utc_now()}).encode())
        os.close(descriptor)
        yield
    finally:
        lock.unlink(missing_ok=True)
