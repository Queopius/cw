#!/usr/bin/env python3
"""Run CW's release-blocking, offline two-repository isolation demonstration."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", type=Path, required=True, help="HOME containing an installed ~/.local/share/cw")
    parser.add_argument("--base", type=Path, required=True, help="Empty parent for cw-demo-a and cw-demo-b")
    args = parser.parse_args()
    home = args.home.resolve()
    base = args.base.resolve()
    share = home / ".local/share/cw"
    launcher = home / ".local/bin/cw"
    current = share / "current"
    if not current.is_symlink() or not (current / "cw").is_dir() or not launcher.is_file():
        raise SystemExit("CW is not installed under the supplied HOME")
    base.mkdir(parents=True, exist_ok=True)
    roots = {"a": base / "cw-demo-a", "b": base / "cw-demo-b"}
    if any(path.exists() for path in roots.values()):
        raise SystemExit("Demo repositories already exist; choose an empty --base")

    # Import the copied installation explicitly, not the development checkout.
    sys.path.insert(0, str(current))
    from cw.adapters.codex import CodexResult
    from cw.agents.reviewer import run_review
    from cw.core.gates import validate_gate
    from cw.core.models import WorkflowState
    from cw.core.project import load_project
    from cw.core.session import create_session
    from cw.core.state import bind_plan, load_state, transition
    from cw.core.utils import atomic_json
    from cw.core.workflow import load_workflow, set_plan_status, workflow_hash, write_workflow
    from cw.planning.planner import Planner

    environment = {**os.environ, "HOME": str(home), "NO_COLOR": "1"}
    goals = {"a": "Build an invoice API", "b": "Build a search index"}
    for key, root in roots.items():
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        subprocess.run([str(launcher), "init", "--json"], cwd=root, env=environment, check=True, capture_output=True, text=True)
        project = load_project(root)
        state = load_state(root)
        transition(root, state, WorkflowState.PLANNING)
        proposal = Planner().propose_plan(root, project.project_id, goals[key])
        plan_path = root / ".codex/workflow/phases.yaml"
        write_workflow(plan_path, proposal)
        workflow = load_workflow(root)
        bind_plan(root, state, workflow)
        transition(root, state, WorkflowState.PLAN_PROPOSED)
        set_plan_status(root, "APPROVED")
        workflow = load_workflow(root)
        state["workflow_sha256"] = workflow_hash(plan_path)
        transition(root, state, WorkflowState.READY)
        transition(root, state, WorkflowState.IN_PROGRESS)

    b_snapshot = {
        relative: digest(roots["b"] / relative)
        for relative in (".cw/project.json", ".cw/state.json", ".codex/workflow/phases.yaml")
    }
    b_snapshot["gates"] = sorted(path.name for path in (roots["b"] / ".cw/gates").iterdir())

    workflow_a = load_workflow(roots["a"])
    phase_a = workflow_a.phases[0]
    for artifact in phase_a.artifacts:
        path = roots["a"] / artifact
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("Verified repository baseline for demo A.\n", encoding="utf-8")
    session = create_session(roots["a"], workflow_a, phase_a)
    atomic_json(roots["a"] / ".cw/runtime/READY_FOR_REVIEW.json", {
        "schema_version": 1,
        "session_id": session["session_id"],
        "phase": phase_a.id,
        "status": "READY_FOR_REVIEW",
        "artifacts": list(phase_a.artifacts),
        "checks_executed": [],
    })

    class Approver:
        def run_reviewer(self, *_args, **_kwargs):
            evidence = f"{phase_a.artifacts[0]}:1 verified offline demo artifact"
            return CodexResult({
                "decision": "APPROVE",
                "summary": "Offline demo approval",
                "blocking_issues": [],
                "criteria": [
                    {"id": criterion.id, "status": "PASS", "evidence": [evidence]}
                    for criterion in phase_a.acceptance_criteria
                ],
                "blocking_criteria": [
                    {"description": description, "status": "PASS", "evidence": [evidence]}
                    for description in phase_a.blocking_criteria
                ],
            }, "")

    run_review(roots["a"], workflow_a, phase_a, load_state(roots["a"]), Approver())
    validate_gate(roots["a"], workflow_a, phase_a.id)

    workflow_b = load_workflow(roots["b"])
    b_after = {
        relative: digest(roots["b"] / relative)
        for relative in (".cw/project.json", ".cw/state.json", ".codex/workflow/phases.yaml")
    }
    b_after["gates"] = sorted(path.name for path in (roots["b"] / ".cw/gates").iterdir())
    project_a, project_b = load_project(roots["a"]), load_project(roots["b"])
    checks = {
        "different_project_ids": project_a.project_id != project_b.project_id,
        "different_fingerprints": project_a.fingerprint != project_b.fingerprint,
        "different_plans": [phase.id for phase in workflow_a.phases] != [phase.id for phase in workflow_b.phases],
        "a_gate_exists": (roots["a"] / ".cw/gates" / f"{phase_a.id}.approved.json").is_file(),
        "a_gate_absent_from_b": not (roots["b"] / ".cw/gates" / f"{phase_a.id}.approved.json").exists(),
        "b_unchanged_after_a_advance": b_snapshot == b_after,
        "states_are_independent": load_state(roots["a"])["current_phase"] != load_state(roots["b"])["current_phase"],
    }
    if not all(checks.values()):
        raise SystemExit("Isolation demonstration failed: " + json.dumps(checks, sort_keys=True))
    print(json.dumps({
        "result": "PASS",
        "installation": str(share),
        "repositories": {key: str(path) for key, path in roots.items()},
        "projects": {
            "a": {"id": project_a.project_id, "fingerprint": project_a.fingerprint, "phase": load_state(roots["a"])["current_phase"]},
            "b": {"id": project_b.project_id, "fingerprint": project_b.fingerprint, "phase": load_state(roots["b"])["current_phase"]},
        },
        "plans": {"a": [phase.id for phase in workflow_a.phases], "b": [phase.id for phase in workflow_b.phases]},
        "checks": checks,
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
