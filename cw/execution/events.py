from __future__ import annotations

import json
import re
import shlex
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Protocol

from cw.core.diagnostics import redact
from cw.core.utils import utc_now


class ExecutionState(str, Enum):
    PREFLIGHT = "PREFLIGHT"
    STARTING = "STARTING"
    SESSION_INITIALIZING = "SESSION_INITIALIZING"
    IMPLEMENTING = "IMPLEMENTING"
    RUNNING_COMMAND = "RUNNING_COMMAND"
    WRITING_FILES = "WRITING_FILES"
    WAITING = "WAITING"
    VALIDATING = "VALIDATING"
    REVIEWING = "REVIEWING"
    GATING = "GATING"
    ADVANCING = "ADVANCING"
    COMPLETED = "COMPLETED"
    STOPPING = "STOPPING"
    ERROR = "ERROR"


class ExecutionEventType(str, Enum):
    PROCESS_STARTED = "PROCESS_STARTED"
    SESSION_STARTED = "SESSION_STARTED"
    TURN_STARTED = "TURN_STARTED"
    COMMAND_STARTED = "COMMAND_STARTED"
    COMMAND_COMPLETED = "COMMAND_COMPLETED"
    FILE_CHANGED = "FILE_CHANGED"
    VALIDATION_STARTED = "VALIDATION_STARTED"
    VALIDATION_COMPLETED = "VALIDATION_COMPLETED"
    REVIEW_STARTED = "REVIEW_STARTED"
    REVIEW_COMPLETED = "REVIEW_COMPLETED"
    GATE_CREATED = "GATE_CREATED"
    PHASE_ADVANCED = "PHASE_ADVANCED"
    TURN_COMPLETED = "TURN_COMPLETED"
    HEARTBEAT = "HEARTBEAT"
    QUIET_WARNING = "QUIET_WARNING"
    STOP_REQUESTED = "STOP_REQUESTED"
    PROCESS_COMPLETED = "PROCESS_COMPLETED"
    ERROR = "ERROR"
    WARNING = "WARNING"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class ExecutionEvent:
    type: ExecutionEventType
    timestamp: str = field(default_factory=utc_now)
    source_type: str | None = None
    session_id: str | None = None
    item_id: str | None = None
    command: str | None = None
    exit_code: int | None = None
    duration_ms: int | None = None
    files: tuple[dict[str, str], ...] = ()
    usage: dict[str, int] | None = None
    summary: str | None = None
    status: str | None = None
    elapsed_seconds: float | None = None
    process_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "event_type": self.type.value,
            "timestamp": self.timestamp,
        }
        for key in (
            "source_type", "session_id", "item_id", "command", "exit_code",
            "duration_ms", "usage", "summary", "status", "elapsed_seconds",
            "process_id",
        ):
            item = getattr(self, key)
            if item is not None:
                value[key] = item
        if self.files:
            value["files"] = [dict(item) for item in self.files]
        return value


class Clock(Protocol):
    def monotonic(self) -> float: ...


class MonotonicClock:
    def monotonic(self) -> float:
        return time.monotonic()


def display_command(command: str) -> str:
    """Return a compact, redacted command without evaluating shell text."""

    clean = (redact(command) or "").strip()
    clean = re.sub(
        r"(?i)(--(?:api[-_]?key|access[-_]?token|auth[-_]?token|token|password|secret)=)[^\s]+",
        r"\1[REDACTED]",
        clean,
    )
    try:
        parts = shlex.split(clean)
    except ValueError:
        parts = []
    if len(parts) >= 3 and Path(parts[0]).name in {
        "sh", "bash", "zsh", "dash", "fish", "pwsh", "powershell",
    } and parts[1] in {"-c", "-lc", "-Command"}:
        clean = parts[2]
    return clean if len(clean) <= 500 else clean[:497] + "..."


