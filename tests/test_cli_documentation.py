from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class CliDocumentationTests(unittest.TestCase):
    def test_reference_matches_public_parser_surface(self) -> None:
        path = ROOT / "scripts" / "check_cli_docs.py"
        spec = importlib.util.spec_from_file_location("check_cli_docs", path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader if spec else None)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)
        self.assertEqual([], module.validate())

    def test_error_reference_covers_public_error_enum(self) -> None:
        path = ROOT / "scripts" / "check_error_docs.py"
        spec = importlib.util.spec_from_file_location("check_error_docs", path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader if spec else None)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)
        self.assertEqual([], module.missing_error_codes())

    def test_documentation_local_links_and_anchors(self) -> None:
        path = ROOT / "scripts" / "check_doc_links.py"
        spec = importlib.util.spec_from_file_location("check_doc_links", path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader if spec else None)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)
        self.assertEqual([], module.broken_links())


if __name__ == "__main__":
    unittest.main()
