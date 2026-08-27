from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from cw.cli.main import main
from cw.cli.parser import parse_args
from cw.cli.runner import run
from cw.core.errors import CwError, ErrorCode
from cw.output_protocol import (
    OUTPUT_SCHEMA,
    OutputStatus,
    changed_for,
    output_schema_document,
)
from cw.ui.console import emit_json
from tests.helpers import TempRepo


class OutputProtocolTests(unittest.TestCase):
    def invoke_runner(self, argv, command):
        stdout = io.StringIO()
        stderr = io.StringIO()
        recorded = []
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = run(
                parse_args(argv),
                commands={parse_args(argv).command: command},
                record_error=lambda error, **metadata: recorded.append((error, metadata)),
            )
        return code, stdout.getvalue(), stderr.getvalue(), recorded

    @staticmethod
    def payload_command(payload, code=0):
        def command(*_args):
            emit_json(payload)
            return code
        return command

    def test_explicit_json_is_one_minified_versioned_document(self):
        code, stdout, stderr, _ = self.invoke_runner(
            ["status", "--output=json"], self.payload_command({"state": "PLAN_PROPOSED"}),
        )
        self.assertEqual(0, code)
        self.assertEqual("", stderr)
        self.assertEqual(1, len(stdout.splitlines()))
        self.assertNotIn("\x1b", stdout)
        self.assertNotIn(": ", stdout)
        payload = json.loads(stdout)
        self.assertEqual(OUTPUT_SCHEMA, payload["schema"])
        self.assertEqual("success", payload["status"])
        self.assertFalse(payload["changed"])
        self.assertEqual("PLAN_PROPOSED", payload["data"]["state"])

    def test_legacy_json_remains_unwrapped(self):
        code, stdout, stderr, _ = self.invoke_runner(
            ["status", "--json"], self.payload_command({"state": "PLAN_PROPOSED"}),
        )
        self.assertEqual(0, code)
        self.assertEqual("", stderr)
        self.assertEqual({"state": "PLAN_PROPOSED"}, json.loads(stdout))

    def test_llm_alias_compacts_status_without_losing_governance_invariants(self):
        source = {
            "state": "PLAN_PROPOSED", "phase": "01", "verbose_note": "omit",
            "authorization": {
                "repository": "Queopius/cw", "head_sha": "a" * 40, "base_sha": "b" * 40,
                "evidence_schema": 2, "generation": "r0", "authorization_state": "AUTHORIZED",
            },
        }
        code, stdout, _, _ = self.invoke_runner(["status", "--llm"], self.payload_command(source))
        self.assertEqual(0, code)
        data = json.loads(stdout)["data"]
        envelope = json.loads(stdout)
        self.assertNotIn("verbose_note", data)
        self.assertEqual("llm_projection", envelope["truncation"]["reason"])
        self.assertEqual("a" * 40, data["authorization"]["head_sha"])
        self.assertEqual("b" * 40, data["authorization"]["base_sha"])
        self.assertEqual("AUTHORIZED", data["authorization"]["authorization_state"])

        code, expanded, _, _ = self.invoke_runner(
            ["status", "--llm", "--expand"], self.payload_command(source),
        )
        self.assertEqual(0, code)
        expanded_payload = json.loads(expanded)
        self.assertEqual("omit", expanded_payload["data"]["verbose_note"])
        self.assertFalse(expanded_payload["truncation"]["truncated"])

    def test_fields_are_allowlisted_nested_and_preserve_invariants(self):
        source = {
            "state": "READY", "phase": "01", "plan": {"status": "APPROVED", "goal": "Ship"},
            "authorization": {"head_sha": "a" * 40, "base_sha": "b" * 40},
        }
        code, stdout, _, _ = self.invoke_runner(
            ["status", "--output=json", "--fields=state,plan.status"], self.payload_command(source),
        )
        self.assertEqual(0, code)
        self.assertEqual(
            {"state": "READY", "plan": {"status": "APPROVED"},
             "authorization": {"head_sha": "a" * 40, "base_sha": "b" * 40}},
            json.loads(stdout)["data"],
        )

    def test_unknown_field_and_arbitrary_expression_fail_closed(self):
        for fields in ("secret", "state[0]"):
            with self.subTest(fields=fields):
                code, stdout, stderr, _ = self.invoke_runner(
                    ["status", "--output=json", f"--fields={fields}"], self.payload_command({"state": "READY"}),
                )
                self.assertEqual(2, code)
                self.assertEqual("", stderr)
                self.assertEqual("USAGE_ERROR", json.loads(stdout)["error"]["code"])

    def test_cli_beats_environment_and_environment_beats_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "cw"
            config.mkdir()
            (config / "config.toml").write_text('[output]\nmode = "jsonl"\n', encoding="utf-8")
            environment = {"XDG_CONFIG_HOME": temporary}
            with patch.dict(os.environ, environment, clear=False):
                code, stdout, _, _ = self.invoke_runner(["version"], self.payload_command({"version": "x"}))
                self.assertEqual(0, code)
                self.assertEqual(OUTPUT_SCHEMA, json.loads(stdout)["schema"])
                with patch.dict(os.environ, {"CW_OUTPUT_MODE": "llm"}, clear=False):
                    code, stdout, _, _ = self.invoke_runner(
                        ["version", "--output=human"], self.payload_command({"version": "x"}),
                    )
                    self.assertEqual(0, code)
                    self.assertEqual({"version": "x"}, json.loads(stdout))

    def test_incompatible_options_are_structured(self):
        for arguments in (
            ["status", "--output=json", "--llm"],
            ["status", "--json", "--llm"],
            ["status", "--json", "--output=json"],
        ):
            with self.subTest(arguments=arguments):
                code, stdout, stderr, _ = self.invoke_runner(
                    arguments, self.payload_command({"state": "READY"}),
                )
                self.assertEqual(2, code)
                self.assertEqual("", stderr)
                payload = json.loads(stdout)
                self.assertEqual("error", payload["status"])
                self.assertEqual("USAGE_ERROR", payload["error"]["code"])

    def test_errors_are_compact_redacted_and_not_noop(self):
        failure = CwError(
            "Workflow changed since authorization access_token=should-not-leak",
            ErrorCode.STALE_WORKFLOW_SHA,
            "Run: cw status",
            details="/home/operator/private access_token=should-not-leak",
            exit_code=4,
        )

        def command(*_args):
            raise failure

        code, stdout, stderr, recorded = self.invoke_runner(["status", "--output=json"], command)
        self.assertEqual(4, code)
        self.assertEqual("", stderr)
        self.assertEqual(1, len(recorded))
        payload = json.loads(stdout)
        self.assertEqual("error", payload["status"])
        self.assertFalse(payload["changed"])
        self.assertNotIn("should-not-leak", stdout)
        self.assertNotIn("/home/operator", stdout)
        self.assertNotIn("details", payload["error"])
        self.assertEqual(payload["error"]["correlation_id"], recorded[0][1]["correlation_id"])

    def test_debug_uses_only_redacted_stderr(self):
        failure = CwError(
            "Invalid request", ErrorCode.USAGE_ERROR, details="password=hunter2 /home/operator/private", exit_code=2,
        )

        def command(*_args):
            raise failure

        code, stdout, stderr, _ = self.invoke_runner(["status", "--output=json", "--debug"], command)
        self.assertEqual(2, code)
        json.loads(stdout)
        self.assertNotIn("hunter2", stderr)
        self.assertNotIn("/home/operator", stderr)
        self.assertIn("[REDACTED]", stderr)

    def test_remote_html_and_unbounded_dumps_are_not_returned(self):
        message = "<html><body>gateway unavailable</body></html> " + "x" * 1000

        def command(*_args):
            raise CwError(message, ErrorCode.UPDATE_CHECK_ERROR, exit_code=1)

        code, stdout, _, _ = self.invoke_runner(["status", "--llm"], command)
        self.assertEqual(1, code)
        rendered = json.loads(stdout)["error"]["message"]
        self.assertNotIn("<html>", rendered)
        self.assertLessEqual(len(rendered), 240)

    def test_pagination_is_bounded_stable_and_rejects_invalid_cursor(self):
        phases = [{"phase": f"{index:02d}"} for index in range(12)]
        command = self.payload_command({"workflow": "sample", "phases": phases, "events": []})
        code, first, _, _ = self.invoke_runner(["history", "--llm"], command)
        self.assertEqual(0, code)
        first_payload = json.loads(first)
        self.assertEqual(10, len(first_payload["data"]["phases"]))
        self.assertTrue(first_payload["page"]["has_more"])
        self.assertTrue(first_payload["truncation"]["truncated"])
        cursor = first_payload["page"]["next_cursor"]
        code, second, _, _ = self.invoke_runner(["history", "--llm", f"--cursor={cursor}"], command)
        self.assertEqual(0, code)
        self.assertEqual([{"phase": "10"}, {"phase": "11"}], json.loads(second)["data"]["phases"])
        code, invalid, _, _ = self.invoke_runner(["history", "--llm", "--cursor=forged"], command)
        self.assertEqual(2, code)
        self.assertEqual("USAGE_ERROR", json.loads(invalid)["error"]["code"])
        changed = self.payload_command({"workflow": "sample", "phases": phases[:-1], "events": []})
        code, stale, _, _ = self.invoke_runner(["history", "--llm", f"--cursor={cursor}"], changed)
        self.assertEqual(2, code)
        self.assertEqual("USAGE_ERROR", json.loads(stale)["error"]["code"])

    def test_jsonl_has_one_valid_envelope_per_event(self):
        def command(*_args):
            emit_json({"sequence": 1})
            emit_json({"sequence": 2})
            return 0

        code, stdout, stderr, _ = self.invoke_runner(["status", "--output=jsonl"], command)
        self.assertEqual(0, code)
        self.assertEqual("", stderr)
        records = [json.loads(line) for line in stdout.splitlines()]
        self.assertEqual(["partial", "success"], [item["status"] for item in records])
        self.assertEqual([1, 2], [item["data"]["sequence"] for item in records])

    def test_machine_cancellation_keeps_exit_130_and_one_document(self):
        def command(*_args):
            raise KeyboardInterrupt

        code, stdout, stderr, _ = self.invoke_runner(["status", "--output=json"], command)
        self.assertEqual(130, code)
        self.assertEqual("", stderr)
        payload = json.loads(stdout)
        self.assertEqual("cancelled", payload["status"])
        self.assertEqual("CANCELLED", payload["error"]["code"])

    def test_noop_requires_a_successful_idempotent_replay(self):
        command = self.payload_command({
            "status": "SUCCEEDED", "idempotent_replay": True, "operation_id": "same-operation",
        })
        code, stdout, _, _ = self.invoke_runner(["start", "--llm"], command)
        self.assertEqual(0, code)
        payload = json.loads(stdout)
        self.assertEqual("noop", payload["status"])
        self.assertFalse(payload["changed"])
        self.assertEqual("same-operation", payload["operation_id"])

    def test_rebaseline_replay_projects_changed_false(self):
        data = {"changed": True, "idempotent_replay": True, "status": "RECOVERED"}
        self.assertFalse(changed_for("plan.rebaseline.recover", OutputStatus.SUCCESS, data))

    def test_authorization_gate_keeps_exact_head_and_base_identity(self):
        details = json.dumps({
            "repository": "Queopius/cw", "pr": 60,
            "head_branch": "dev", "head_sha": "a" * 40,
            "base_branch": "staging", "base_sha": "b" * 40,
            "evidence_schema": 2, "generation": "r0",
            "authorization_state": "REQUIRED", "next_safe_action": "Obtain exact-SHA authorization",
        })

        def command(*_args):
            raise CwError(
                "Exact-SHA authorization is required", ErrorCode.AUTHORIZATION_REQUIRED,
                details=details, exit_code=3,
            )

        code, stdout, stderr, _ = self.invoke_runner(["governance", "authorize", "--llm"], command)
        self.assertEqual(3, code)
        self.assertEqual("", stderr)
        payload = json.loads(stdout)
        self.assertEqual("authorization_required", payload["status"])
        self.assertEqual("a" * 40, payload["gate"]["head_sha"])
        self.assertEqual("b" * 40, payload["gate"]["base_sha"])
        self.assertEqual(2, payload["gate"]["evidence_schema"])

    def test_schema_is_closed_and_declares_all_statuses(self):
        schema = output_schema_document()
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(
            {"success", "noop", "error", "authorization_required", "blocked", "partial", "cancelled"},
            set(schema["properties"]["status"]["enum"]),
        )


