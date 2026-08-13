from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from cw.core.errors import CwError, ErrorCode
from cw.adapters.structured_output import validate_codex_output_schema
from cw.integrations.diagnostics import parse_mcp_diagnostics
from cw.adapters.invocation import (
    invocation_details,
    managed_codex_environment,
    record_invocation,
    record_run_result,
)
from cw.adapters.result import CodexResult, CodexRunResult


class CodexAdapter:
    def __init__(self, command: str = "codex") -> None:
        self.command = command

    def check_availability(self) -> bool:
        return shutil.which(self.command) is not None

    def _require(self) -> None:
        if not self.check_availability():
            raise CwError("Codex CLI was not found", ErrorCode.CODEX_NOT_FOUND, "Install Codex and run: cw doctor")

    def _result(
        self,
        root: Path,
        role: str,
        completed: subprocess.CompletedProcess[str],
        *,
        payload: dict | None = None,
    ) -> CodexRunResult:
        """Build and persist the canonical result without promoting MCP noise."""

        diagnostics = parse_mcp_diagnostics(completed.stderr)
        terminal_error = None
        if completed.returncode != 0:
            error_role = "implementer" if role.startswith("implementer") else role
            terminal_error = self.classify_process_error(
                completed.stderr, completed.stdout, role=error_role,
            )
        result = CodexRunResult(
            payload,
            completed.stderr,
            completed.stdout,
            completed.returncode,
            diagnostics,
            terminal_error,
        )
        record_run_result(
            root,
            role,
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            diagnostics=tuple(item.to_dict() for item in diagnostics),
        )
        return result

    def _run_captured(
        self,
        root: Path,
        role: str,
        command: list[str],
        environment: dict[str, str],
        *,
        timeout: int | None,
    ) -> CodexRunResult:
        completed = subprocess.run(
            command,
            cwd=root,
            env=environment,
            text=True,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
        return self._result(root, role, completed)

    def _validate_implementer_configuration(
        self,
        root: Path,
        global_arguments: list[str],
        environment: dict[str, str],
    ) -> None:
        command = [self.command, *global_arguments, "--cd", str(root), "doctor", "--json"]
        invocation = record_invocation(root, "implementer-config", command, environment)
        result = self._run_captured(
            root, "implementer-config", command, environment, timeout=30,
        )
        if result.exit_code == 0:
            return
        code = self.classify_process_error(result.stderr, result.stdout, role="implementer")
        if code is ErrorCode.CODEX_CONFIG_ERROR:
            diagnostic = self._diagnostic(result.stdout, result.stderr)
            raise CwError(
                "Codex configuration invalid",
                code,
                "Run: cw error",
                details=f"{diagnostic}\n\n{invocation_details(invocation)}",
            )

    def run_implementer(
        self,
        root: Path,
        prompt: str,
        *,
        allow_network: bool = False,
        session_id: str | None = None,
        required_integrations: tuple[str, ...] = (),
        timeout: int | None = None,
    ) -> CodexRunResult:
        self._require()
        environment = managed_codex_environment("implementer", session_id=session_id)
        global_arguments = [
            "--strict-config",
            "--config", f"sandbox_workspace_write.network_access={str(allow_network).lower()}",
        ]
        if not allow_network:
            global_arguments.extend(["--config", 'web_search="disabled"'])
        # Required integrations are checked by the workflow preflight. Optional
        # integrations remain part of Codex's normal effective configuration;
        # CW never writes or overlays mcp_servers.* definitions.
        self._validate_implementer_configuration(root, global_arguments, environment)
        command = [self.command, *global_arguments]
        command.extend([
            "--cd", str(root), "--sandbox", "workspace-write",
            "--ask-for-approval", "never", "exec", "--color", "never", prompt,
        ])
        invocation = record_invocation(root, "implementer", command, environment, prompt=prompt)
        try:
            result = self._run_captured(
                root, "implementer", command, environment, timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise CwError(
                "Batch time limit reached during implementation",
                ErrorCode.BATCH_TIME_EXHAUSTED,
                "Run: cw status",
                details=f"Hard agent timeout: {timeout}s",
                exit_code=4,
            ) from exc
        if result.exit_code:
            code = result.terminal_error or ErrorCode.IMPLEMENTER_PROCESS_ERROR
            config_error = code is ErrorCode.CODEX_CONFIG_ERROR
            raise CwError(
                "Codex configuration invalid" if config_error else "Codex implementer exited unexpectedly",
                code,
                "Run: cw error" if config_error else "Run: cw retry",
                details=(
                    f"Codex exit code: {result.exit_code}\n"
                    f"{self._diagnostic(result.stdout, result.stderr)}\n\n"
                    f"{invocation_details(invocation)}"
                ),
            )
        return result

    def _run_structured(
        self, root: Path, prompt: str, schema: Path, timeout: int, *, role: str
    ) -> CodexResult:
        self._require()
        validate_codex_output_schema(schema, role=role)
        with tempfile.TemporaryDirectory(prefix=f"cw-{role}-") as temporary:
            output = Path(temporary) / "result.json"
            environment = managed_codex_environment(role)
            command = [
                self.command, "--strict-config", "--config", 'web_search="disabled"',
                "--config", "project_doc_max_bytes=0",
                "--ask-for-approval", "never", "exec", "--ephemeral",
                "--disable", "hooks", "--sandbox", "read-only",
                "--ignore-rules", "--color", "never", "--output-schema", str(schema),
                "--output-last-message", str(output), "--cd", str(root), prompt,
            ]
            invocation = record_invocation(root, role, command, environment, prompt=prompt)
            try:
                result = self._run_captured(
                    root, role, command, environment, timeout=timeout,
                )
            except subprocess.TimeoutExpired as exc:
                code = ErrorCode.REVIEW_TIMEOUT if role == "reviewer" else ErrorCode.PLAN_TIMEOUT
                label = "Independent reviewer" if role == "reviewer" else "Codex planner"
                raise CwError(f"{label} timed out", code, "Run: cw retry", details=str(exc)) from exc
            if result.exit_code:
                code = result.terminal_error or self.classify_process_error(
                    result.stderr, result.stdout, role=role,
                )
                label = "Independent reviewer" if role == "reviewer" else "Codex planner"
                hint = "Run: cw error" if code in {
                    ErrorCode.PLANNER_SCHEMA_ERROR, ErrorCode.CODEX_CONFIG_ERROR,
                } else "Run: cw retry"
                diagnostic = self._diagnostic(result.stdout, result.stderr)
                raise CwError(
                    f"{label} unavailable", code, hint,
                    details=f"{diagnostic}\n\n{invocation_details(invocation)}",
                )
            try:
                payload = json.loads(output.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                code = ErrorCode.REVIEWER_PROCESS_ERROR if role == "reviewer" else ErrorCode.PLANNER_SCHEMA_ERROR
                hint = "Run: cw retry" if role == "reviewer" else "Run: cw error"
                raise CwError(f"{role.title()} returned invalid JSON", code, hint, details=str(exc)) from exc
            if not isinstance(payload, dict):
                code = ErrorCode.REVIEWER_PROCESS_ERROR if role == "reviewer" else ErrorCode.PLANNER_SCHEMA_ERROR
                hint = "Run: cw retry" if role == "reviewer" else "Run: cw error"
                raise CwError(f"{role.title()} returned an invalid result", code, hint)
            return CodexRunResult(
                payload, result.stderr, result.stdout, result.exit_code,
                result.integration_diagnostics, result.terminal_error,
            )

    def run_reviewer(self, root: Path, prompt: str, schema: Path, timeout: int) -> CodexResult:
        return self._run_structured(root, prompt, schema, timeout, role="reviewer")

    def run_planner(self, root: Path, prompt: str, schema: Path, timeout: int) -> CodexResult:
        return self._run_structured(root, prompt, schema, timeout, role="planner")

    def smoke_test(self, root: Path, schema: Path, timeout: int = 60) -> CodexResult:
        return self.run_reviewer(
            root,
            "Connectivity smoke test only. Do not inspect repository files. Return decision APPROVE, empty criteria and blocking_criteria lists, no blocking issues, and a short summary.",
            schema,
            timeout,
        )

    @staticmethod
    def _diagnostic(stdout: str, stderr: str) -> str:
        return f"STDOUT\n{stdout[-6000:]}\n\nSTDERR\n{stderr[-6000:]}"

    @staticmethod
    def classify_process_error(stderr: str, stdout: str = "", *, role: str = "reviewer") -> ErrorCode:
        """Classify the terminal cause, not earlier unrelated MCP noise."""
        text = f"{stdout}\n{stderr}".lower()
        if (
            any(term in text for term in (
                "error loading config.toml", "invalid transport", "invalid configuration",
            )) and ("mcp_servers." in text or "config.toml" in text)
        ) or any(term in text for term in (
            '"config.load"', "config could not be loaded", "failed to load codex config",
        )):
            return ErrorCode.CODEX_CONFIG_ERROR
        if role == "planner" and any(term in text for term in (
            "invalid_json_schema", "invalid schema for response_format",
            "structured output schema", "text.format.schema",
        )):
            return ErrorCode.PLANNER_SCHEMA_ERROR
        if any(term in text for term in ("unauthorized", "authentication", "invalid api key", "login required")):
            if role == "reviewer":
                return ErrorCode.REVIEWER_PROCESS_ERROR
            if role == "implementer":
                return ErrorCode.IMPLEMENTER_PROCESS_ERROR
            return ErrorCode.PLANNER_PROCESS_ERROR
        if any(term in text for term in ("websocket", "wss://", "transport channel closed", "transport error")):
            if role == "reviewer":
                return ErrorCode.REVIEWER_NETWORK_ERROR
            if role == "implementer":
                return ErrorCode.IMPLEMENTER_PROCESS_ERROR
            return ErrorCode.PLANNER_TRANSPORT_ERROR
        if any(term in text for term in ("network unavailable", "connection refused", "connection failed", "dns", "timed out connecting")):
            if role == "reviewer":
                return ErrorCode.REVIEWER_NETWORK_ERROR
            if role == "implementer":
                return ErrorCode.IMPLEMENTER_PROCESS_ERROR
            return ErrorCode.PLANNER_NETWORK_ERROR
        if role == "reviewer":
            return ErrorCode.REVIEWER_PROCESS_ERROR
        if role == "implementer":
            return ErrorCode.IMPLEMENTER_PROCESS_ERROR
        return ErrorCode.PLANNER_PROCESS_ERROR

    @staticmethod
    def classify_transport_error(text: str, *, role: str = "reviewer") -> ErrorCode:
        """Compatibility wrapper for callers using the v0.1.4 API."""
        return CodexAdapter.classify_process_error(text, role=role)
