from __future__ import annotations

import io
import json
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cw.core.severity import CriterionSeverity
from cw.ui.console import Console, emit_json
from cw.ui.progress import progress_bar, progress_percentage
from cw.ui.renderers import (
    render_doctor,
    render_history,
    render_review_result,
    render_status,
)


FIXTURES = Path(__file__).parent / "fixtures" / "ui"


class Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


def status_data() -> dict:
    return {
        "project": "sample-app",
        "repository_root": "/tmp/sample-app",
        "branch": "dev",
        "workflow": "ACTIVE",
        "plan": "APPROVED",
        "state": "IN_PROGRESS",
        "phase": "02-build",
        "phase_index": 1,
        "position": 2,
        "phase_count": 3,
        "approved_count": 1,
        "attempt": 0,
        "max_attempts": 3,
        "ready": False,
        "gate": False,
        "gates": {"01-assess": True, "02-build": False, "03-release": False},
        "gate_states": {"01-assess": "approved", "02-build": "pending", "03-release": "pending"},
        "invalid_gates": [],
        "gate_error": None,
        "phases": [
            {"id": "01-assess", "number": "01", "name": "Repository Assessment", "objective": "Assess."},
            {"id": "02-build", "number": "02", "name": "Build Authentication", "objective": "Implement secure authentication."},
            {"id": "03-release", "number": "03", "name": "Release Verification", "objective": "Release."},
        ],
        "last_error": None,
        "infrastructure_error": None,
    }


def render(renderer, *args, width: int = 64, tty: bool = False, **kwargs) -> str:
    stream = Tty() if tty else io.StringIO()
    renderer(Console(stream=stream, no_color=not tty, width_override=width), *args, **kwargs)
    return stream.getvalue()


class ProgressRenderingTests(unittest.TestCase):
    def test_percentage_clamps_and_handles_empty(self) -> None:
        self.assertEqual(0, progress_percentage(0, 0))
        self.assertEqual(67, progress_percentage(6, 9))
        self.assertEqual(100, progress_percentage(12, 9))

    def test_bar_zero_mid_and_complete(self) -> None:
        zero = progress_bar(0, 4, 8, unicode=False)
        middle = progress_bar(2, 4, 8, unicode=False)
        complete = progress_bar(4, 4, 8, unicode=False)
        self.assertEqual("--------", zero.remaining)
        self.assertEqual(("####", "----", 50), (middle.complete, middle.remaining, middle.percentage))
        self.assertEqual(("########", "", 100), (complete.complete, complete.remaining, complete.percentage))

    def test_console_width_is_narrow_aware_and_wide_capped(self) -> None:
        self.assertEqual(36, Console(stream=io.StringIO(), width_override=36).width)
        self.assertEqual(88, Console(stream=io.StringIO(), width_override=200).width)

    def test_narrow_status_does_not_overflow(self) -> None:
        output = render(render_status, status_data(), width=40)
        self.assertLessEqual(max(map(len, output.splitlines())), 40)
        self.assertIn("CURRENT PHASE", output)