class OutputProtocolBinaryTests(unittest.TestCase):
    def test_real_binary_stdout_stderr_and_exit_code(self):
        with tempfile.TemporaryDirectory() as temporary:
            environment = dict(os.environ)
            environment.update({"PYTHONPATH": str(Path(__file__).parents[1]), "XDG_CONFIG_HOME": temporary})
            completed = subprocess.run(
                [sys.executable, "-m", "cw", "version", "--output=json"],
                cwd=temporary, env=environment, text=True, capture_output=True, check=False,
            )
        self.assertEqual(0, completed.returncode)
        self.assertEqual("", completed.stderr)
        self.assertEqual(OUTPUT_SCHEMA, json.loads(completed.stdout)["schema"])
        self.assertEqual(1, len(completed.stdout.splitlines()))

    def test_configured_output_mode_is_written_atomically_and_observed(self):
        with tempfile.TemporaryDirectory() as temporary:
            environment = dict(os.environ)
            environment.update({"PYTHONPATH": str(Path(__file__).parents[1]), "XDG_CONFIG_HOME": temporary})
            configured = subprocess.run(
                [sys.executable, "-m", "cw", "config", "set", "output.mode", "llm", "--output=json"],
                cwd=temporary, env=environment, text=True, capture_output=True, check=False,
            )
            observed = subprocess.run(
                [sys.executable, "-m", "cw", "version"], cwd=temporary, env=environment,
                text=True, capture_output=True, check=False,
            )
            config_text = (Path(temporary) / "cw" / "config.toml").read_text(encoding="utf-8")
        self.assertEqual(0, configured.returncode)
        self.assertEqual(0, observed.returncode)
        self.assertEqual("llm", json.loads(configured.stdout)["data"]["value"])
        self.assertEqual(OUTPUT_SCHEMA, json.loads(observed.stdout)["schema"])
        self.assertIn('[output]\nmode = "llm"', config_text)

    def test_capability_discovery_reports_exact_plugin_surface(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = main(("capabilities", "--output=json"))
        self.assertEqual(0, code)
        data = json.loads(stdout.getvalue())["data"]
        self.assertEqual("0.1.0", data["plugin"])
        self.assertEqual(12, data["plugin_compatibility"]["tool_count"])
        self.assertEqual("cw.remote.v1", data["remote_protocol"])
        self.assertEqual("cw.output.v1", data["schemas"]["output"])

    def test_status_human_json_and_llm_observe_the_same_state_without_mutation(self):
        repository = TempRepo()
        previous = Path.cwd()
        try:
            os.chdir(repository.root)
            before = {
                path.relative_to(repository.root).as_posix(): path.read_bytes()
                for base in (repository.root / ".cw", repository.root / ".codex")
                for path in base.rglob("*") if path.is_file()
            }
            outputs = []
            for arguments in (("status",), ("status", "--output=json"), ("status", "--llm")):
                stdout = io.StringIO()
                stderr = io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr), patch("cw.cli.runner.automatic_update_notice", return_value=None):
                    code = main(arguments)
                self.assertEqual(0, code)
                self.assertEqual("", stderr.getvalue())
                outputs.append(stdout.getvalue())
            after = {
                path.relative_to(repository.root).as_posix(): path.read_bytes()
                for base in (repository.root / ".cw", repository.root / ".codex")
                for path in base.rglob("*") if path.is_file()
            }
            self.assertEqual(before, after)
            self.assertIn("IN PROGRESS", outputs[0])
            self.assertEqual("IN_PROGRESS", json.loads(outputs[1])["data"]["state"])
            self.assertEqual("IN_PROGRESS", json.loads(outputs[2])["data"]["state"])
        finally:
            os.chdir(previous)
            repository.close()


if __name__ == "__main__":
    unittest.main()
