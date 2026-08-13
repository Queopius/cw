from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cw.adapters.codex import CodexAdapter
from cw.core.errors import CwError, ErrorCode
from cw.integrations.diagnostics import parse_mcp_diagnostics
from cw.integrations.manager import IntegrationManager
from cw.integrations.models import Integration, IntegrationHealth, Requirement
from cw.integrations.config import project_requirements
from cw.core.models import Phase
from cw.ui.console import Console
from cw.ui.renderers import render_integrations
from cw.cli.commands.read import doctor_checks


VERCEL_500 = """2026-08-13T09:00:00Z WARN MCP client for `vercel` failed to start:
unexpected server response: HTTP 500 Internal Server Error
<html><body><script>large diagnostic content</script></body></html>
⚠ MCP startup incomplete (failed: vercel)
"""


class FakeRunner:
    def __init__(self, *, stderr: str = "", exit_code: int = 0, status: str = "enabled"):
        self.stderr = stderr
        self.exit_code = exit_code
        self.status = status
        self.calls: list[list[str]] = []

    def __call__(self, command, **_kwargs):
        self.calls.append(command)
        if len(command) > 2 and command[1] == "mcp" and command[-1] == "list":
            return subprocess.CompletedProcess(command, 0, f"Name Url Status Auth\nvercel https://mcp.vercel.com {self.status} Unknown\n", "")
        return subprocess.CompletedProcess(command, self.exit_code, "INTEGRATIONS_OK\n", self.stderr)


class IntegrationDiagnosticTests(unittest.TestCase):
    def test_http_500_is_server_error_and_deduplicated(self):
        diagnostics = parse_mcp_diagnostics(VERCEL_500 + VERCEL_500)
        self.assertEqual(1, len(diagnostics))
        self.assertEqual("MCP_SERVER_ERROR", diagnostics[0].error_code)
        self.assertEqual(500, diagnostics[0].http_status)
        self.assertGreater(diagnostics[0].occurrences, 1)

    def test_auth_error_has_priority(self):
        diagnostic = parse_mcp_diagnostics(
            "MCP client for `vercel` failed to start\nAuthRequired: invalid_token\nHTTP 500"
        )[0]
        self.assertEqual("MCP_AUTH_REQUIRED", diagnostic.error_code)
        self.assertEqual(IntegrationHealth.AUTH_REQUIRED, diagnostic.status)

    def test_unrelated_hook_diagnostic_is_not_mcp(self):
        self.assertEqual((), parse_mcp_diagnostics("SessionStart hook failed with ERROR"))


class IntegrationManagerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="cw-integrations-")
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def manager(self, runner: FakeRunner) -> IntegrationManager:
        return IntegrationManager("fake-codex", runner=runner, cache_path=self.root / "health.json")

    def test_optional_mcp_unavailable_does_not_block(self):
        result = self.manager(FakeRunner(stderr=VERCEL_500)).check(self.root, force=True)
        item = result.integrations[0]
        self.assertEqual("NONE", item.impact)
        self.assertEqual(IntegrationHealth.UNAVAILABLE, item.health)
        self.assertEqual(0, result.exit_code)

    def test_required_mcp_unavailable_blocks(self):
        with self.assertRaises(CwError) as caught:
            self.manager(FakeRunner(stderr=VERCEL_500)).preflight(self.root, {"vercel"})
        self.assertEqual(ErrorCode.MCP_REQUIRED_UNAVAILABLE, caught.exception.code)
        self.assertEqual(3, caught.exception.exit_code)

    def test_required_disabled_blocks(self):
        with self.assertRaises(CwError) as caught:
            self.manager(FakeRunner(status="disabled")).preflight(self.root, {"vercel"})
        self.assertEqual(ErrorCode.MCP_DISABLED, caught.exception.code)

    def test_required_not_configured_blocks(self):
        runner = FakeRunner()
        def empty(command, **kwargs):
            if command[1:3] == ["mcp", "list"]:
                return subprocess.CompletedProcess(command, 0, "Name Url Status Auth\n", "")
            return runner(command, **kwargs)
        manager = IntegrationManager("fake-codex", runner=empty, cache_path=self.root / "health.json")
        with self.assertRaises(CwError) as caught:
            manager.preflight(self.root, {"vercel"})
        self.assertEqual(ErrorCode.MCP_NOT_CONFIGURED, caught.exception.code)

    def test_health_cache_has_no_raw_html_or_tokens(self):
        stderr = VERCEL_500 + " Authorization: Bearer secret-token-value"
        manager = self.manager(FakeRunner(stderr=stderr))
        manager.check(self.root, force=True)
        persisted = manager.cache_path.read_text(encoding="utf-8")
        self.assertNotIn("<html>", persisted)
        self.assertNotIn("secret-token-value", persisted)

    def test_normal_render_hides_html_and_verbose_can_show_raw(self):
        item = Integration(
            "vercel", "mcp", True, Requirement.OPTIONAL,
            IntegrationHealth.UNAVAILABLE, error_code="MCP_SERVER_ERROR", http_status=500,
        )
        payload = {"integrations": [item.to_dict()], "workflow_can_continue": True}
        normal = io.StringIO()
        render_integrations(Console(stream=normal), payload, raw=VERCEL_500)
        self.assertNotIn("<html>", normal.getvalue())
        self.assertIn("HTTP 500", normal.getvalue())
        verbose = io.StringIO()
        render_integrations(Console(stream=verbose), payload, verbose=True, raw=VERCEL_500)
        self.assertIn("<html>", verbose.getvalue())

    def test_project_requirement_contains_no_credentials(self):
        config = self.root / ".cw/config.toml"
        config.parent.mkdir()
        config.write_text("[integrations.vercel]\nrequired = true\n", encoding="utf-8")
        self.assertEqual({"vercel"}, project_requirements(self.root))

    def test_phase_specific_required_integrations_parse(self):
        phase = Phase.from_dict({
            "id": "01-deploy", "name": "Deploy", "objective": "Deploy",
            "acceptance_criteria": [{"id": "D-1", "description": "Deployed"}],
            "required_integrations": ["vercel"],
        })
        self.assertEqual(("vercel",), phase.required_integrations)

    def test_effective_mcp_discovery_does_not_depend_on_config_toml(self):
        config = self.root / "config.toml"
        config.write_text("model = 'gpt-5'\n", encoding="utf-8")
        manager = self.manager(FakeRunner())
        self.assertEqual("vercel", manager.configured()[0].id)
        self.assertNotIn("mcp_servers", config.read_text(encoding="utf-8"))

    def test_doctor_keeps_independent_checks_visible_after_workflow_error(self):
        available = Integration(
            "vercel", "mcp", True, Requirement.OPTIONAL, IntegrationHealth.AVAILABLE,
        )
        failure = CwError("broken readiness", ErrorCode.INVALID_STATE)
        with patch("cw.cli.commands.read.CodexAdapter.smoke_test"), patch(
            "cw.cli.commands.read.IntegrationManager.check",
            return_value=SimpleNamespace(integrations=(available,)),
        ):
            checks = doctor_checks(
                self.root,
                True,
                True,
                context=lambda _root: (_ for _ in ()).throw(failure),
                current_resolver=lambda _workflow, _state: None,
            )
        names = {item["name"] for item in checks}
        self.assertIn("Workflow integrity", names)
        self.assertIn("Reviewer connectivity", names)
        self.assertIn("Vercel MCP", names)


