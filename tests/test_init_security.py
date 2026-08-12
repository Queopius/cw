from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from cw.checks.deterministic import load_readiness
from cw.core.errors import CwError
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

    def test_init_rejects_foreign_plan(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self.repo(Path(temporary), "alpha")
            initialize(root)
            plan = root / ".codex/workflow/phases.yaml"
            plan.write_text(plan.read_text().replace('"alpha"', '"foreign"'), encoding="utf-8")
            with self.assertRaises(CwError):
                initialize(root)

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
