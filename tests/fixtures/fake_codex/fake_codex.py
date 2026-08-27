#!/usr/bin/env python3
"""Deterministic external Codex process double for CW acceptance tests.

It implements only observable CLI/process contracts used by CW.  It never
emits model reasoning and is intentionally unsuitable as a production backend.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

_HOOK_CONTRACT_FAILURE = 46
_HOOK_FAILURE_MESSAGE = "fake reviewer hook contract failed"


def _option(arguments: list[str], name: str) -> str | None:
    try:
        return arguments[arguments.index(name) + 1]
    except (ValueError, IndexError):
        return None


def _scenario() -> str:
    return os.environ.get("CW_FAKE_CODEX_SCENARIO", "success").strip().lower()


def _write_output(arguments: list[str], payload: Any) -> None:
    name = _option(arguments, "--output-last-message")
    if not name:
        raise SystemExit("fake Codex requires --output-last-message for structured roles")
    path = Path(name)
    if _scenario() == "malformed_output":
        path.write_text("{not-json", encoding="utf-8")
    else:
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _emit(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False), flush=True)


def _acceptance_cw_executable() -> Path:
    """Resolve the exact installed CW binary without accepting an external alias."""

    executable_value = os.environ.get("CW_ACCEPTANCE_CW_EXECUTABLE")
    runtime_value = os.environ.get("CW_ACCEPTANCE_RUNTIME_ROOT")
    if not executable_value or not runtime_value:
        raise ValueError("acceptance CW identity is unavailable")
    executable_path = Path(executable_value)
    runtime_path = Path(runtime_value)
    if not executable_path.is_absolute() or not runtime_path.is_absolute():
        raise ValueError("acceptance CW identity is invalid")
    if executable_path.is_symlink() or runtime_path.is_symlink():
        raise ValueError("acceptance CW identity is not canonical")
    executable = executable_path.resolve(strict=True)
    runtime = runtime_path.resolve(strict=True)
    metadata = executable.stat()
    if not executable.is_file() or metadata.st_nlink != 1 or not os.access(executable, os.X_OK):
        raise ValueError("acceptance CW executable is unsafe")
    if not os.path.samefile(executable.parent.parent, runtime):
        raise ValueError("acceptance CW executable is outside its runtime")
    return executable


def _valid_hook_response(value: str) -> bool:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return False
    return (
        isinstance(payload, dict)
        and bool(payload)
        and payload.get("continue") is False
        and isinstance(payload.get("stopReason"), str)
    )


def _review_hook(root: Path, environment: dict[str, str]) -> int:
    try:
        executable = _acceptance_cw_executable()
        completed = subprocess.run(
            [str(executable), "review", "--hook"], cwd=root, env=environment,
            text=True, capture_output=True, timeout=60, check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        print(_HOOK_FAILURE_MESSAGE, file=sys.stderr)
        return _HOOK_CONTRACT_FAILURE
    if completed.returncode != 0 or not _valid_hook_response(completed.stdout):
        print(_HOOK_FAILURE_MESSAGE, file=sys.stderr)
        return _HOOK_CONTRACT_FAILURE
    return 0


def _plan(root: Path) -> dict[str, Any]:
    count = int(os.environ.get("CW_FAKE_CODEX_PHASES", "1"))
    phases: list[dict[str, Any]] = []
    previous: str | None = None
    for index in range(1, count + 1):
        phase_id = f"{index:02d}-acceptance-{index}"
        phases.append({
            "id": phase_id,
            "name": f"Acceptance phase {index}",
            "objective": f"Create deterministic acceptance artifact {index}",
            "depends_on": [previous] if previous else [],
            "artifacts": [f"artifacts/phase-{index}.txt"],
            "review_paths": ["artifacts/**/*"],
            "required_commands": [],
            "acceptance_criteria": [{
                "id": f"ACC-{index:02d}-001",
                "severity": "blocking",
                "description": f"Acceptance artifact {index} exists and is reviewable",
            }],
            "blocking_criteria": [],
            "requires_human_approval": False,
        })
        previous = phase_id
    return {
        "completion_target": {
            "id": "functional-prototype", "name": "Functional Prototype",
            "description": "Prove the deterministic acceptance goal.",
            "target_type": "functional-prototype",
            "requirements": [
                {
                    "id": "FUNCTIONAL_BEHAVIOR", "description": "The declared behavior works end to end.",
                    "severity": "blocking", "evidence_expectations": ["Executable acceptance evidence"],
                    "project_specific": False,
                },
                {
                    "id": "VERIFICATION_BASELINE", "description": "Deterministic verification passes.",
                    "severity": "blocking", "evidence_expectations": ["Current test evidence"],
                    "project_specific": False,
                },
            ],
        },
        "phases": phases,
    }


def _review(root: Path) -> dict[str, Any]:
    state = json.loads((root / ".cw/state.json").read_text(encoding="utf-8"))
    plan = json.loads((root / ".codex/workflow/phases.yaml").read_text(encoding="utf-8"))
    phase = next(item for item in plan["phases"] if item["id"] == state["current_phase"])
    artifact = phase["artifacts"][0]
    revise = _scenario() == "semantic_revision"
    criteria = [{
        "id": item["id"],
        "status": "FAIL" if revise else "PASS",
        "evidence": [f"{artifact}:1 deterministic fake-Codex evidence"],
    } for item in phase["acceptance_criteria"]]
    return {
        "decision": "REVISE" if revise else "APPROVE",
        "criteria": criteria,
        "blocking_criteria": [{
            "description": description,
            "status": "FAIL" if revise else "PASS",
            "evidence": [f"{artifact}:1 deterministic fake-Codex evidence"],
        } for description in phase.get("blocking_criteria", [])],
        "blocking_issues": ["Deterministic revision requested"] if revise else [],
        "summary": "Deterministic reviewer requested revision" if revise else "Deterministic reviewer approved",
    }


def _completion_review(root: Path) -> dict[str, Any]:
    plan = json.loads((root / ".codex/workflow/phases.yaml").read_text(encoding="utf-8"))
    contract = plan["completion_target"]
    artifact = plan["phases"][-1]["artifacts"][0]
    extend = _scenario() == "completion_extension"
    missing = contract["requirements"][0]["id"] if extend else None
    results = [{
        "id": item["id"],
        "status": "MISSING" if item["id"] == missing else "VERIFIED",
        "evidence": ["MISSING: deterministic extension evidence"] if item["id"] == missing else [f"{artifact}:1 deterministic completion evidence"],
        "rationale": "Missing in the extension scenario" if item["id"] == missing else "Verified by deterministic acceptance evidence",
    } for item in contract["requirements"]]
    return {
        "decision": "EXTENSION_REQUIRED" if extend else "SATISFIED",
        "contract_results": results,
        "system_findings": [] if not extend else [{
            "category": "acceptance", "severity": "blocking",
            "summary": "Deterministic extension required",
            "evidence": ["MISSING: deterministic extension evidence"],
            "requirement_ids": [missing],
        }],
        "missing_evidence": [] if not extend else ["Deterministic extension evidence"],
        "extension_recommendation": {
            "rationale": "" if not extend else "Add one acceptance hardening phase",
            "requirement_ids": [] if not extend else [missing],
        },
        "summary": "Deterministic completion review satisfied" if not extend else "Deterministic completion extension required",
    }


def _extension_plan(root: Path) -> dict[str, Any]:
    plan = json.loads((root / ".codex/workflow/phases.yaml").read_text(encoding="utf-8"))
    number = len(plan["phases"]) + 1
    previous = plan["phases"][-1]["id"]
    requirement = plan["completion_target"]["requirements"][0]["id"]
    phase_id = f"{number:02d}-acceptance-hardening"
    return {"phases": [{
        "id": phase_id, "name": "Acceptance Hardening",
        "objective": "Close deterministic completion evidence gaps.",
        "depends_on": [previous], "artifacts": [f"artifacts/phase-{number}.txt"],
        "review_paths": ["artifacts/**/*"], "required_commands": [],
        "acceptance_criteria": [{
            "id": f"EXT-{number:02d}-001", "severity": "blocking",
            "description": "Completion evidence is present.",
        }],
        "blocking_criteria": [], "requires_human_approval": False,
        "expected_evidence": ["Deterministic acceptance evidence"],
        "completion_requirements": [requirement],
    }]}


def _implement(root: Path, arguments: list[str]) -> int:
    scenario = _scenario()
    if scenario == "implementer_failure":
        print("fake implementer process failure", file=sys.stderr)
        return 41
    if scenario == "implementer_timeout":
        time.sleep(float(os.environ.get("CW_FAKE_CODEX_SLEEP", "30")))
        return 0
    state = json.loads((root / ".cw/state.json").read_text(encoding="utf-8"))
    plan = json.loads((root / ".codex/workflow/phases.yaml").read_text(encoding="utf-8"))
    phase = next(item for item in plan["phases"] if item["id"] == state["current_phase"])
    session_id = os.environ.get("CW_IMPLEMENTER_SESSION", "")
    if "--json" in arguments:
        _emit({"type": "thread.started", "thread_id": "fake-session"})
        _emit({"type": "turn.started"})
    artifacts: list[str] = []
    for relative in phase["artifacts"]:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"Completed {phase['id']} — São Paulo\n", encoding="utf-8", newline="\n")
        artifacts.append(relative)
        if "--json" in arguments:
            _emit({
                "type": "item.completed",
                "item": {
                    "id": f"file-{phase['id']}", "type": "file_change", "status": "completed",
                    "changes": [{"path": str(path), "kind": "add"}],
                },
            })
    if scenario != "missing_readiness":
        readiness = {
            "schema_version": 1,
            "session_id": session_id,
            "phase": phase["id"],
            "status": "READY_FOR_REVIEW",
            "artifacts": artifacts,
            "checks_executed": [],
        }
        (root / ".cw/runtime/READY_FOR_REVIEW.json").write_text(
            json.dumps(readiness, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n",
        )
        if scenario == "reviewer_infrastructure_failure":
            environment = {**os.environ, "CW_FAKE_CODEX_SCENARIO": scenario}
        else:
            environment = os.environ.copy()
        hook_result = _review_hook(root, environment)
        if hook_result:
            return hook_result
    if "--json" in arguments:
        _emit({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}})
    return 0


def main() -> int:
    arguments = sys.argv[1:]
    root = Path(_option(arguments, "--cd") or os.getcwd()).resolve()
    scenario = _scenario()
    if "doctor" in arguments:
        if scenario == "config_failure":
            print("invalid configuration in config.toml", file=sys.stderr)
            return 42
        print(json.dumps({"status": "ok", "backend": "fake-codex"}))
        return 0
    if os.environ.get("CW_PLANNER_ACTIVE") == "1":
        if scenario == "planner_timeout":
            time.sleep(float(os.environ.get("CW_FAKE_CODEX_SLEEP", "30")))
            return 0
        if scenario == "planner_failure":
            print("transport channel closed", file=sys.stderr)
            return 43
        _write_output(arguments, _plan(root))
        return 0
    if os.environ.get("CW_REVIEWER_ACTIVE") == "1":
        if scenario == "reviewer_infrastructure_failure":
            print("connection refused while contacting reviewer", file=sys.stderr)
            return 44
        _write_output(arguments, _review(root))
        return 0
    if os.environ.get("CW_COMPLETION_REVIEWER_ACTIVE") == "1":
        if scenario == "reviewer_infrastructure_failure":
            print("connection refused while contacting completion reviewer", file=sys.stderr)
            return 44
        _write_output(arguments, _completion_review(root))
        return 0
    if os.environ.get("CW_EXTENSION_PLANNER_ACTIVE") == "1":
        _write_output(arguments, _extension_plan(root))
        return 0
    if os.environ.get("CW_IMPLEMENTER_ACTIVE") == "1":
        return _implement(root, arguments)
    print("fake Codex was invoked without a managed CW role", file=sys.stderr)
    return 45


if __name__ == "__main__":
    raise SystemExit(main())