class CodexIntegrationIsolationTests(unittest.TestCase):
    def test_planner_uses_user_auth_but_ignores_optional_user_config(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); schema = root / "schema.json"; output = {"phases": []}
            schema.write_text('{"type":"object"}', encoding="utf-8")
            def run(command, **_kwargs):
                Path(command[command.index("--output-last-message") + 1]).write_text(json.dumps(output), encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, "", VERCEL_500)
            with patch("cw.adapters.codex.shutil.which", return_value="/usr/bin/codex"), patch(
                "cw.adapters.codex.subprocess.run", side_effect=run,
            ) as invoked:
                result = CodexAdapter().run_planner(root, "plan", schema, 10)
            command = invoked.call_args.args[0]
            self.assertIn("--ignore-user-config", command)
            self.assertIn(["--disable", "plugins"], [command[index:index + 2] for index in range(len(command) - 1)])
            self.assertNotIn("CODEX_HOME", invoked.call_args.kwargs["env"])
            self.assertEqual(1, len(result.mcp_diagnostics))
            self.assertEqual(output, result.payload)

    def test_reviewer_success_is_not_overridden_by_mcp_error_text(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); schema = root / "schema.json"; schema.write_text('{"type":"object"}', encoding="utf-8")
            payload = {"decision": "APPROVE"}
            def run(command, **_kwargs):
                Path(command[command.index("--output-last-message") + 1]).write_text(json.dumps(payload), encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, "", VERCEL_500)
            with patch("cw.adapters.codex.shutil.which", return_value="/usr/bin/codex"), patch(
                "cw.adapters.codex.subprocess.run", side_effect=run,
            ):
                result = CodexAdapter().run_reviewer(root, "review", schema, 10)
            self.assertEqual(payload, result.payload)

    def test_implementer_disables_optional_plugins_without_reconstructing_definition(self):
        effective = (
            Integration("vercel", "mcp", True, Requirement.OPTIONAL, IntegrationHealth.UNKNOWN),
        )
        with tempfile.TemporaryDirectory() as name, patch(
            "cw.adapters.codex.shutil.which", return_value="/usr/bin/codex",
        ), patch(
            "cw.adapters.codex.IntegrationManager.configured", side_effect=(effective, ()),
        ), patch(
            "cw.adapters.codex.CodexAdapter._validate_implementer_configuration"
        ), patch(
            "cw.adapters.codex.subprocess.call", return_value=0,
        ) as invoked:
            CodexAdapter().run_implementer(Path(name), "work")
        command = invoked.call_args.args[0]
        self.assertIn(["--disable", "plugins"], [command[index:index + 2] for index in range(len(command) - 1)])
        self.assertFalse(any("mcp_servers.vercel" in value for value in command))
        self.assertFalse(any("transport" in value for value in command))

    def test_standalone_optional_mcp_receives_only_enabled_false(self):
        standalone = (Integration(
            "figma", "mcp", True, Requirement.OPTIONAL, IntegrationHealth.UNKNOWN,
        ),)
        with patch("cw.adapters.codex.IntegrationManager.configured", side_effect=(standalone, standalone)):
            arguments = CodexAdapter()._integration_arguments(())
        rendered = " ".join(arguments)
        self.assertIn("--disable plugins", rendered)
        self.assertIn("mcp_servers.figma.enabled=false", rendered)
        self.assertNotIn("transport", rendered)

    def test_implementer_preserves_effective_config_when_plugin_is_required(self):
        effective = (
            Integration("vercel", "mcp", True, Requirement.REQUIRED, IntegrationHealth.AVAILABLE),
            Integration("figma", "mcp", True, Requirement.OPTIONAL, IntegrationHealth.UNKNOWN),
        )
        standalone = (effective[1],)
        with tempfile.TemporaryDirectory() as name, patch(
            "cw.adapters.codex.shutil.which", return_value="/usr/bin/codex",
        ), patch("cw.adapters.codex.IntegrationManager.configured", side_effect=(effective, standalone)), patch(
            "cw.adapters.codex.CodexAdapter._validate_implementer_configuration"
        ), patch(
            "cw.adapters.codex.subprocess.call", return_value=0,
        ) as invoked:
            CodexAdapter().run_implementer(Path(name), "work", required_integrations=("vercel",))
        command = invoked.call_args.args[0]
        rendered = " ".join(command)
        self.assertNotIn("mcp_servers.figma.enabled=false", rendered)
        self.assertNotIn("mcp_servers.vercel.enabled=false", rendered)
        self.assertNotIn("--disable plugins", rendered)
        self.assertNotIn("transport", rendered)

    def test_process_isolation_never_modifies_user_config(self):
        with tempfile.TemporaryDirectory() as name:
            config = Path(name) / "config.toml"
            config.write_text("[plugins.vercel]\nenabled = true\n", encoding="utf-8")
            before = config.read_bytes()
            effective = (Integration(
                "vercel", "mcp", True, Requirement.OPTIONAL, IntegrationHealth.UNKNOWN,
            ),)
            with patch("cw.adapters.codex.IntegrationManager.configured", side_effect=(effective, ())):
                CodexAdapter()._integration_arguments(())
            self.assertEqual(before, config.read_bytes())


if __name__ == "__main__":
    unittest.main()
