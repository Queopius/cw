from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cw.core.errors import CwError, ErrorCode
from cw.adapters.structured_output import validate_codex_output_schema
from cw.integrations.diagnostics import parse_mcp_diagnostics
from cw.integrations.manager import IntegrationManager
from cw.integrations.models import IntegrationDiagnostic


@dataclass(frozen=True, slots=True)
class CodexResult:
    payload: dict[str, Any]
    stderr: str
    stdout: str = ""
    mcp_diagnostics: tuple[IntegrationDiagnostic, ...] = ()


class CodexAdapter:
    def __init__(self, command: str = "codex") -> None:
        self.command = command

    def check_availability(self) -> bool:
        return shutil.which(self.command) is not None

    def _require(self) -> None:
        if not self.check_availability():
            raise CwError("Codex CLI was not found", ErrorCode.CODEX_NOT_FOUND, "Install Codex and run: cw doctor")

    def run_implementer(
        self,
        root: Path,
        prompt: str,
        *,
        allow_network: bool = False,
        session_id: str | None = None,
        required_integrations: tuple[str, ...] = (),
        timeout: int | None = None,
    ) -> int:
        self._require()
        environment = os.environ.copy()
        environment["CW_IMPLEMENTER_ACTIVE"] = "1"
        if session_id:
            environment["CW_IMPLEMENTER_SESSION"] = session_id
        command = [
            self.command, "--strict-config",
            "--config", f"sandbox_workspace_write.network_access={str(allow_network).lower()}",
        ]
        if not allow_network:
            command.extend(["--config", 'web_search="disabled"'])
        try:
            configured = IntegrationManager(self.command).configured(set(required_integrations))
            for integration in configured:
                if integration.id not in required_integrations:
                    command.extend(["--config", f"mcp_servers.{integration.id}.enabled=false"])
        except CwError:
            # Optional-integration discovery cannot turn a healthy implementer
            # into a workflow failure. Required integrations are preflighted by
            # the execution command before this point.
            pass
        command.extend([
            "--cd", str(root), "--sandbox", "workspace-write",
            "--ask-for-approval", "never", "--no-alt-screen", prompt,
        ])
        if timeout is None:
            return_code = subprocess.call(command, cwd=root, env=environment)
        else:
            process = subprocess.Popen(command, cwd=root, env=environment)
            try:
                return_code = process.wait(timeout=timeout)
            except KeyboardInterrupt:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                raise
            except subprocess.TimeoutExpired as exc:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                raise CwError(
                    "Batch time limit reached during implementation",
                    ErrorCode.BATCH_TIME_EXHAUSTED,
                    "Run: cw status",
                    details=f"Hard agent timeout: {timeout}s",
                    exit_code=4,
                ) from exc
        if return_code:
            raise CwError(
                "Codex implementer exited unexpectedly",
                ErrorCode.IMPLEMENTER_PROCESS_ERROR,
                "Run: cw retry",
                details=f"Codex exit code: {return_code}",
            )
        return 0

    def _run_structured(
        self, root: Path, prompt: str, schema: Path, timeout: int, *, role: str
    ) -> CodexResult:
        self._require()
        validate_codex_output_schema(schema, role=role)
        with tempfile.TemporaryDirectory(prefix=f"cw-{role}-") as temporary:
            output = Path(temporary) / "result.json"
            environment = os.environ.copy()
            environment[f"CW_{role.upper()}_ACTIVE"] = "1"
            command = [
                self.command, "--strict-config", "--config", 'web_search="disabled"',
                "--config", "project_doc_max_bytes=0",
                "--ask-for-approval", "never", "exec", "--ephemeral", "--ignore-user-config",
                "--disable", "hooks", "--sandbox", "read-only",
                "--ignore-rules", "--color", "never", "--output-schema", str(schema),
                "--output-last-message", str(output), "--cd", str(root), prompt,
            ]
            try:
                completed = subprocess.run(
                    command, cwd=root, env=environment, text=True, capture_output=True,
                    stdin=subprocess.DEVNULL, timeout=timeout, check=False,
                )
            except subprocess.TimeoutExpired as exc:
                code = ErrorCode.REVIEW_TIMEOUT if role == "reviewer" else ErrorCode.PLAN_TIMEOUT
                label = "Independent reviewer" if role == "reviewer" else "Codex planner"
                raise CwError(f"{label} timed out", code, "Run: cw retry", details=str(exc)) from exc
            if completed.returncode:
                code = self.classify_process_error(completed.stderr, completed.stdout, role=role)
                label = "Independent reviewer" if role == "reviewer" else "Codex planner"
                hint = "Run: cw error" if code is ErrorCode.PLANNER_SCHEMA_ERROR else "Run: cw retry"
                diagnostic = self._diagnostic(completed.stdout, completed.stderr)
                raise CwError(f"{label} unavailable", code, hint, details=diagnostic)
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
            return CodexResult(
                payload, completed.stderr, completed.stdout,
                parse_mcp_diagnostics(completed.stderr),
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
        if role == "planner" and any(term in text for term in (
            "invalid_json_schema", "invalid schema for response_format",
            "structured output schema", "text.format.schema",
        )):
            return ErrorCode.PLANNER_SCHEMA_ERROR
        if any(term in text for term in ("unauthorized", "authentication", "invalid api key", "login required")):
            return ErrorCode.REVIEWER_PROCESS_ERROR if role == "reviewer" else ErrorCode.PLANNER_PROCESS_ERROR
        if any(term in text for term in ("websocket", "wss://", "transport channel closed", "transport error")):
            return ErrorCode.REVIEWER_NETWORK_ERROR if role == "reviewer" else ErrorCode.PLANNER_TRANSPORT_ERROR
        if any(term in text for term in ("network unavailable", "connection refused", "connection failed", "dns", "timed out connecting")):
            return ErrorCode.REVIEWER_NETWORK_ERROR if role == "reviewer" else ErrorCode.PLANNER_NETWORK_ERROR
        return ErrorCode.REVIEWER_PROCESS_ERROR if role == "reviewer" else ErrorCode.PLANNER_PROCESS_ERROR

    @staticmethod
    def classify_transport_error(text: str, *, role: str = "reviewer") -> ErrorCode:
        """Compatibility wrapper for callers using the v0.1.4 API."""
        return CodexAdapter.classify_process_error(text, role=role)
