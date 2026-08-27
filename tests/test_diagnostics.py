from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from cw.cli.main import _record_error, main
from cw.core.diagnostics import (
    correlation_id,
    load_diagnostic,
    load_global_diagnostic,
    record_diagnostic,
    record_global_diagnostic,
)
from cw.core.errors import CwError, ErrorCode
from cw.core.gates import create_gate
from tests.helpers import TempRepo


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.repo = TempRepo()
        self.previous = Path.cwd()
        os.chdir(self.repo.root)

    def tearDown(self):
        os.chdir(self.previous)
        self.repo.close()

    def invoke(self, *args):
        stream = io.StringIO()
        with redirect_stdout(stream):
            code = main(args)
        return code, stream.getvalue()

    def test_diagnostic_redacts_common_credentials(self):
        error = CwError(
            "transport failed", ErrorCode.REVIEWER_NETWORK_ERROR,
            details=(
                "Authorization: Bearer bearer-secret api_key=key-secret "
                "ghp_1234567890abcdef https://person:password@example.test/path"
            ),
        )
        record = record_diagnostic(self.repo.root, error, source="review")
        serialized = json.dumps(record)
        for secret in ("bearer-secret", "key-secret", "ghp_1234567890abcdef", "password"):
            self.assertNotIn(secret, serialized)
        self.assertIn("[REDACTED]", serialized)

    def test_error_survives_corrupt_workflow_state(self):
        record_diagnostic(
            self.repo.root,
            CwError("review failed", ErrorCode.REVIEWER_PROCESS_ERROR, details="full diagnostic"),
            source="review",
        )
        (self.repo.root / ".cw/state.json").write_text("{", encoding="utf-8")
        code, output = self.invoke("error")
        self.assertEqual(1, code)
        self.assertIn("full diagnostic", output)
        self.assertIn("REVIEWER_PROCESS_ERROR", output)

    def test_raw_includes_stored_traceback_but_normal_output_does_not(self):
        with patch("cw.cli.main.command_status", side_effect=ValueError("simulated defect")), patch.dict(
            "cw.cli.main.COMMANDS", {"status": __import__("cw.cli.main", fromlist=["command_status"]).command_status}
        ):
            code, output = self.invoke("status")
        self.assertEqual(1, code)
        self.assertIn("internal error", output)
        self.assertNotIn("Traceback", output)

        code, raw = self.invoke("error", "--raw")
        self.assertEqual(1, code)
        self.assertIn("Traceback", raw)
        self.assertIn("ValueError: simulated defect", raw)

    def test_error_json_is_structured_and_has_no_ansi(self):
        record_diagnostic(self.repo.root, CwError("bad gate", ErrorCode.INVALID_GATE), source="status")
        code, output = self.invoke("error", "--json")
        self.assertEqual(1, code)
        payload = json.loads(output)
        self.assertEqual("INVALID_GATE", payload["error"]["code"])
        self.assertNotIn("\033[", output)

    def test_validation_failure_is_available_to_error_command(self):
        code, _ = self.invoke("validate", "--json")
        self.assertEqual(1, code)
        record = load_diagnostic(self.repo.root)
        self.assertEqual("WORKFLOW_ERROR", record["code"])
        self.assertIn("Readiness manifest", record["details"])

    def test_workflow_state_error_is_redacted(self):
        failure = CwError(
            "implementer failed", ErrorCode.IMPLEMENTER_PROCESS_ERROR,
            details="Authorization: Bearer top-secret-token",
        )
        with patch("cw.cli.main.CodexAdapter.run_implementer", side_effect=failure):
            code, _ = self.invoke("start")
        self.assertEqual(1, code)
        state = json.loads((self.repo.root / ".cw/state.json").read_text(encoding="utf-8"))
        self.assertNotIn("top-secret-token", state["last_error"])
        self.assertIn("[REDACTED]", state["last_error"])

    def test_quiet_internal_error_is_silent_but_recorded(self):
        with patch("cw.cli.main.COMMANDS", {"status": lambda *_: 1 / 0}):
            code, output = self.invoke("status", "--quiet")
        self.assertEqual(1, code)
        self.assertEqual("", output)
        self.assertEqual("INTERNAL_ERROR", load_diagnostic(self.repo.root)["code"])

    def test_status_persists_invalid_gate_diagnostic(self):
        self.repo.artifact()
        review = self.repo.approved_review()
        create_gate(self.repo.root, self.repo.workflow, self.repo.workflow.phases[0], review)
        self.repo.artifact(content="changed")
        code, output = self.invoke("status")
        self.assertEqual(1, code)
        self.assertIn("Approval gate invalidated", output)
        record = load_diagnostic(self.repo.root)
        self.assertIsNotNone(record)
        self.assertEqual("INVALID_GATE", record["code"])

    def test_duplicate_diagnostic_is_not_appended_twice(self):
        error = CwError("same failure", ErrorCode.INVALID_STATE)
        record_diagnostic(self.repo.root, error, source="status")
        record_diagnostic(self.repo.root, error, source="status")
        history = (self.repo.root / ".cw/logs/errors.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(1, len(history))

    def test_machine_internal_error_uses_one_correlation_id_in_envelope_and_records(self):
        goal_canary = "goal-private-canary"
        token_canary = "token-private-canary"
        with patch("cw.cli.main.COMMANDS", {"plan": lambda *_: (_ for _ in ()).throw(
            RuntimeError(f"{goal_canary} {token_canary}"),
        )}):
            code, output = self.invoke("plan", "--goal", goal_canary, "--output=json")

        self.assertEqual(1, code)
        self.assertEqual(1, len(output.splitlines()))
        payload = json.loads(output)
        error = payload["error"]
        self.assertEqual("INTERNAL_ERROR", error["code"])
        self.assertEqual("Unexpected internal failure", error["message"])
        self.assertRegex(error["correlation_id"], r"^[0-9a-f]{16}$")
        self.assertEqual(
            correlation_id("plan", "INTERNAL_ERROR", "Unexpected internal failure"),
            error["correlation_id"],
        )
        record = load_diagnostic(self.repo.root)
        self.assertIsNotNone(record)
        self.assertEqual(error["correlation_id"], record["correlation_id"])
        history = [json.loads(line) for line in (self.repo.root / ".cw/logs/errors.jsonl").read_text(
            encoding="utf-8",
        ).splitlines()]
        self.assertEqual(error["correlation_id"], history[-1]["correlation_id"])
        self.assertNotIn(goal_canary, output)
        self.assertNotIn(token_canary, output)
        self.assertNotIn("RuntimeError", output)
        self.assertNotIn("Traceback", output)

    def test_legacy_diagnostic_without_correlation_id_remains_readable(self):
        error = CwError("legacy failure", ErrorCode.INVALID_STATE)
        record_diagnostic(self.repo.root, error, source="status")
        record = load_diagnostic(self.repo.root)
        self.assertIsNotNone(record)
        self.assertNotIn("correlation_id", record)

    def test_global_diagnostic_persists_safe_traceback_without_private_fields(self):
        safe_traceback = {
            "version": 1,
            "exception_type": "OSError",
            "frames": [{"module": "cw.checks.verification", "function": "_safe_runtime", "line": 91}],
        }
        canaries = "GOAL_PRIVATE_CANARY TOKEN_PRIVATE_CANARY STDOUT_PRIVATE_CANARY STDERR_PRIVATE_CANARY --goal C:\\Users\\runner /home/operator Authorization: Bearer api_key password secret"
        with tempfile.TemporaryDirectory() as temporary, patch(
            "cw.core.diagnostics.global_config_dir", return_value=Path(temporary)
        ):
            record = record_global_diagnostic(
                CwError("global diagnostic", ErrorCode.INVALID_STATE, details=canaries),
                source="update",
                safe_traceback=safe_traceback,
            )
            loaded = load_global_diagnostic()

        self.assertEqual(safe_traceback, record["safe_traceback"])
        assert loaded is not None
        self.assertEqual(safe_traceback, loaded["safe_traceback"])
        self.assertEqual("INVALID_STATE", loaded["code"])
        serialized = json.dumps(loaded["safe_traceback"])
        for canary in (
            "GOAL_PRIVATE_CANARY", "TOKEN_PRIVATE_CANARY", "STDOUT_PRIVATE_CANARY",
            "STDERR_PRIVATE_CANARY", "--goal", "C:\\Users\\", "/home/",
            "Authorization: Bearer", "api_key", "password", "secret",
        ):
            self.assertNotIn(canary, serialized)

    def test_global_diagnostic_without_safe_traceback_is_backward_compatible(self):
        with tempfile.TemporaryDirectory() as temporary, patch(
            "cw.core.diagnostics.global_config_dir", return_value=Path(temporary)
        ):
            record_global_diagnostic(CwError("legacy global", ErrorCode.INVALID_STATE))
            loaded = load_global_diagnostic()

        self.assertIsNotNone(loaded)
        self.assertNotIn("safe_traceback", loaded)

    def test_record_error_update_preserves_safe_traceback_in_global_diagnostic(self):
        self._assert_global_fallback_preserves_safe_traceback(source="update")

    def test_record_error_none_local_fallback_preserves_safe_traceback(self):
        with patch("cw.cli.main.record_diagnostic", return_value=None):
            self._assert_global_fallback_preserves_safe_traceback()

    def test_record_error_exception_local_fallback_preserves_safe_traceback(self):
        with patch("cw.cli.main.record_diagnostic", side_effect=OSError("local diagnostics unavailable")):
            self._assert_global_fallback_preserves_safe_traceback()

    def _assert_global_fallback_preserves_safe_traceback(self, *, source: str | None = None) -> None:
        safe_traceback = {
            "version": 1,
            "exception_type": "CwError",
            "frames": [{"module": "cw.cli.main", "function": "_record_error", "line": 484}],
        }
        with tempfile.TemporaryDirectory() as temporary, patch(
            "cw.core.diagnostics.global_config_dir", return_value=Path(temporary)
        ):
            _record_error(
                CwError("global fallback", ErrorCode.INVALID_STATE),
                source=source,
                safe_traceback=safe_traceback,
            )
            loaded = load_global_diagnostic()

        assert loaded is not None
        self.assertEqual(safe_traceback, loaded["safe_traceback"])


if __name__ == "__main__":
    unittest.main()
