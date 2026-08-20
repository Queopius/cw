import io
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock

from scripts import sync_readthedocs_stable as sync


class ReadTheDocsStableSyncTests(unittest.TestCase):
    def test_rejects_branch_as_stable_release(self):
        with mock.patch.object(
            sys,
            "argv",
            ["sync_readthedocs_stable.py", "--project", "cw", "--version", "prod", "--dry-run"],
        ):
            self.assertEqual(2, sync.main())

    def test_syncs_activates_and_verifies_managed_stable_alias(self):
        responses = iter(
            [
                (202, '{"triggered": true}'),
                (404, ""),
                (200, '{"active": false, "hidden": false, "built": false}'),
                (204, ""),
                (200, '{"active": true, "hidden": false, "built": true}'),
                (200, '{"slug": "stable", "ref": "v0.14.0"}'),
            ]
        )
        with (
            mock.patch.object(sync, "_request", side_effect=responses) as request,
            mock.patch.object(sync.time, "sleep"),
            mock.patch.object(
                sys,
                "argv",
                [
                    "sync_readthedocs_stable.py",
                    "--project",
                    "cw-codex-workflow",
                    "--version",
                    "v0.14.0",
                    "--token",
                    "fixture-token",
                    "--poll-interval",
                    "0",
                ],
            ),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(0, sync.main())

        calls = request.call_args_list
        self.assertEqual("POST", calls[0].args[0])
        self.assertTrue(calls[0].args[1].endswith("/sync-versions/"))
        self.assertEqual("PATCH", calls[3].args[0])
        self.assertEqual({"active": True, "hidden": False}, calls[3].kwargs["payload"])
        self.assertNotIn("ref", calls[3].kwargs["payload"])


if __name__ == "__main__":
    unittest.main()
