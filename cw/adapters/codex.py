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


@dataclass(frozen=True, slots=True)
class CodexResult:
    payload: dict[str, Any]
    stderr: str


class CodexAdapter:
    def __init__(self, command: str = "codex") -> None:
        self.command = command

    def check_availability(self) -> bool:
        return shutil.which(self.command) is not None

    def _require(self) -> None:
        if not self.check_availability():
            raise CwError("Codex CLI was not found", ErrorCode.CODEX_NOT_FOUND, "Install Codex and run: cw doctor")

    def run_implementer(self, root: Path, prompt: str, *, allow_network: bool = False) -> int:
        self._require()
        environment = os.environ.copy()
        environment["CW_IMPLEMENTER_ACTIVE"] = "1"
        command = [
            self.command, "--strict-config",
            "--config", f"sandbox_workspace_write.network_access={str(allow_network).lower()}",
        ]
        if not allow_network:
            command.extend(["--config", 'web_search="disabled"'])
        command.extend([
            "--cd", str(root), "--sandbox", "workspace-write",
            "--ask-for-approval", "never", "--no-alt-screen", prompt,
        ])
        return_code = subprocess.call(command, cwd=root, env=environment)
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
        with tempfile.TemporaryDirectory(prefix=f"cw-{role}-") as temporary:
            output = Path(temporary) / "result.json"
            environment = os.environ.copy()
            environment[f"CW_{role.upper()}_ACTIVE"] = "1"
            command = [
                self.command, "--strict-config", "--config", 'web_search="disabled"',
                "--config", "project_doc_max_bytes=0",
                "--ask-for-approval", "never", "exec", "--ephemeral", "--disable", "hooks", "--sandbox", "read-only",
                "--ignore-rules", "--color", "never", "--output-schema", str(schema),
                "--output-last-message", str(output), "--cd", str(root), "-" if role == "planner" else prompt,
            ]
            try:
                completed = subprocess.run(
                    command, cwd=root, env=environment, text=True, capture_output=True,
                    input=prompt if role == "planner" else None, timeout=timeout, check=False,
                )
            except subprocess.TimeoutExpired as exc:
                code = ErrorCode.REVIEW_TIMEOUT if role == "reviewer" else ErrorCode.PLAN_TIMEOUT
                label = "Independent reviewer" if role == "reviewer" else "Codex planner"
                raise CwError(f"{label} timed out", code, "Run: cw retry", details=str(exc)) from exc
            if completed.returncode:
                code = self.classify_transport_error(completed.stderr + completed.stdout, role=role)
                label = "Independent reviewer" if role == "reviewer" else "Codex planner"
                raise CwError(f"{label} unavailable", code, "Run: cw retry", details=(completed.stderr + completed.stdout)[-12000:])
            try:
                payload = json.loads(output.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                code = ErrorCode.REVIEWER_PROCESS_ERROR if role == "reviewer" else ErrorCode.PLANNER_PROCESS_ERROR
                raise CwError(f"{role.title()} returned invalid JSON", code, "Run: cw retry", details=str(exc)) from exc
            if not isinstance(payload, dict):
                code = ErrorCode.REVIEWER_PROCESS_ERROR if role == "reviewer" else ErrorCode.PLANNER_PROCESS_ERROR
                raise CwError(f"{role.title()} returned an invalid result", code, "Run: cw retry")
            return CodexResult(payload, completed.stderr)

    def run_reviewer(self, root: Path, prompt: str, schema: Path, timeout: int) -> CodexResult:
        return self._run_structured(root, prompt, schema, timeout, role="reviewer")

    def run_planner(self, root: Path, prompt: str, schema: Path, timeout: int) -> CodexResult:
        return self._run_structured(root, prompt, schema, timeout, role="planner")

    def smoke_test(self, root: Path, schema: Path, timeout: int = 60) -> CodexResult:
        return self.run_reviewer(
            root,
            "Connectivity smoke test only. Do not inspect repository files. Return decision APPROVE, an empty criteria list, no blocking issues, and a short summary.",
            schema,
            timeout,
        )

    @staticmethod
    def classify_transport_error(text: str, *, role: str = "reviewer") -> ErrorCode:
        lowered = text.lower()
        if any(term in lowered for term in ("network", "websocket", "wss://", "https://", "connection", "dns", "transport")):
            return ErrorCode.REVIEWER_NETWORK_ERROR if role == "reviewer" else ErrorCode.PLANNER_NETWORK_ERROR
        return ErrorCode.REVIEWER_PROCESS_ERROR if role == "reviewer" else ErrorCode.PLANNER_PROCESS_ERROR
