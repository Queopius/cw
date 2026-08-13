from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cw.core.build import version_diagnostics


class BuildDiagnosticTests(unittest.TestCase):
    def test_stale_source_installation_is_detectable(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            (source / ".git").mkdir()
            (source / "cw").mkdir()
            (source / "pyproject.toml").write_text("[project]\nname='codex-workflow'\n")
            with patch("cw.core.build.build_metadata", return_value={
                "commit": "installed-build", "source": "source-install",
            }), patch("cw.core.build.git_build", return_value="new-source-build"):
                result = version_diagnostics(source)
        self.assertEqual("installed-build", result["build"])
        self.assertEqual("new-source-build", result["source_build"])
        self.assertFalse(result["source_match"])


if __name__ == "__main__":
    unittest.main()
