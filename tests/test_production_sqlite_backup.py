from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cw.remote.persistence import RemoteStore
from cw.remote.backup import create_backup, verify_backup


class ProductionSQLiteBackupTests(unittest.TestCase):
    def test_backup_records_and_verifies_required_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "gateway.sqlite3"
            backup = root / "backup.sqlite3"
            store = RemoteStore(source)
            store.close()
            payload = create_backup(source, backup, "d" * 40)
            self.assertEqual(1, payload["schema_version"])
            self.assertEqual("d" * 40, payload["deployed_sha"])
            self.assertEqual("ok", payload["integrity_result"])
            self.assertEqual(64, len(str(payload["backup_sha256"])))
            self.assertTrue(backup.with_suffix(".sqlite3.manifest.json").is_file())
            verified = verify_backup(backup, str(payload["backup_sha256"]))
            self.assertEqual("ok", verified["integrity_result"])

    def test_backup_refuses_overwrite_bad_sha_and_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "gateway.sqlite3"
            backup = root / "backup.sqlite3"
            store = RemoteStore(source)
            store.close()
            with self.assertRaises(ValueError):
                create_backup(source, backup, "short")
            payload = create_backup(source, backup, "e" * 40)
            with self.assertRaises(ValueError):
                create_backup(source, backup, "e" * 40)
            backup.write_bytes(backup.read_bytes() + b"tampered")
            with self.assertRaises(ValueError):
                verify_backup(backup, str(payload["backup_sha256"]))


if __name__ == "__main__":
    unittest.main()
