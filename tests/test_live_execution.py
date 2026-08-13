from __future__ import annotations

import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cw.adapters.codex import CodexAdapter
from cw.execution.context import execution_event_sink
from cw.execution.events import (
    CodexEventParser,
    ExecutionEvent,
    ExecutionEventType,
    ExecutionState,
    ExecutionTracker,
    StartupProfile,
    display_command,
)
from cw.execution.processes import ProcessInspector
from cw.execution.runs import (
    RunRecorder,
    archive_interrupted_run,
    latest_run,
    load_active_run,
    load_run_events,
    new_run_id,
)
from cw.ui.console import Console
from cw.ui.live import LiveExecutionObserver, render_performance


class FakeClock:
    def __init__(self) -> None:
        self.value = 100.0

    def monotonic(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class CodexEventParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path("/tmp/project")
        self.clock = FakeClock()
        self.parser = CodexEventParser(self.root, clock=self.clock)

    def event(self, value: dict) -> ExecutionEvent | None:
        return self.parser.parse_line(json.dumps(value))

    def test_session_turn_command_and_usage_events_match_observed_jsonl(self):
        session = self.event({"type": "thread.started", "thread_id": "abc"})
        turn = self.event({"type": "turn.started"})
        started = self.event({
            "type": "item.started",
            "item": {"id": "item_1", "type": "command_execution", "command": "/usr/bin/zsh -lc 'composer check'", "status": "in_progress"},
        })
        self.clock.advance(3.25)
        completed = self.event({
            "type": "item.completed",
            "item": {"id": "item_1", "type": "command_execution", "command": "/usr/bin/zsh -lc 'composer check'", "exit_code": 0, "status": "completed"},
        })
        usage = self.event({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2}})
        self.assertEqual(ExecutionEventType.SESSION_STARTED, session.type)
        self.assertEqual(ExecutionEventType.TURN_STARTED, turn.type)
        self.assertEqual("composer check", started.command)
        self.assertEqual(ExecutionEventType.COMMAND_COMPLETED, completed.type)
        self.assertEqual(3250, completed.duration_ms)
        self.assertEqual({"input_tokens": 10, "output_tokens": 2}, usage.usage)

    def test_file_changes_are_project_relative_and_aggregated(self):
        event = self.event({
            "type": "item.completed",
            "item": {
                "id": "item_2", "type": "file_change", "status": "completed",
                "changes": [
                    {"path": "/tmp/project/src/Refund.php", "kind": "modify"},
                    {"path": "/tmp/project/tests/RefundTest.php", "kind": "add"},
                ],
            },
        })
        self.assertEqual(ExecutionEventType.FILE_CHANGED, event.type)
        self.assertEqual("src/Refund.php", event.files[0]["path"])
        self.assertEqual("tests/RefundTest.php", event.files[1]["path"])

    def test_reasoning_and_agent_messages_are_never_exposed(self):
        for item_type in ("reasoning", "agent_message"):
            event = self.event({"type": "item.completed", "item": {"id": item_type, "type": item_type, "text": "private reasoning"}})
            self.assertIsNone(event)

    def test_unknown_malformed_and_duplicate_events_are_safe(self):
        malformed = self.parser.parse_line("not-json")
        unknown = self.event({"type": "future.event", "secret": "ignored"})
        duplicate = self.event({"type": "future.event", "secret": "different"})
        self.assertEqual(ExecutionEventType.WARNING, malformed.type)
        self.assertEqual(ExecutionEventType.UNKNOWN, unknown.type)
        self.assertIsNone(duplicate)
        self.assertNotIn("secret", json.dumps(unknown.to_dict()))

    def test_command_display_is_redacted_and_shell_wrapper_is_removed(self):
        self.assertEqual("composer check", display_command("/usr/bin/zsh -lc 'composer check'"))
        self.assertNotIn("secret-value", display_command("tool --token=secret-value"))
        self.assertTrue(display_command("x" * 600).endswith("..."))


class ExecutionTrackerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.tracker = ExecutionTracker(clock=self.clock, quiet_threshold_seconds=90, heartbeat_seconds=60)

    def test_state_transitions_and_command_silence(self):
        self.assertEqual(ExecutionState.SESSION_INITIALIZING, self.tracker.observe(ExecutionEvent(ExecutionEventType.PROCESS_STARTED)))
        self.assertEqual(ExecutionState.IMPLEMENTING, self.tracker.observe(ExecutionEvent(ExecutionEventType.SESSION_STARTED)))
        self.assertEqual(ExecutionState.RUNNING_COMMAND, self.tracker.observe(ExecutionEvent(ExecutionEventType.COMMAND_STARTED, command="composer check")))
        self.clock.advance(95)
        heartbeat = self.tracker.poll(process_alive=True)
        self.assertEqual(ExecutionEventType.HEARTBEAT, heartbeat.type)
        self.assertNotEqual(ExecutionEventType.QUIET_WARNING, heartbeat.type)
        self.assertEqual(ExecutionState.IMPLEMENTING, self.tracker.observe(ExecutionEvent(ExecutionEventType.COMMAND_COMPLETED, exit_code=0)))

    def test_quiet_process_warns_once_and_dead_process_does_not(self):
        self.tracker.observe(ExecutionEvent(ExecutionEventType.SESSION_STARTED))
        self.clock.advance(91)
        warning = self.tracker.poll(process_alive=True)
        self.assertEqual(ExecutionEventType.QUIET_WARNING, warning.type)
        self.assertIsNone(self.tracker.poll(process_alive=True))
        self.clock.advance(100)
        self.assertIsNone(self.tracker.poll(process_alive=False))

    def test_heartbeat_is_throttled(self):
        self.tracker.observe(ExecutionEvent(ExecutionEventType.SESSION_STARTED))
        self.clock.advance(61)
        self.assertEqual(ExecutionEventType.HEARTBEAT, self.tracker.poll(process_alive=True).type)
        self.assertIsNone(self.tracker.poll(process_alive=True))

    def test_nonzero_process_exit_is_error(self):
        state = self.tracker.observe(ExecutionEvent(
            ExecutionEventType.PROCESS_COMPLETED,
            exit_code=1,
        ))
        self.assertEqual(ExecutionState.ERROR, state)

    def test_turn_completion_returns_to_implementation(self):
        self.tracker.observe(ExecutionEvent(ExecutionEventType.FILE_CHANGED))
        state = self.tracker.observe(ExecutionEvent(ExecutionEventType.TURN_COMPLETED))
        self.assertEqual(ExecutionState.IMPLEMENTING, state)


class LiveRenderingAndPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="cw-live-")
        self.root = Path(self.temporary.name)
        for directory in (".cw/runtime", ".cw/logs"):
            (self.root / directory).mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def observer(self, *, quiet: bool = False, verbose: bool = False):
        stream = io.StringIO()
        console = Console(stream=stream, no_color=True, quiet=quiet)
        recorder = RunRecorder(self.root, run_id=new_run_id(), phase_id="10-refunds", role="implementation")
        return stream, recorder, LiveExecutionObserver(console, recorder, role="implementation", verbose=verbose)

    def test_startup_transitions_immediately_to_real_activity(self):
        stream, recorder, observer = self.observer()
        stream.write("→ Starting Codex session…\n")
        observer(ExecutionEvent(ExecutionEventType.PROCESS_STARTED, process_id=123))
        observer(ExecutionEvent(ExecutionEventType.SESSION_STARTED, session_id="bd257f16-1234"))
        observer(ExecutionEvent(ExecutionEventType.COMMAND_STARTED, command="composer check"))
        output = stream.getvalue()
        self.assertLess(output.index("Starting Codex"), output.index("Codex process started"))
        self.assertIn("Session initialized", output)
        self.assertIn("Running command", output)
        self.assertIn("composer check", output)
        observer.finish(success=True)
        self.assertFalse((self.root / ".cw/runtime/active-run.json").exists())
        self.assertEqual("COMPLETED", latest_run(self.root)["status"])

    def test_files_elapsed_verbose_and_no_reasoning(self):
        stream, recorder, observer = self.observer(verbose=True)
        observer(ExecutionEvent(ExecutionEventType.FILE_CHANGED, source_type="item.completed", files=(
            {"path": "src/Refund.php", "kind": "modify"},
            {"path": "tests/RefundTest.php", "kind": "add"},
        )))
        output = stream.getvalue()
        self.assertIn("1 modified · 1 created", output)
        self.assertIn("src/Refund.php", output)
        self.assertNotIn("reasoning", output.lower())
        observer.finish(success=True)

    def test_quiet_mode_persists_without_rendering(self):
        stream, recorder, observer = self.observer(quiet=True)
        observer(ExecutionEvent(ExecutionEventType.COMMAND_STARTED, command="composer check"))
        observer.finish(success=True)
        self.assertEqual("", stream.getvalue())
        events = load_run_events(self.root, recorder.run_id)
        self.assertEqual("COMMAND_STARTED", events[0]["event_type"])

    def test_performance_omits_unmeasured_fields(self):
        stream = io.StringIO()
        render_performance(Console(stream=stream, no_color=True), {
            "run_id": "run_x", "phase": "10-refunds", "status": "COMPLETED",
            "profile": {"spawn_ms": 44, "session_init_ms": 1800},
        })
        output = stream.getvalue()
        self.assertIn("Codex spawn", output)
        self.assertIn("Session initialization", output)
        self.assertNotIn("CW preflight", output)

    def test_interrupted_run_is_archived_without_losing_events(self):
        stream, recorder, observer = self.observer(quiet=True)
        observer(ExecutionEvent(ExecutionEventType.SESSION_STARTED, session_id="session-1"))
        active = load_active_run(self.root)
        archive = archive_interrupted_run(self.root, active)
        self.assertFalse((self.root / ".cw/runtime/active-run.json").exists())
        self.assertEqual("INTERRUPTED", json.loads(archive.read_text())["status"])
        self.assertEqual("SESSION_STARTED", load_run_events(self.root, recorder.run_id)[0]["event_type"])


