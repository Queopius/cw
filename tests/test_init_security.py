from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from cw.checks.deterministic import load_readiness
from cw.core.errors import CwError, ErrorCode
from cw.core.initialize import initialize, repair
from cw.core.project import load_project
from cw.core.utils import safe_project_path
from cw.planning.planner import Planner


class InitAndSecurityTests(unittest.TestCase):
    def repo(self, parent: Path, name: str) -> Path:
        root = parent / name
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        return root

    def test_clean_init(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self.repo(Path(temporary), "alpha")
            project, created = initialize(root)
            self.assertTrue(created)
            self.assertEqual("alpha", project.project_id)
            self.assertTrue((root / ".codex/hooks/phase_gate.py").is_file())
            self.assertTrue((root / ".cw/project.json").is_file())

    def test_idempotent_init(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self.repo(Path(temporary), "alpha")
            initialize(root); _, created = initialize(root)
            self.assertFalse(created)
            self.assertEqual(1, (root / "AGENTS.md").read_text().count("<!-- CW:BEGIN -->"))

    def test_two_repositories_are_isolated(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            a, b = self.repo(base, "alpha"), self.repo(base, "bravo")
            pa, _ = initialize(a); pb, _ = initialize(b)
            plan_a = Planner().propose_plan(a, pa.project_id, "Build an invoice API")
            plan_b = Planner().propose_plan(b, pb.project_id, "Build a search index")
            self.assertNotEqual(pa.project_id, pb.project_id)
            self.assertNotEqual(plan_a["phases"][1]["id"], plan_b["phases"][1]["id"])
            self.assertNotEqual(json.loads((a / ".cw/state.json").read_text())["workflow_id"], json.loads((b / ".cw/state.json").read_text())["workflow_id"])
            (a / ".cw/gates/private.approved.json").write_text("{}")
            self.assertFalse((b / ".cw/gates/private.approved.json").exists())

    def test_project_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self.repo(Path(temporary), "alpha")
            initialize(root)
            identity = json.loads((root / ".cw/project.json").read_text())
            identity["project_id"] = "foreign"
            (root / ".cw/project.json").write_text(json.dumps(identity))
            with self.assertRaises(CwError):
                load_project(root)

    def test_same_basename_different_repository_fails_fingerprint(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            first = self.repo(base / "one", "same")
            second = self.repo(base / "two", "same")
            initialize(first); initialize(second)
            (second / ".cw/project.json").write_bytes((first / ".cw/project.json").read_bytes())
            with self.assertRaises(CwError):
                load_project(second)

    def test_repository_move_preserves_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            original = self.repo(base, "alpha")
            initialize(original)
            moved = base / "nested" / "alpha"
            moved.parent.mkdir()
            original.rename(moved)
            self.assertEqual("alpha", load_project(moved).project_id)

    def test_repair_backs_up_metadata_not_application(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self.repo(Path(temporary), "alpha")
            initialize(root)
            application = root / "app.txt"; application.write_text("keep")
            identity = json.loads((root / ".cw/project.json").read_text()); identity["project_id"] = "foreign"
            (root / ".cw/project.json").write_text(json.dumps(identity))
            backup = repair(root)
            self.assertEqual("keep", application.read_text())
            self.assertTrue((backup / "project.json").is_file())
            self.assertEqual("alpha", load_project(root).project_id)

    def test_repair_rebinds_plan_hash(self):
        from tests.helpers import TempRepo
        from cw.core.state import load_state, validate_state
        from cw.core.workflow import load_workflow
        repo = TempRepo()
        try:
            plan = repo.root / ".codex/workflow/phases.yaml"
            plan.write_text(plan.read_text().replace("sample-app", "foreign-app"), encoding="utf-8")
            repair(repo.root)
            validate_state(repo.root, load_state(repo.root), load_workflow(repo.root))
        finally:
            repo.close()

    def test_repair_preserves_plan_after_same_repository_rename(self):
        from tests.helpers import TempRepo
        from cw.core.state import load_state, validate_state
        from cw.core.workflow import load_workflow

        repo = TempRepo()
        try:
            moved = repo.root.with_name("renamed-app")
            repo.root.rename(moved)
            repo.root = moved
            backup = repair(moved)
            workflow = load_workflow(moved)
            state = load_state(moved)
            self.assertEqual("renamed-app", workflow.id)
            self.assertEqual(2, len(workflow.phases))
            self.assertEqual("renamed-app", state["workflow_id"])
            validate_state(moved, state, workflow)
            self.assertTrue((backup / "phases.yaml").is_file())
        finally:
            repo.close()

    def test_repair_preserves_approved_evidence_after_same_repository_rename(self):
        from tests.helpers import FakeAdapter, TempRepo, result
        from cw.agents.reviewer import run_review
        from cw.core.audit import audit_history
        from cw.core.gates import validate_gate
        from cw.core.state import load_state, validate_state
        from cw.core.workflow import load_workflow

        repo = TempRepo()
        try:
            repo.artifact(); repo.ready()
            run_review(repo.root, repo.workflow, repo.workflow.phases[0], repo.state(), FakeAdapter(result()))
            moved = repo.root.with_name("renamed-approved-app")
            repo.root.rename(moved); repo.root = moved

            repair(moved)

            workflow = load_workflow(moved)
            state = load_state(moved)
            validate_state(moved, state, workflow)
            validate_gate(moved, workflow, workflow.phases[0].id)
            self.assertEqual({"reviews": 1, "gates": 1, "events": 1}, audit_history(moved, workflow, state))
            review = json.loads((moved / state["last_review"]).read_text(encoding="utf-8"))
            gate = json.loads((moved / state["last_gate"]).read_text(encoding="utf-8"))
            self.assertEqual("renamed-approved-app", review["workflow"])
            self.assertEqual("renamed-approved-app", gate["workflow"])
        finally:
            repo.close()

    def test_repair_never_adopts_foreign_repository_workflow(self):
        from tests.helpers import TempRepo
        from cw.core.gates import create_gate
        from cw.core.state import load_state
        from cw.core.workflow import load_workflow

        source = TempRepo(name="same-name")
        target = TempRepo(name="same-name")
        try:
            source.artifact()
            review = source.approved_review()
            gate = create_gate(source.root, source.workflow, source.workflow.phases[0], review)
            (source.root / ".cw/config.toml").write_text("allow_network = true\n", encoding="utf-8")
            (source.root / ".cw/logs/source.log").write_text("foreign", encoding="utf-8")
            for relative in ("project.json", "state.json", "config.toml"):
                shutil.copy2(source.root / ".cw" / relative, target.root / ".cw" / relative)
            shutil.copy2(
                source.root / ".codex/workflow/phases.yaml",
                target.root / ".codex/workflow/phases.yaml",
            )
            shutil.copy2(source.root / review, target.root / ".cw/reviews" / Path(review).name)
            shutil.copy2(gate, target.root / ".cw/gates" / gate.name)
            shutil.copy2(source.root / ".cw/logs/source.log", target.root / ".cw/logs/source.log")

            backup = repair(target.root)

            workflow = load_workflow(target.root)
            state = load_state(target.root)
            self.assertEqual("NOT_CREATED", workflow.status)
            self.assertEqual((), workflow.phases)
            self.assertEqual("INITIALIZED", state["status"])
            self.assertEqual([], list((target.root / ".cw/reviews").iterdir()))
            self.assertEqual([], list((target.root / ".cw/gates").iterdir()))
            self.assertEqual([], list((target.root / ".cw/logs").iterdir()))
            self.assertNotIn("allow_network = true", (target.root / ".cw/config.toml").read_text(encoding="utf-8"))
            self.assertTrue((backup / "reviews" / Path(review).name).is_file())
            self.assertTrue((backup / "gates" / gate.name).is_file())
            self.assertTrue((backup / "phases.yaml").is_file())
            self.assertNotEqual(
                json.loads((source.root / ".cw/project.json").read_text())["repository_root_fingerprint"],
                json.loads((target.root / ".cw/project.json").read_text())["repository_root_fingerprint"],
            )
        finally:
            source.close()
            target.close()

    def test_init_rejects_foreign_plan(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self.repo(Path(temporary), "alpha")
            initialize(root)
            plan = root / ".codex/workflow/phases.yaml"
            plan.write_text(plan.read_text().replace('"alpha"', '"foreign"'), encoding="utf-8")
            with self.assertRaises(CwError):
                initialize(root)

    def test_init_rejects_schema_less_foreign_metadata_before_migration(self):
        from tests.helpers import TempRepo

        source = TempRepo(name="same-name")
        target = TempRepo(name="same-name")
        try:
            tracked = (".cw/project.json", ".cw/state.json", ".codex/workflow/phases.yaml")
            for relative in tracked:
                data = json.loads((source.root / relative).read_text(encoding="utf-8"))
                data.pop("schema_version")
                (target.root / relative).write_text(json.dumps(data), encoding="utf-8")
            before = {relative: (target.root / relative).read_bytes() for relative in tracked}
            with self.assertRaises(CwError) as caught:
                initialize(target.root)
            self.assertEqual(ErrorCode.WORKFLOW_PROJECT_MISMATCH, caught.exception.code)
            self.assertEqual(before, {relative: (target.root / relative).read_bytes() for relative in tracked})
            self.assertEqual([], list((target.root / ".cw/backups").iterdir()))
        finally:
            source.close()
            target.close()

    def test_legacy_state_migrates(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self.repo(Path(temporary), "alpha")
            (root / ".codex/workflow").mkdir(parents=True)
            (root / ".codex/workflow/state.json").write_text(json.dumps({"workflow_id": "alpha", "status": "IN_PROGRESS", "current_phase": "01-old"}))
            initialize(root)
            state = json.loads((root / ".cw/state.json").read_text())
            self.assertEqual("01-old", state["current_phase"])
            self.assertEqual(1, state["schema_version"])

    def test_path_traversal_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with self.assertRaises(CwError):
                safe_project_path(root, "../secret")

    def test_absolute_artifact_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(CwError):
                safe_project_path(Path(temporary).resolve(), "/etc/passwd")

    def test_symlink_escape_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "root"; root.mkdir()
            outside = Path(temporary) / "outside"; outside.write_text("secret")
            (root / "link").symlink_to(outside)
            with self.assertRaises(CwError):
                safe_project_path(root.resolve(), "link", must_exist=True)

    def test_arbitrary_manifest_command_rejected(self):
        from tests.helpers import TempRepo
        repo = TempRepo()
        try:
            repo.artifact(); repo.ready(checks=[{"command": "curl example.test", "exit_code": 0}])
            with self.assertRaises(CwError):
                load_readiness(repo.root, repo.workflow.phases[0])
        finally:
            repo.close()

    def test_distribution_has_no_project_data(self):
        forbidden = ("previous-client-name", "copied-project-phase", "/home/example-user")
        source = Path(__file__).resolve().parents[1] / "cw"
        for path in source.rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts:
                text = path.read_text(encoding="utf-8", errors="ignore").lower()
                for term in forbidden:
                    self.assertNotIn(term, text, str(path))

    def test_explicit_roadmap_drives_plan_shape(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self.repo(Path(temporary), "roadmap-app")
            (root / "ROADMAP.md").write_text(
                "# Product Roadmap\n\n## Phase 1 — Domain Foundation\n\n## Phase 2 — Public API\n\n## Phase 3 — Operations\n",
                encoding="utf-8",
            )
            project, _ = initialize(root)
            plan = Planner().propose_plan(root, project.project_id, "Deliver the roadmap")
            self.assertEqual(["01-domain-foundation", "02-public-api", "03-operations"], [phase["id"] for phase in plan["phases"]])


if __name__ == "__main__":
    unittest.main()
