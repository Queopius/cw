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

    def run_implementer(self, root: Path, prompt: str) -> int:
        self._require()
        environment = os.environ.copy()
        environment["CW_IMPLEMENTER_ACTIVE"] = "1"
        return subprocess.call([
            self.command, "--cd", str(root), "--sandbox", "workspace-write",
            "--ask-for-approval", "never", "--no-alt-screen", prompt,
        ], cwd=root, env=environment)

    def run_reviewer(self, root: Path, prompt: str, schema: Path, timeout: int) -> CodexResult:
        self._require()
        with tempfile.TemporaryDirectory(prefix="cw-review-") as temporary:
            output = Path(temporary) / "result.json"
            environment = os.environ.copy()
            environment["CW_REVIEWER_ACTIVE"] = "1"
            command = [
                self.command, "--ask-for-approval", "never", "exec", "--ephemeral", "--disable", "hooks", "--sandbox", "read-only",
                "--color", "never", "--output-schema", str(schema),
                "--output-last-message", str(output), "--cd", str(root), prompt,
            ]
            try:
                completed = subprocess.run(command, cwd=root, env=environment, text=True, capture_output=True, timeout=timeout, check=False)
            except subprocess.TimeoutExpired as exc:
                raise CwError("Independent reviewer timed out", ErrorCode.REVIEW_TIMEOUT, "Run: cw retry", details=str(exc)) from exc
            if completed.returncode:
                code = self.classify_transport_error(completed.stderr + completed.stdout)
                raise CwError("Independent reviewer unavailable", code, "Run: cw retry", details=(completed.stderr + completed.stdout)[-12000:])
            try:
                payload = json.loads(output.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise CwError("Reviewer returned invalid JSON", ErrorCode.REVIEWER_PROCESS_ERROR, "Run: cw retry", details=str(exc)) from exc
            if not isinstance(payload, dict):
                raise CwError("Reviewer returned an invalid result", ErrorCode.REVIEWER_PROCESS_ERROR, "Run: cw retry")
            return CodexResult(payload, completed.stderr)

    def smoke_test(self, root: Path, schema: Path, timeout: int = 60) -> CodexResult:
        return self.run_reviewer(
            root,
            "Connectivity smoke test only. Do not inspect repository files. Return decision APPROVE, an empty criteria list, no blocking issues, and a short summary.",
            schema,
            timeout,
        )

    @staticmethod
    def classify_transport_error(text: str) -> ErrorCode:
        lowered = text.lower()
        if any(term in lowered for term in ("network", "websocket", "wss://", "https://", "connection", "dns", "transport")):
            return ErrorCode.REVIEWER_NETWORK_ERROR
        return ErrorCode.REVIEWER_PROCESS_ERROR