class CodexEventParser:
    """Normalize the documented ``codex exec --json`` JSONL event stream.

    Agent messages and reasoning items are deliberately not emitted. CW only
    exposes observable execution facts: sessions, commands, files and usage.
    """

    def __init__(self, root: Path | None = None, *, clock: Clock | None = None) -> None:
        self.root = root.resolve() if root is not None else None
        self.clock = clock or MonotonicClock()
        self._command_started: dict[str, float] = {}
        self._seen: set[str] = set()

    def parse_line(self, line: str) -> ExecutionEvent | None:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            return ExecutionEvent(
                ExecutionEventType.WARNING,
                source_type="malformed_jsonl",
                summary="Malformed Codex event was ignored",
            )
        if not isinstance(value, dict) or not isinstance(value.get("type"), str):
            return ExecutionEvent(
                ExecutionEventType.WARNING,
                source_type="malformed_event",
                summary="Malformed Codex event was ignored",
            )
        source = value["type"]
        item = value.get("item") if isinstance(value.get("item"), dict) else {}
        item_type = item.get("type")
        # Never expose or persist model reasoning or conversational narration.
        if item_type in {"reasoning", "agent_message"}:
            return None
        event: ExecutionEvent
        if source == "thread.started":
            event = ExecutionEvent(
                ExecutionEventType.SESSION_STARTED,
                source_type=source,
                session_id=str(value.get("thread_id") or "") or None,
            )
        elif source == "turn.started":
            event = ExecutionEvent(ExecutionEventType.TURN_STARTED, source_type=source)
        elif source in {"item.started", "item.completed"} and item_type == "command_execution":
            item_id = str(item.get("id") or "") or None
            command = display_command(str(item.get("command") or "Codex command"))
            if source == "item.started":
                if item_id:
                    self._command_started[item_id] = self.clock.monotonic()
                event = ExecutionEvent(
                    ExecutionEventType.COMMAND_STARTED,
                    source_type=source,
                    item_id=item_id,
                    command=command,
                    status=str(item.get("status") or "in_progress"),
                )
            else:
                duration = None
                started = self._command_started.pop(item_id, None) if item_id else None
                if started is not None:
                    duration = max(0, round((self.clock.monotonic() - started) * 1000))
                exit_code = item.get("exit_code")
                event = ExecutionEvent(
                    ExecutionEventType.COMMAND_COMPLETED,
                    source_type=source,
                    item_id=item_id,
                    command=command,
                    exit_code=exit_code if isinstance(exit_code, int) and not isinstance(exit_code, bool) else None,
                    duration_ms=duration,
                    status=str(item.get("status") or "completed"),
                )
        elif source in {"item.started", "item.completed"} and item_type == "file_change":
            if source == "item.started":
                return None
            changes: list[dict[str, str]] = []
            raw_changes = item.get("changes")
            if isinstance(raw_changes, list):
                for change in raw_changes:
                    if not isinstance(change, dict) or not isinstance(change.get("path"), str):
                        continue
                    path = self._display_path(change["path"])
                    changes.append({"path": path, "kind": str(change.get("kind") or "modify")})
            event = ExecutionEvent(
                ExecutionEventType.FILE_CHANGED,
                source_type=source,
                item_id=str(item.get("id") or "") or None,
                files=tuple(changes),
                status=str(item.get("status") or "completed"),
            )
        elif source == "turn.completed":
            usage = value.get("usage") if isinstance(value.get("usage"), dict) else {}
            safe_usage = {
                key: item for key, item in usage.items()
                if key in {
                    "input_tokens", "cached_input_tokens", "cache_write_input_tokens",
                    "output_tokens", "reasoning_output_tokens",
                } and isinstance(item, int) and not isinstance(item, bool) and item >= 0
            }
            event = ExecutionEvent(
                ExecutionEventType.TURN_COMPLETED,
                source_type=source,
                usage=safe_usage or None,
            )
        elif source in {"error", "turn.failed"}:
            event = ExecutionEvent(
                ExecutionEventType.ERROR,
                source_type=source,
                summary="Codex reported an execution error",
            )
        else:
            event = ExecutionEvent(ExecutionEventType.UNKNOWN, source_type=source)
        key = self._deduplication_key(event)
        if key in self._seen:
            return None
        self._seen.add(key)
        return event

    def _display_path(self, value: str) -> str:
        path = Path(value)
        if self.root is not None:
            try:
                return path.resolve().relative_to(self.root).as_posix()
            except (OSError, ValueError):
                pass
        return path.name or "project file"

    @staticmethod
    def _deduplication_key(event: ExecutionEvent) -> str:
        if event.type in {ExecutionEventType.HEARTBEAT, ExecutionEventType.QUIET_WARNING}:
            return f"{event.type.value}:{event.timestamp}"
        return json.dumps({
            "type": event.type.value,
            "source": event.source_type,
            "item": event.item_id,
            "command": event.command,
            "status": event.status,
            "files": event.files,
        }, sort_keys=True)


