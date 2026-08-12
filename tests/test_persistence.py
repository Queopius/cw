from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cw.core.errors import CwError
from cw.core.locking import operation_lock
from cw.core.utils import atomic_write, atomic_write_new


class PersistenceTests(unittest.TestCase):
    def test_concurrent_operation_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); (root / ".cw/locks").mkdir(parents=True)
            with operation_lock(root, "first"):
                with self.assertRaises(CwError):
                    with operation_lock(root, "second"):
                        pass

    def test_atomic_replace_failure_preserves_original(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            path.write_text("original", encoding="utf-8")
            with patch("cw.core.utils.os.replace", side_effect=OSError("simulated")):
                with self.assertRaises(OSError):
                    atomic_write(path, "replacement")
            self.assertEqual("original", path.read_text(encoding="utf-8"))

    def test_atomic_create_refuses_to_replace_existing_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "review.json"
            path.write_text("original", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                atomic_write_new(path, "replacement")
            self.assertEqual("original", path.read_text(encoding="utf-8"))
            self.assertEqual([path], list(Path(temporary).iterdir()))


if __name__ == "__main__":
    unittest.main()
