from __future__ import annotations

import io
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from cw.cli.main import main
from cw.core.errors import CwError, ErrorCode
from cw.core.initialize import backup_metadata
from tests.helpers import TempRepo


class LayoutSecurityTests(unittest.TestCase):
    def fresh_repo(self, parent: Path) -> Path:
        root = parent / "application"
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        return root

    def invoke(self, root: Path, *args):
        previous = Path.cwd()
        stream = io.StringIO()
        try:
            os.chdir(root)
            with redirect_stdout(stream):
                code = main(args)
        finally:
            os.chdir(previous)
        return code, stream.getvalue()

    def test_init_rejects_symlinked_runtime_root_before_lock_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = self.fresh_repo(base)
            outside = base / "outside"; outside.mkdir()
            (root / ".cw").symlink_to(outside, target_is_directory=True)
            code, output = self.invoke(root, "init")
            self.assertEqual(1, code)
            self.assertIn("Workflow data invalid", output)
            self.assertEqual([], list(outside.iterdir()))

    def test_init_rejects_symlinked_lock_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = self.fresh_repo(base); (root / ".cw").mkdir()
            outside = base / "outside"; outside.mkdir()
            (root / ".cw/locks").symlink_to(outside, target_is_directory=True)
            code, _ = self.invoke(root, "init")
            self.assertEqual(1, code)
            self.assertEqual([], list(outside.iterdir()))

    def test_init_rejects_symlinked_static_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = self.fresh_repo(base)
            outside = base / "outside"; outside.mkdir()
            (root / ".codex").symlink_to(outside, target_is_directory=True)
            code, _ = self.invoke(root, "init")
            self.assertEqual(1, code)
            self.assertEqual([], list(outside.iterdir()))

    def test_init_does_not_follow_agents_or_template_file_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            for name, setup in (
                ("agents", lambda root, outside: (root / "AGENTS.md").symlink_to(outside)),
                ("schema", lambda root, outside: self._schema_link(root, outside)),
            ):
                with self.subTest(name=name):
                    root = self.fresh_repo(base / name)
                    outside = base / f"{name}.txt"; outside.write_text("do not change", encoding="utf-8")
                    setup(root, outside)
                    code, _ = self.invoke(root, "init")
                    self.assertEqual(1, code)
                    self.assertEqual("do not change", outside.read_text(encoding="utf-8"))

    @staticmethod
    def _schema_link(root: Path, outside: Path) -> None:
        schemas = root / ".codex/schemas"; schemas.mkdir(parents=True)
        (schemas / "ready-for-review.schema.json").symlink_to(outside)

    def test_context_rejects_symlinked_workflow_plan(self):
        repo = TempRepo()
        try:
            plan = repo.root / ".codex/workflow/phases.yaml"
            outside = repo.root.parent / "outside-plan"; outside.write_bytes(plan.read_bytes())
            plan.unlink(); plan.symlink_to(outside)
            code, _ = self.invoke(repo.root, "status")
            self.assertEqual(1, code)
        finally:
            repo.close()

    def test_backup_rejects_nested_symlink_before_creating_backup(self):
        repo = TempRepo()
        try:
            outside = repo.root.parent / "outside-log"; outside.write_text("secret", encoding="utf-8")
            (repo.root / ".cw/logs/escape").symlink_to(outside)
            with self.assertRaises(CwError) as caught:
                backup_metadata(repo.root)
            self.assertEqual(ErrorCode.SCHEMA_VALIDATION_ERROR, caught.exception.code)
            self.assertEqual([], list((repo.root / ".cw/backups").iterdir()))
        finally:
            repo.close()


if __name__ == "__main__":
    unittest.main()