class ProcessAbstractionTests(unittest.TestCase):
    def test_common_core_uses_no_proc_and_supports_platform_labels(self):
        for platform in ("posix-linux", "posix-macos", "nt"):
            inspector = ProcessInspector(platform=platform, signaler=lambda _pid, _signal: None)
            status = inspector.inspect(42)
            self.assertTrue(status.alive)
            self.assertEqual(platform, status.platform)

    def test_missing_process_is_not_alive(self):
        def missing(_pid: int, _signal: int) -> None:
            raise ProcessLookupError

        self.assertFalse(ProcessInspector(signaler=missing).inspect(42).alive)


class StreamingAdapterTests(unittest.TestCase):
    class FakePopen:
        def __init__(self, command, **_kwargs):
            self.command = command
            self.pid = 777
            self.stdout = io.StringIO("\n".join((
                json.dumps({"type": "thread.started", "thread_id": "session-1"}),
                json.dumps({"type": "turn.started"}),
                json.dumps({"type": "item.started", "item": {"id": "c1", "type": "command_execution", "command": "/bin/sh -lc 'composer check'", "status": "in_progress"}}),
                json.dumps({"type": "item.completed", "item": {"id": "c1", "type": "command_execution", "command": "/bin/sh -lc 'composer check'", "exit_code": 0, "status": "completed"}}),
                json.dumps({"type": "turn.completed", "usage": {"input_tokens": 4, "output_tokens": 2}}),
            )) + "\n")
            self.stderr = io.StringIO("MCP client for `vercel` failed to start\nAuthRequired invalid_token\n")
            self.returncode = None

        def poll(self):
            if self.stdout.tell() == len(self.stdout.getvalue()) and self.stderr.tell() == len(self.stderr.getvalue()):
                self.returncode = 0
            return self.returncode

        def wait(self, timeout=None):
            self.returncode = 0
            return 0

        def terminate(self):
            self.returncode = -15

        def kill(self):
            self.returncode = -9

    def test_implementer_streams_actual_event_shape_and_optional_noise_does_not_fail(self):
        with tempfile.TemporaryDirectory(prefix="cw-stream-") as temporary:
            root = Path(temporary)
            (root / ".cw/logs").mkdir(parents=True)
            events: list[ExecutionEvent] = []
            config_ok = subprocess.CompletedProcess([], 0, "{}", "")
            with patch("cw.adapters.codex.shutil.which", return_value="/usr/bin/codex"), patch(
                "cw.adapters.codex.subprocess.run", return_value=config_ok,
            ), patch("cw.adapters.codex.subprocess.Popen", self.FakePopen):
                with execution_event_sink(events.append):
                    result = CodexAdapter().run_implementer(root, "work", session_id="a" * 32)
            self.assertEqual(0, result.exit_code)
            self.assertEqual("session-1", result.session_id)
            self.assertTrue(result.integration_diagnostics)
            self.assertIn(ExecutionEventType.PROCESS_STARTED, [event.type for event in events])
            self.assertIn(ExecutionEventType.COMMAND_STARTED, [event.type for event in events])
            self.assertIn(ExecutionEventType.PROCESS_COMPLETED, [event.type for event in events])
            invocation = (root / ".cw/logs/codex-invocations.jsonl").read_text(encoding="utf-8")
            self.assertIn("--json", invocation)
            self.assertNotIn("mcp_servers.", invocation)


if __name__ == "__main__":
    unittest.main()
