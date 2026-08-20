from __future__ import annotations

import errno
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cw.core.errors import CwError
from cw.core.locking import operation_lock
from cw.core.platform import fsync_directory
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

    def test_atomic_replace_synchronizes_the_parent_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            with patch("cw.core.utils.fsync_directory") as synchronize:
                atomic_write(path, "replacement")
            synchronize.assert_called_once_with(path.parent)

    def test_unsupported_directory_fsync_is_an_explicit_safe_fallback(self):
        with patch("cw.core.platform.os.name", "posix"), patch(
            "cw.core.platform.os.open",
            side_effect=OSError(errno.EINVAL, "directory fsync unsupported"),
        ):
            fsync_directory(Path("unavailable-directory"))

    def test_unexpected_directory_fsync_error_is_not_silenced(self):
        with patch("cw.core.platform.os.name", "posix"), patch(
            "cw.core.platform.os.open",
            side_effect=OSError(errno.EPERM, "denied"),
        ), self.assertRaises(OSError):
            fsync_directory(Path("denied-directory"))


if __name__ == "__main__":
    unittest.main()