@dataclass(slots=True)
class StartupProfile:
    preflight_ms: int | None = None
    spawn_ms: int | None = None
    session_init_ms: int | None = None
    first_event_ms: int | None = None

    def to_dict(self) -> dict[str, int]:
        return {
            key: value for key, value in {
                "preflight_ms": self.preflight_ms,
                "spawn_ms": self.spawn_ms,
                "session_init_ms": self.session_init_ms,
                "first_event_ms": self.first_event_ms,
            }.items() if value is not None
        }


class ExecutionTracker:
    def __init__(
        self,
        *,
        clock: Clock | None = None,
        quiet_threshold_seconds: float = 90.0,
        heartbeat_seconds: float = 60.0,
    ) -> None:
        self.clock = clock or MonotonicClock()
        self.started = self.clock.monotonic()
        self.last_activity = self.started
        self.last_heartbeat = self.started
        self.quiet_threshold_seconds = quiet_threshold_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self.state = ExecutionState.STARTING
        self.current_command: str | None = None
        self.quiet_warning_emitted = False

    def observe(self, event: ExecutionEvent) -> ExecutionState:
        if event.type not in {ExecutionEventType.HEARTBEAT, ExecutionEventType.QUIET_WARNING}:
            self.last_activity = self.clock.monotonic()
            self.quiet_warning_emitted = False
        if event.type is ExecutionEventType.PROCESS_STARTED:
            self.state = ExecutionState.SESSION_INITIALIZING
        elif event.type in {ExecutionEventType.SESSION_STARTED, ExecutionEventType.TURN_STARTED}:
            self.state = ExecutionState.IMPLEMENTING
        elif event.type is ExecutionEventType.COMMAND_STARTED:
            self.state = ExecutionState.RUNNING_COMMAND
            self.current_command = event.command
        elif event.type is ExecutionEventType.COMMAND_COMPLETED:
            self.state = ExecutionState.IMPLEMENTING
            self.current_command = None
        elif event.type is ExecutionEventType.FILE_CHANGED:
            self.state = ExecutionState.WRITING_FILES
        elif event.type is ExecutionEventType.TURN_COMPLETED:
            self.state = ExecutionState.IMPLEMENTING
        elif event.type in {ExecutionEventType.VALIDATION_STARTED, ExecutionEventType.VALIDATION_COMPLETED}:
            self.state = ExecutionState.VALIDATING
        elif event.type in {ExecutionEventType.REVIEW_STARTED, ExecutionEventType.REVIEW_COMPLETED}:
            self.state = ExecutionState.REVIEWING
        elif event.type is ExecutionEventType.GATE_CREATED:
            self.state = ExecutionState.GATING
        elif event.type is ExecutionEventType.PHASE_ADVANCED:
            self.state = ExecutionState.ADVANCING
        elif event.type is ExecutionEventType.QUIET_WARNING:
            self.state = ExecutionState.WAITING
        elif event.type is ExecutionEventType.PROCESS_COMPLETED:
            self.state = (
                ExecutionState.COMPLETED
                if event.exit_code in {0, None}
                else ExecutionState.ERROR
            )
            self.current_command = None
        elif event.type is ExecutionEventType.STOP_REQUESTED:
            self.state = ExecutionState.STOPPING
        elif event.type is ExecutionEventType.ERROR:
            self.state = ExecutionState.ERROR
        return self.state

    def elapsed(self) -> float:
        return max(0.0, self.clock.monotonic() - self.started)

    def touch(self) -> None:
        """Record safe activity for an event that CW intentionally does not expose."""

        self.last_activity = self.clock.monotonic()
        self.quiet_warning_emitted = False

    def poll(self, *, process_alive: bool) -> ExecutionEvent | None:
        now = self.clock.monotonic()
        if not process_alive:
            return None
        quiet = now - self.last_activity
        # A command can legitimately be silent while its child process works.
        if self.current_command is None and quiet >= self.quiet_threshold_seconds and not self.quiet_warning_emitted:
            self.quiet_warning_emitted = True
            self.last_heartbeat = now
            return ExecutionEvent(
                ExecutionEventType.QUIET_WARNING,
                summary=f"No new Codex activity for {round(quiet)}s",
                elapsed_seconds=self.elapsed(),
            )
        if now - self.last_heartbeat >= self.heartbeat_seconds:
            self.last_heartbeat = now
            return ExecutionEvent(
                ExecutionEventType.HEARTBEAT,
                summary="Codex still working",
                elapsed_seconds=self.elapsed(),
            )
        return None


EventSink = Callable[[ExecutionEvent], None]
