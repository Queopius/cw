from __future__ import annotations

import json
import errno
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cw.adapters.codex import CodexAdapter
from cw.adapters.prompt_transport import MAX_PROMPT_BYTES, PromptTransport
from cw.adapters.result import CodexRunResult
from cw.core.errors import CwError, ErrorCode


class CodexAdapterTests(unittest.TestCase):
    def test_planner_executes_the_platform_resolved_launcher(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            schema = root / "schema.json"
            schema.write_text("{}", encoding="utf-8")

            def fake_run(command, **_kwargs):
                output = Path(command[command.index("--output-last-message") + 1])
                output.write_text(json.dumps({"phases": []}), encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch(
                "cw.adapters.codex.shutil.which", return_value="C:/Codex/bin/codex.cmd",
            ), patch("cw.adapters.codex.subprocess.run", side_effect=fake_run) as call:
                CodexAdapter().run_planner(root, "plan", schema, 10)

        self.assertEqual("C:/Codex/bin/codex.cmd", call.call_args.args[0][0])

    def test_implementer_denies_network_and_web_search_by_default(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch("cw.adapters.codex.shutil.which", return_value="/usr/bin/codex"), patch(
                "cw.adapters.codex.CodexAdapter._validate_implementer_configuration"
            ), patch(
                "cw.adapters.codex.subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, "done", ""),
            ) as call:
                CodexAdapter().run_implementer(root, "implement")
            command = call.call_args.args[0]
            self.assertIn("--strict-config", command)
            self.assertIn("sandbox_workspace_write.network_access=false", command)
            self.assertIn('web_search="disabled"', command)
            self.assertIn("workspace-write", command)

    def test_implementer_policy_can_allow_sandbox_network(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch("cw.adapters.codex.shutil.which", return_value="/usr/bin/codex"), patch(
                "cw.adapters.codex.CodexAdapter._validate_implementer_configuration"
            ), patch(
                "cw.adapters.codex.subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, "done", ""),
            ) as call:
                CodexAdapter().run_implementer(root, "implement", allow_network=True)
            command = call.call_args.args[0]
            self.assertIn("sandbox_workspace_write.network_access=true", command)
            self.assertNotIn('web_search="disabled"', command)

    def test_implementer_exports_session_to_hook_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch("cw.adapters.codex.shutil.which", return_value="/usr/bin/codex"), patch(
                "cw.adapters.codex.CodexAdapter._validate_implementer_configuration"
            ), patch(
                "cw.adapters.codex.subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, "done", ""),
            ) as call:
                CodexAdapter().run_implementer(root, "implement", session_id="a" * 32)
        self.assertEqual("1", call.call_args.kwargs["env"]["CW_IMPLEMENTER_ACTIVE"])
        self.assertEqual("a" * 32, call.call_args.kwargs["env"]["CW_IMPLEMENTER_SESSION"])

    def test_implementer_nonzero_exit_is_classified(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch("cw.adapters.codex.shutil.which", return_value="/usr/bin/codex"), patch(
                "cw.adapters.codex.CodexAdapter._validate_implementer_configuration"
            ), patch(
                "cw.adapters.codex.subprocess.run",
                return_value=subprocess.CompletedProcess([], 17, "", "process crashed"),
            ), self.assertRaises(CwError) as raised:
                CodexAdapter().run_implementer(Path(temporary), "implement")
        self.assertEqual(ErrorCode.IMPLEMENTER_PROCESS_ERROR, raised.exception.code)
        self.assertIn("17", raised.exception.details or "")

    def test_batch_interrupt_propagates_from_captured_child(self):
        with tempfile.TemporaryDirectory() as temporary, patch(
            "cw.adapters.codex.shutil.which", return_value="/usr/bin/codex",
        ), patch("cw.adapters.codex.CodexAdapter._validate_implementer_configuration"), patch(
            "cw.adapters.codex.subprocess.run", side_effect=KeyboardInterrupt,
        ), self.assertRaises(KeyboardInterrupt):
            CodexAdapter().run_implementer(Path(temporary), "implement", timeout=60)

    def test_reviewer_is_read_only_ephemeral_and_hooks_disabled(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            schema = root / "schema.json"; schema.write_text("{}")
            def fake_run(command, **kwargs):
                self.assertIn("read-only", command)
                self.assertIn("--ephemeral", command)
                self.assertIn(["--disable", "hooks"], [command[index:index + 2] for index in range(len(command) - 1)])
                self.assertFalse(any("mcp_servers." in value for value in command))
                self.assertIn('web_search="disabled"', command)
                self.assertIn("project_doc_max_bytes=0", command)
                self.assertEqual("-", command[-1])
                self.assertEqual(b"review", kwargs["input"])
                self.assertNotIn("review", command)
                output = Path(command[command.index("--output-last-message") + 1])
                output.write_text(json.dumps({"decision": "APPROVE"}))
                return subprocess.CompletedProcess(command, 0, "", "")
            with patch("cw.adapters.codex.shutil.which", return_value="/usr/bin/codex"), patch("cw.adapters.codex.subprocess.run", side_effect=fake_run):
                result = CodexAdapter().run_reviewer(root, "review", schema, 10)
            self.assertIsInstance(result, CodexRunResult)
            self.assertEqual("APPROVE", result.payload["decision"])

    def test_reviewer_accepts_current_agent_message_stream_and_structured_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            schema = root / "schema.json"
            schema.write_text("{}", encoding="utf-8")

            def fake_run(command, **_kwargs):
                output = Path(command[command.index("--output-last-message") + 1])
                output.write_text(
                    json.dumps({"decision": "APPROVE"}), encoding="utf-8"
                )
                stdout = "\n".join(
                    (
                        json.dumps({"type": "thread.started", "thread_id": "synthetic"}),
                        json.dumps({"type": "turn.started"}),
                        json.dumps(
                            {
                                "type": "item.completed",
                                "item": {
                                    "id": "item_0",
                                    "type": "agent_message",
                                    "text": "{\"decision\":\"APPROVE\"}",
                                },
                            }
                        ),
                        json.dumps({"type": "turn.completed", "usage": {}}),
                    )
                )
                return subprocess.CompletedProcess(command, 0, stdout, "")

            with patch(
                "cw.adapters.codex.shutil.which", return_value="/usr/bin/codex"
            ), patch("cw.adapters.codex.subprocess.run", side_effect=fake_run):
                result = CodexAdapter(persist=False).run_reviewer(
                    root, "review", schema, 10
                )

        self.assertEqual("APPROVE", result.payload["decision"])

    def test_reviewer_discards_apparent_approval_when_runtime_cleanup_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            schema = root / "schema.json"; schema.write_text("{}")

            def fake_run(command, **_kwargs):
                output = Path(command[command.index("--output-last-message") + 1])
                output.write_text(json.dumps({"decision": "APPROVE"}), encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, "", "")

            cleanup = CwError(
                "Verification runtime cleanup failed",
                ErrorCode.VERIFICATION_INFRASTRUCTURE_ERROR,
                "Run: cw retry",
                details="[redacted]",
            )
            with patch("cw.adapters.codex.shutil.which", return_value="/usr/bin/codex"), patch(
                "cw.adapters.codex.subprocess.run", side_effect=fake_run
            ), patch("cw.checks.verification._cleanup_runtime", side_effect=cleanup), self.assertRaises(CwError) as raised:
                CodexAdapter().run_reviewer(root, "review", schema, 10)

        self.assertIs(cleanup, raised.exception)
        self.assertEqual(ErrorCode.VERIFICATION_INFRASTRUCTURE_ERROR, raised.exception.code)

    def test_planner_is_structured_read_only_ephemeral_and_rules_disabled(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            schema = root / "schema.json"; schema.write_text("{}")
            def fake_run(command, **kwargs):
                self.assertIn("read-only", command)
                self.assertIn("--ephemeral", command)
                self.assertIn("--ignore-rules", command)
                self.assertIn(["--disable", "hooks"], [command[index:index + 2] for index in range(len(command) - 1)])
                self.assertFalse(any("mcp_servers." in value for value in command))
                self.assertIn('web_search="disabled"', command)
                self.assertIn("project_doc_max_bytes=0", command)
                self.assertEqual("-", command[-1])
                self.assertEqual(b"plan", kwargs["input"])
                self.assertNotIn("plan", command)
                output = Path(command[command.index("--output-last-message") + 1])
                output.write_text(json.dumps({"phases": []}))
                return subprocess.CompletedProcess(command, 0, "", "")
            with patch("cw.adapters.codex.shutil.which", return_value="/usr/bin/codex"), patch(
                "cw.adapters.codex.subprocess.run", side_effect=fake_run
            ):
                result = CodexAdapter().run_planner(root, "plan", schema, 10)
            self.assertIsInstance(result, CodexRunResult)
            self.assertEqual([], result.payload["phases"])

    def test_planner_network_failure_is_classified_separately(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            schema = root / "schema.json"; schema.write_text("{}")
            failure = subprocess.CompletedProcess([], 1, "", "WebSocket connection failed")
            with patch("cw.adapters.codex.shutil.which", return_value="/usr/bin/codex"), patch(
                "cw.adapters.codex.subprocess.run", return_value=failure
            ), self.assertRaises(CwError) as raised:
                CodexAdapter().run_planner(root, "plan", schema, 10)
            self.assertEqual(ErrorCode.PLANNER_TRANSPORT_ERROR, raised.exception.code)

    def test_planner_timeout_is_classified_separately(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            schema = root / "schema.json"; schema.write_text("{}")
            timeout = subprocess.TimeoutExpired(["codex"], 10)
            with patch("cw.adapters.codex.shutil.which", return_value="/usr/bin/codex"), patch(
                "cw.adapters.codex.subprocess.run", side_effect=timeout
            ), self.assertRaises(CwError) as raised:
                CodexAdapter().run_planner(root, "plan", schema, 10)
            self.assertEqual(ErrorCode.PLAN_TIMEOUT, raised.exception.code)

    def test_invalid_mcp_transport_is_nonretryable_codex_config_error(self):
        failure = subprocess.CompletedProcess(
            [], 1, "", "Error loading config.toml: invalid transport in `mcp_servers.vercel`",
        )
        with tempfile.TemporaryDirectory() as temporary, patch(
            "cw.adapters.codex.shutil.which", return_value="/usr/bin/codex",
        ), patch(
            "cw.adapters.codex.subprocess.run", return_value=failure,
        ), self.assertRaises(CwError) as raised:
            CodexAdapter().run_implementer(Path(temporary), "implement")
        self.assertEqual(ErrorCode.CODEX_CONFIG_ERROR, raised.exception.code)
        self.assertEqual("Run: cw error", raised.exception.hint)

    def test_codex_doctor_config_load_failure_is_config_error(self):
        output = '{"checks":{"config.load":{"status":"fail","summary":"config could not be loaded"}}}'
        self.assertEqual(
            ErrorCode.CODEX_CONFIG_ERROR,
            CodexAdapter.classify_process_error("", output, role="implementer"),
        )

    def test_managed_environment_preserves_auth_home_but_drops_parent_identity(self):
        from cw.adapters.invocation import managed_codex_environment

        with patch.dict("os.environ", {
            "HOME": "/home/user", "CODEX_HOME": "/auth/codex",
            "CODEX_THREAD_ID": "parent", "CODEX_PERMISSION_PROFILE": "parent-policy",
        }, clear=True):
            environment = managed_codex_environment("planner")
        self.assertEqual("/auth/codex", environment["CODEX_HOME"])
        self.assertNotIn("CODEX_THREAD_ID", environment)
        self.assertNotIn("CODEX_PERMISSION_PROFILE", environment)

    def test_sanitized_final_argv_and_environment_are_logged(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".cw/logs").mkdir(parents=True)
            with patch("cw.adapters.codex.shutil.which", return_value="/usr/bin/codex"), patch(
                "cw.adapters.codex.CodexAdapter._validate_implementer_configuration"
            ), patch(
                "cw.adapters.codex.subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, "done", ""),
            ):
                CodexAdapter().run_implementer(root, "secret prompt", session_id="b" * 32)
            records = [json.loads(line) for line in (root / ".cw/logs/codex-invocations.jsonl").read_text().splitlines()]
        invocation = records[-1]
        self.assertEqual("implementer", invocation["role"])
        self.assertIn(" exec ", invocation["command"])
        self.assertNotIn("mcp_servers.", invocation["command"])
        self.assertIn("[PROMPT stdin sha256:", invocation["command"])
        self.assertNotIn("secret prompt", invocation["command"])
        self.assertEqual("b" * 32, invocation["environment"]["CW_IMPLEMENTER_SESSION"])

    def test_prompt_transport_preserves_small_large_unicode_and_multiline_bytes(self):
        prompts = (
            "x",
            "normal planning prompt",
            "p" * (160 * 1024),
            "q" * (512 * 1024),
            "Unicode: café λ 🚀\nquotes='\"' backslash=\\\nsecond line",
        )
        adapter = CodexAdapter(persist=False)
        for prompt in prompts:
            with self.subTest(bytes=len(prompt.encode("utf-8"))), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                command = [
                    sys.executable, "-c",
                    "import sys; data=sys.stdin.buffer.read(); sys.stdout.buffer.write(data)",
                ]
                result = adapter._run_captured(
                    root, "planner", command, os.environ.copy(), timeout=10,
                    prompt=PromptTransport.create(prompt, role="planner"),
                )
                self.assertEqual(prompt, result.stdout)

    def test_prompt_is_absent_from_argv_and_environment_near_size_limits(self):
        prompt = "private-content-" + "z" * (512 * 1024)
        environment = os.environ.copy()
        padding = 1_000 if os.name == "nt" else 60_000
        for index in range(8):
            environment[f"CW_TEST_PADDING_{index}"] = "e" * padding
        with tempfile.TemporaryDirectory() as temporary:
            result = CodexAdapter(persist=False)._run_captured(
                Path(temporary), "planner",
                [sys.executable, "-c", "import sys; print(len(sys.stdin.buffer.read()))", "-"],
                environment, timeout=10,
                prompt=PromptTransport.create(prompt, role="planner"),
            )
        self.assertEqual(str(len(prompt.encode("utf-8"))), result.stdout.strip())
        self.assertNotIn(prompt, result.stdout)

    def test_prompt_transport_has_a_bounded_four_mibibyte_contract(self):
        self.assertEqual(4 * 1024 * 1024, MAX_PROMPT_BYTES)
        PromptTransport.create("x" * MAX_PROMPT_BYTES, role="planner")
        with self.assertRaises(CwError) as raised:
            PromptTransport.create("x" * (MAX_PROMPT_BYTES + 1), role="planner")
        self.assertEqual(ErrorCode.PLANNER_TRANSPORT_ERROR, raised.exception.code)
        self.assertIn("maximum_bytes=4194304", raised.exception.details or "")

    def test_subprocess_launch_failure_is_typed_and_retry_safe_without_prompt_leak(self):
        secret = "launch-secret-must-not-leak"
        with tempfile.TemporaryDirectory() as temporary, patch(
            "cw.adapters.codex.subprocess.run", side_effect=OSError(errno.E2BIG, "argument list too long"),
        ), self.assertRaises(CwError) as raised:
            CodexAdapter(persist=False)._run_captured(
                Path(temporary), "planner", ["codex", "exec", "-"], {}, timeout=10,
                prompt=PromptTransport.create(secret, role="planner"),
            )
        self.assertEqual(ErrorCode.PLANNER_TRANSPORT_ERROR, raised.exception.code)
        self.assertIn("retry_safe=true", raised.exception.details or "")
        self.assertNotIn(secret, raised.exception.details or "")

    def test_stdin_transport_creates_no_artifact_after_success_or_failure(self):
        adapter = CodexAdapter(persist=False)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for exit_code in (0, 7):
                with self.subTest(exit_code=exit_code):
                    result = adapter._run_captured(
                        root, "planner",
                        [
                            sys.executable, "-c",
                            f"import sys; sys.stdin.buffer.read(); raise SystemExit({exit_code})",
                            "-",
                        ],
                        os.environ.copy(), timeout=10,
                        prompt=PromptTransport.create("sensitive", role="planner"),
                    )
                    self.assertEqual(exit_code, result.exit_code)
                    self.assertEqual([], list(root.iterdir()))


if __name__ == "__main__":
    unittest.main()
