from __future__ import annotations

from typing import Any

from cw.execution.events import (
    ExecutionEvent,
    ExecutionEventType,
    ExecutionState,
    ExecutionTracker,
    StartupProfile,
)
from cw.execution.runs import RunRecorder
from cw.execution.observability import load_observability_settings

from .console import Console, emit_json
from .symbols import ACTIVE, ERROR, PENDING, SUCCESS, WARNING


def _duration(seconds: float | int | None) -> str:
    total = max(0, round(float(seconds or 0)))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}h {minutes:02d}m {secs:02d}s"
    return f"{minutes:02d}m {secs:02d}s"


def _command_duration(milliseconds: int | None) -> str:
    if milliseconds is None:
        return ""
    seconds = milliseconds / 1000
    return f"{seconds:.1f}s" if seconds < 10 else f"{round(seconds)}s"


class LiveExecutionObserver:
    """Line-oriented live renderer and durable event observer.

    The observer renders facts only. It never renders agent-message or reasoning
    payloads, and JSON mode emits one independent event object per line.
    """

    def __init__(
        self,
        console: Console,
        recorder: RunRecorder,
        *,
        role: str,
        json_mode: bool = False,
        verbose: bool = False,
        tracker: ExecutionTracker | None = None,
    ) -> None:
        self.console = console
        self.recorder = recorder
        self.role = role
        self.json_mode = json_mode
        self.verbose = verbose
        if tracker is None:
            settings = load_observability_settings()
            tracker = ExecutionTracker(
                quiet_threshold_seconds=settings.quiet_threshold_seconds,
                heartbeat_seconds=settings.heartbeat_seconds,
            )
        self.tracker = tracker
        self.started_rendered = False
        self.session_rendered = False

    def __call__(self, event: ExecutionEvent) -> None:
        state = self.tracker.observe(event)
        self.recorder.record(event, state)
        if self.json_mode:
            emit_json({"run_id": self.recorder.run_id, "phase": self.recorder.phase_id, **event.to_dict(), "state": state.value})
            return
        if self.console.quiet:
            return
        self._render(event, state)

    def set_profile(self, profile: StartupProfile) -> None:
        self.recorder.profile(profile)

    def finish(self, *, success: bool, status: ExecutionState | None = None) -> None:
        state = status or (ExecutionState.COMPLETED if success else ExecutionState.ERROR)
        self.recorder.finish(status=state, elapsed_seconds=self.tracker.elapsed())

    def _render(self, event: ExecutionEvent, state: ExecutionState) -> None:
        if event.type is ExecutionEventType.PROCESS_STARTED:
            self.console.line(f"{self.console.style(SUCCESS, '32')} Codex process started")
            self.started_rendered = True
        elif event.type is ExecutionEventType.SESSION_STARTED:
            self.console.line(f"{self.console.style(SUCCESS, '32')} Session initialized")
            if event.session_id:
                self.console.field("Session", f"{event.session_id[:8]}…")
            self.console.line()
            self.console.subsection(self.role.upper())
            self.console.line()
            self.console.line(f"{self.console.style(ACTIVE, '36')} {self.role.title()} active")
            self._elapsed()
            self.session_rendered = True
        elif event.type is ExecutionEventType.COMMAND_STARTED:
            self.console.line()
            self.console.line(f"{self.console.style(ACTIVE, '36')} Running command")
            self.console.wrapped(event.command or "Codex command", 2)
            self._elapsed()
        elif event.type is ExecutionEventType.COMMAND_COMPLETED:
            marker = SUCCESS if event.exit_code in {0, None} else ERROR
            color = "32" if marker == SUCCESS else "31"
            duration = _command_duration(event.duration_ms)
            suffix = f" · {duration}" if duration else ""
            self.console.line(f"{self.console.style(marker, color)} {event.command or 'Command'}{suffix}")
            if event.exit_code not in {0, None}:
                self.console.field("Exit code", event.exit_code)
        elif event.type is ExecutionEventType.FILE_CHANGED:
            counts = {"add": 0, "modify": 0, "delete": 0}
            for change in event.files:
                kind = change.get("kind", "modify")
                counts[kind if kind in counts else "modify"] += 1
            parts = []
            if counts["modify"]:
                parts.append(f"{counts['modify']} modified")
            if counts["add"]:
                parts.append(f"{counts['add']} created")
            if counts["delete"]:
                parts.append(f"{counts['delete']} removed")
            if parts:
                self.console.field("Files", " · ".join(parts))
            if self.verbose:
                for change in event.files[-5:]:
                    self.console.wrapped(f"{PENDING} {change['path']}", 4)
        elif event.type is ExecutionEventType.VALIDATION_STARTED:
            self.console.line()
            self.console.subsection("Validation")
            self.console.line(f"{self.console.style(ACTIVE, '36')} Running deterministic validation")
        elif event.type is ExecutionEventType.VALIDATION_COMPLETED:
            self.console.line(f"{self.console.style(SUCCESS, '32')} {event.summary or 'Validation passed'}")
        elif event.type is ExecutionEventType.REVIEW_STARTED:
            self.console.line()
            self.console.subsection("Review")
            self.console.line(f"{self.console.style(ACTIVE, '36')} Running independent reviewer")
        elif event.type is ExecutionEventType.REVIEW_COMPLETED:
            approved = event.status == "APPROVE"
            marker, color = (SUCCESS, "32") if approved else (WARNING, "33")
            self.console.line(f"{self.console.style(marker, color)} {event.summary or 'Review completed'}")
        elif event.type is ExecutionEventType.GATE_CREATED:
            self.console.line()
            self.console.subsection("Gate")
            self.console.line(f"{self.console.style(SUCCESS, '32')} {event.summary or 'Approval gate created'}")
        elif event.type is ExecutionEventType.PHASE_ADVANCED:
            self.console.line(f"{self.console.style(SUCCESS, '32')} {event.summary or 'Workflow advanced'}")
        elif event.type is ExecutionEventType.HEARTBEAT:
            activity = f"Running {self.tracker.current_command}" if self.tracker.current_command else "Codex still working"
            self.console.line(f"{self.console.style(PENDING, '2')} {activity} · {_duration(event.elapsed_seconds)}")
        elif event.type is ExecutionEventType.QUIET_WARNING:
            self.console.line()
            self.console.line(f"{self.console.style(WARNING, '33')} {event.summary or 'Codex activity is quiet'}")
            self.console.field("Process", "still running")
            self.console.field("Elapsed", _duration(event.elapsed_seconds))
            self.console.wrapped("CW has not terminated the session.", 2)
        elif event.type is ExecutionEventType.PROCESS_COMPLETED:
            marker = SUCCESS if event.exit_code == 0 else ERROR
            color = "32" if marker == SUCCESS else "31"
            label = f"{self.role.title()} completed" if event.exit_code == 0 else f"{self.role.title()} stopped"
            self.console.line()
            self.console.line(f"{self.console.style(marker, color)} {label}")
            self.console.field("Elapsed", _duration(event.elapsed_seconds or self.tracker.elapsed()))
        elif event.type is ExecutionEventType.ERROR:
            self.console.line(f"{self.console.style(ERROR, '31')} Codex reported an execution error")
        elif event.type is ExecutionEventType.STOP_REQUESTED:
            self.console.line()
            self.console.line(f"{self.console.style(WARNING, '33')} Stop requested")
            self.console.wrapped("CW will not start another operation. Preserving workflow state…", 2)
        if self.verbose and event.source_type:
            self.console.wrapped(f"[{event.timestamp}] {event.source_type} → {state.value}", 2)

    def _elapsed(self) -> None:
        self.console.field("Elapsed", _duration(self.tracker.elapsed()))