class SemanticRenderingTests(unittest.TestCase):
    def test_status_has_hierarchy_markers_progress_and_actions(self) -> None:
        output = render(render_status, status_data())
        for token in ("WORKFLOW", "Progress", "33%", "CURRENT PHASE", "→ 02 ·", "DEVELOPMENT PLAN", "✓ 01", "· 03", "cw validate"):
            self.assertIn(token, output)

    def test_current_phase_is_visually_separated(self) -> None:
        output = render(render_status, status_data())
        self.assertIn("\n  → 02 · Build Authentication\n\n", output)
        self.assertIn("\n    → 02  Build Authentication\n\n", output)

    def test_completed_workflow_is_satisfying_and_exact(self) -> None:
        data = status_data()
        data.update(state="COMPLETED", phase="03-release", phase_index=2, position=3, approved_count=3, gate=True)
        data["gate_states"] = {phase["id"]: "approved" for phase in data["phases"]}
        output = render(render_status, data)
        self.assertIn("✓ WORKFLOW COMPLETE", output)
        self.assertIn("100%", output)
        self.assertIn("All configured gates are valid.", output)

    def test_error_and_human_states_have_contextual_actions(self) -> None:
        error = status_data()
        error.update(state="ERROR", last_error="REVIEWER_NETWORK_ERROR: offline", infrastructure_error={"error_code": "REVIEWER_NETWORK_ERROR", "retryable": True})
        error_output = render(render_status, error)
        self.assertIn("WORKFLOW BLOCKED", error_output)
        self.assertIn("cw retry", error_output)
        self.assertNotIn("Traceback", error_output)
        warning = status_data()
        warning["state"] = "HUMAN_REVIEW_REQUIRED"
        warning_output = render(render_status, warning)
        self.assertIn("HUMAN REVIEW REQUIRED", warning_output)
        self.assertIn("cw review --human-approve", warning_output)

    def test_invalid_gate_uses_attention_marker(self) -> None:
        data = status_data()
        data["gate_states"]["01-assess"] = "invalid"
        data["gate_error"] = "hash mismatch"
        output = render(render_status, data)
        self.assertIn("! 01", output)
        self.assertIn("Approval gate invalidated", output)

    def test_verbose_adds_paths_normal_omits_them(self) -> None:
        normal = render(render_status, status_data())
        verbose = render(render_status, status_data(), verbose=True)
        self.assertNotIn("/tmp/sample-app", normal)
        self.assertIn("/tmp/sample-app", verbose)
        self.assertIn(".codex/workflow/phases.yaml", verbose)

    def test_ansi_tty_no_color_and_non_tty(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            colored = render(render_status, status_data(), tty=True)
        self.assertIn("\x1b[", colored)
        plain = render(render_status, status_data())
        self.assertNotIn("\x1b[", plain)
        with patch.dict(os.environ, {"NO_COLOR": "1"}):
            stream = Tty()
            render_status(Console(stream=stream, width_override=64), status_data())
        self.assertNotIn("\x1b[", stream.getvalue())

    def test_json_is_independent_and_ansi_free(self) -> None:
        stream = io.StringIO()
        emit_json(status_data(), stream)
        payload = json.loads(stream.getvalue())
        self.assertEqual("02-build", payload["phase"])
        self.assertNotIn("\x1b[", stream.getvalue())


class CommandViewTests(unittest.TestCase):
    def test_doctor_healthy_and_warning(self) -> None:
        checks = [
            {"section": "Environment", "name": "Git", "status": "pass", "detail": "/usr/bin/git"},
            {"section": "Security", "name": "Hook trust", "status": "warning", "detail": "review required"},
        ]
        output = render(render_doctor, checks, {"passed": 1, "warnings": 1, "errors": 0})
        self.assertIn("Environment", output)
        self.assertIn("Healthy with warnings", output)
        self.assertIn("1 warnings", output)

    def test_history_is_an_audit_timeline(self) -> None:
        phases = [{
            "phase": "01-assess", "number": "01", "name": "Repository Assessment",
            "approved": True, "current": False,
            "entries": [
                {"kind": "infrastructure_failure_recovered", "attempt": None, "timestamp": None},
                {"kind": "approved", "attempt": 1, "timestamp": None},
            ],
        }]
        output = render(render_history, phases)
        self.assertIn("✓ 01 · Repository Assessment", output)
        self.assertIn("Infrastructure failure recovered", output)
        self.assertIn("Approved · attempt 1", output)

    def test_review_approve_and_revise(self) -> None:
        phase = SimpleNamespace(
            id="02-build", name="Build Authentication", requires_human_approval=False,
            acceptance_criteria=[SimpleNamespace(id="AUTH-001", severity=CriterionSeverity.BLOCKING)],
        )
        workflow = SimpleNamespace(phase=lambda _: SimpleNamespace(id="03-release", name="Release Verification"))
        approved = render(render_review_result, phase, {"decision": "APPROVE", "criteria": [], "next_phase": "03-release"}, workflow)
        revised = render(render_review_result, phase, {"decision": "REVISE", "blocking_issues": ["AUTH-001 Authentication tests are incomplete."]}, workflow)
        self.assertIn("✓ APPROVED", approved)
        self.assertIn("Next", approved)
        self.assertIn("✕ REVISION REQUIRED", revised)
        self.assertIn("1 blocking", revised)
        self.assertIn("cw", revised)


class GoldenFixtureTests(unittest.TestCase):
    def test_representative_colorless_golden_outputs(self) -> None:
        data = status_data()
        initialized = status_data()
        initialized.update(
            project="clean-project", state="INITIALIZED", phase=None,
            phase_index=None, position=None, phase_count=0, approved_count=0,
            phases=[], gates={}, gate_states={},
        )
        completed = status_data()
        completed.update(state="COMPLETED", phase="03-release", phase_index=2, position=3, approved_count=3, gate=True)
        completed["gate_states"] = {phase["id"]: "approved" for phase in completed["phases"]}
        error = status_data()
        error.update(state="ERROR", last_error="REVIEWER_NETWORK_ERROR: offline", infrastructure_error={"error_code": "REVIEWER_NETWORK_ERROR", "retryable": True})
        checks = [
            {"section": "Environment", "name": "Git", "status": "pass", "detail": "/usr/bin/git"},
            {"section": "Environment", "name": "Python", "status": "pass", "detail": "/usr/bin/python3"},
            {"section": "Workflow", "name": "Project identity", "status": "pass", "detail": "sample-app"},
            {"section": "Security", "name": "Runtime writable", "status": "pass", "detail": "required"},
        ]
        history = [
            {"phase": "01-assess", "number": "01", "name": "Repository Assessment", "approved": True, "current": False, "entries": [{"kind": "approved", "attempt": 1, "timestamp": None}]},
            {"phase": "02-build", "number": "02", "name": "Build Authentication", "approved": False, "current": True, "entries": [{"kind": "current", "attempt": 0, "timestamp": None}]},
        ]
        phase = SimpleNamespace(id="02-build", name="Build Authentication", requires_human_approval=False, acceptance_criteria=[SimpleNamespace(id="AUTH-001", severity=CriterionSeverity.BLOCKING)])
        workflow = SimpleNamespace(phase=lambda _: SimpleNamespace(id="03-release", name="Release Verification"))
        cases = {
            "status-initialized.txt": render(render_status, initialized),
            "status-active.txt": render(render_status, data),
            "status-error.txt": render(render_status, error),
            "status-completed.txt": render(render_status, completed),
            "doctor-healthy.txt": render(render_doctor, checks, {"passed": 4, "warnings": 0, "errors": 0}),
            "history.txt": render(render_history, history),
            "review-approved.txt": render(render_review_result, phase, {"decision": "APPROVE", "criteria": [], "next_phase": "03-release"}, workflow),
            "review-revise.txt": render(render_review_result, phase, {"decision": "REVISE", "blocking_issues": ["AUTH-001 Authentication tests are incomplete."]}, workflow),
        }
        for name, actual in cases.items():
            with self.subTest(name=name):
                self.assertEqual((FIXTURES / name).read_text(encoding="utf-8"), actual)


if __name__ == "__main__":
    unittest.main()
