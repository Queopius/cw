#!/usr/bin/env python3
"""Installed-wheel acceptance for the governed MCP application adapter."""

from __future__ import annotations

import json
import subprocess
import tempfile
import time
from pathlib import Path

from cw.adapters.mcp import MCPRuntime, RuntimeConfig
from cw.adapters.result import CodexResult
from cw.core.initialize import initialize
from cw.core.models import WorkflowState
from cw.core.state import bind_plan, load_state, transition
from cw.core.utils import atomic_json
from cw.core.workflow import load_workflow, set_plan_status, workflow_hash, write_workflow


class FakeReviewer:
    def run_reviewer(self, root, prompt, schema, timeout):
        return CodexResult({
            "decision": "APPROVE",
            "summary": "installed-wheel reviewer approved deterministic evidence",
            "criteria": [{
                "id": "P1-001", "status": "PASS",
                "evidence": ["docs/phase-1.md:1 installed-wheel evidence"],
            }],
            "blocking_criteria": [],
            "blocking_issues": [],
        }, "")


def wait(runtime: MCPRuntime, handle: str, operation_id: str) -> dict:
    deadline = time.monotonic() + 20
    poll = 0
    while time.monotonic() < deadline:
        response = runtime.call_tool("cw_operation_status", {
            "project_id": handle,
            "operation_id": f"acceptance-poll-{poll}",
            "target_operation_id": operation_id,
        })
        if response["status"] not in {"QUEUED", "RUNNING"}:
            return response
        poll += 1
        time.sleep(0.01)
    raise RuntimeError(f"Installed-wheel operation did not finish: {operation_id}")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="cw-installed-mcp-") as temporary:
        root = Path(temporary) / "project"
        root.mkdir()
        subprocess.run(
            ["git", "init", "-q", str(root)],
            check=True, stdin=subprocess.DEVNULL, timeout=20,
        )
        initialize(root)
        write_workflow(root / ".codex/workflow/phases.yaml", {
            "schema_version": 1,
            "workflow": {
                "id": "project", "repository": "project", "version": 1,
                "status": "PROPOSED", "goal": "Prove installed MCP controlled actions",
            },
            "settings": {"max_review_attempts": 3, "command_timeout_seconds": 30},
            "reviewer": {"command": "codex", "timeout_seconds": 30, "sandbox": "read-only"},
            "phases": [{
                "id": "01-phase-1", "name": "Phase 1", "objective": "Deliver evidence",
                "depends_on": [], "artifacts": ["docs/phase-1.md"],
                "review_paths": ["docs/**/*"], "required_commands": [],
                "acceptance_criteria": [{
                    "id": "P1-001", "description": "Evidence is complete", "severity": "blocking",
                }],
                "blocking_criteria": [], "requires_human_approval": False,
            }],
        })
        workflow = load_workflow(root)
        state = load_state(root)
        transition(root, state, WorkflowState.PLANNING)
        bind_plan(root, state, workflow)
        transition(root, state, WorkflowState.PLAN_PROPOSED)
        set_plan_status(root, "APPROVED")
        workflow = load_workflow(root)
        state["workflow_sha256"] = workflow_hash(root / ".codex/workflow/phases.yaml")
        transition(root, state, WorkflowState.READY)
        transition(root, state, WorkflowState.IN_PROGRESS)

        runtime = MCPRuntime(
            RuntimeConfig.create([root]), diagnostic_sink=lambda _: None,
            review_backend_factory=FakeReviewer,
        )
        try:
            handle = runtime.project_handles()[0]["repository_id"]
            names = {item["name"] for item in runtime.tool_contracts()}
            forbidden = {"cw_execute", "shell", "git", "cw_create_gate", "cw_authorize_extension"}
            if names & forbidden or len(names) != 12:
                raise RuntimeError("Installed MCP registry is not the governed 0.9 surface")

            runtime.call_tool("cw_phase_start", {
                "project_id": handle, "operation_id": "acceptance-start",
            })
            started = wait(runtime, handle, "acceptance-start")
            if started["status"] != "SUCCEEDED":
                raise RuntimeError(f"Installed phase start failed: {started}")
            session_id = started["data"]["result"]["session_id"]
            artifact = root / "docs/phase-1.md"
            artifact.parent.mkdir()
            artifact.write_text("complete\n", encoding="utf-8")
            atomic_json(root / ".cw/runtime/READY_FOR_REVIEW.json", {
                "schema_version": 1,
                "session_id": session_id,
                "phase": "01-phase-1",
                "status": "READY_FOR_REVIEW",
                "artifacts": ["docs/phase-1.md"],
                "checks_executed": [],
            })

            runtime.call_tool("cw_validate", {
                "project_id": handle, "operation_id": "acceptance-validation",
            })
            validation = wait(runtime, handle, "acceptance-validation")
            if validation["data"]["result"]["validation_status"] != "PASSED":
                raise RuntimeError(f"Installed validation failed: {validation}")

            runtime.call_tool("cw_request_review", {
                "project_id": handle, "operation_id": "acceptance-review",
            })
            review = wait(runtime, handle, "acceptance-review")
            if review["status"] != "SUCCEEDED" or review["data"]["result"]["decision"] != "APPROVE":
                raise RuntimeError(f"Installed review failed: {review}")
            if not (root / ".cw/gates/01-phase-1.approved.json").is_file():
                raise RuntimeError("Installed supervisor did not create the valid gate")
            if load_state(root)["status"] != WorkflowState.COMPLETED.value:
                raise RuntimeError("Installed controlled lifecycle did not complete")
        finally:
            runtime.shutdown()
    print(json.dumps({
        "schema_version": 1,
        "status": "PASS",
        "acceptance": "installed-wheel-mcp-controlled-actions",
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