def render_performance(console: Console, run: dict[str, Any] | None) -> None:
    console.header("Performance")
    if not run:
        console.line(f"  {PENDING} No managed execution profile recorded")
        return
    console.subsection("Startup")
    profile = run.get("profile") if isinstance(run.get("profile"), dict) else {}
    labels = (
        ("CW preflight", "preflight_ms"),
        ("Codex spawn", "spawn_ms"),
        ("Session initialization", "session_init_ms"),
        ("First activity", "first_event_ms"),
    )
    shown = 0
    for label, key in labels:
        value = profile.get(key)
        if not isinstance(value, int):
            continue
        shown += 1
        marker = WARNING if key in {"session_init_ms", "first_event_ms"} and value >= 10_000 else SUCCESS
        color = "33" if marker == WARNING else "32"
        rendered = f"{value}ms" if value < 1000 else f"{value / 1000:.1f}s"
        console.line(f"  {console.style(marker, color)} {label.ljust(24)} {rendered}")
    if not shown:
        console.line(f"  {PENDING} Startup timings unavailable")
    console.line()
    console.field("Run", run.get("run_id", "unknown"))
    console.field("Phase", run.get("phase", "unknown"))
    console.field("State", run.get("status", "unknown"))


def render_processes(console: Console, run: dict[str, Any] | None, *, process_alive: bool) -> None:
    console.header("Processes")
    if not run:
        console.item(PENDING, "No CW-managed process recorded")
        return
    if run.get("finished_at"):
        console.item(SUCCESS, "Latest CW-managed run completed")
    elif process_alive:
        console.item(SUCCESS, "Current CW-managed Codex process")
    else:
        console.item(WARNING, "Stale CW-managed execution metadata")
    console.field("Run", run.get("run_id", "unknown"))
    console.field("Phase", run.get("phase", "unknown"))
    if run.get("process_pid"):
        console.field("PID", run["process_pid"])
    console.field("State", run.get("status", "unknown"))
