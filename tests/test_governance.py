from __future__ import annotations

import json
import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cw.cli.parser import parse_args
from cw.core.errors import CwError
from cw.core.governance import (Check, Collaborator, GovernanceMode, PullRequestSnapshot, Review,
                                authorize_solo_promotion, configure_governance, governance_diagnosis,
                                load_governance_policy, recommend_mode, remote_protection_plan,
                                review_diagnosis, validate_promotion_authorization, validate_promotion_preflight)

SHA = "a" * 40


def snapshot(**changes: object) -> PullRequestSnapshot:
    values: dict[str, object] = {
        "repository": "owner/repo", "number": 34, "author": "owner", "authenticated_user": "owner",
        "base_branch": "dev", "head_branch": "fix", "sha": SHA, "mergeable": True, "merge_state": "BLOCKED",
        "collaborators": (Collaborator("owner", "admin"),), "requested_reviewers": (), "reviews": (),
        "required_checks": ("CI", "Security"),
        "checks": (Check("CI", "COMPLETED", "SUCCESS"), Check("Security", "COMPLETED", "SUCCESS")),
        "required_approvals": 1,
    }
    values.update(changes)
    return PullRequestSnapshot(**values)  # type: ignore[arg-type]


class GovernanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(); self.root = Path(self.temporary.name); (self.root / ".cw").mkdir()

    def tearDown(self) -> None: self.temporary.cleanup()

    def configure(self, mode: GovernanceMode = GovernanceMode.SOLO_MAINTAINER) -> None:
        configure_governance(self.root, mode, actor="owner", source="explicit")

    def test_solo_repository_is_detected_from_authorized_accounts(self) -> None:
        self.assertEqual(GovernanceMode.SOLO_MAINTAINER, recommend_mode(snapshot()))

    def test_team_repository_requires_other_authorized_account(self) -> None:
        item = snapshot(collaborators=(Collaborator("owner", "admin"), Collaborator("reviewer", "write")))
        self.assertEqual(GovernanceMode.TEAM_REVIEWED, recommend_mode(item))

    def test_author_self_approval_is_invalid(self) -> None:
        self.assertEqual("invalid_approval", review_diagnosis(snapshot(reviews=(Review("owner", "APPROVED", SHA, "1"),)))["status"])

    def test_other_authorized_current_approval_is_valid(self) -> None:
        item = snapshot(collaborators=(Collaborator("owner", "admin"), Collaborator("reviewer", "write")),
                        reviews=(Review("reviewer", "APPROVED", SHA, "1"),))
        self.assertEqual("valid_approval", review_diagnosis(item)["status"])
        validate_promotion_preflight(item, GovernanceMode.TEAM_REVIEWED)

    def test_approval_is_stale_after_sha_change(self) -> None:
        item = snapshot(collaborators=(Collaborator("owner", "admin"), Collaborator("reviewer", "write")),
                        reviews=(Review("reviewer", "APPROVED", "b" * 40, "1"),))
        self.assertEqual("stale_approval", review_diagnosis(item)["status"])

    def test_requested_review_is_pending(self) -> None:
        self.assertEqual("review_pending", review_diagnosis(snapshot(requested_reviewers=("reviewer",)))["status"])

    def test_changes_requested_is_distinct(self) -> None:
        item = snapshot(reviews=(Review("reviewer", "CHANGES_REQUESTED", SHA, "1"),))
        self.assertEqual("changes_requested", review_diagnosis(item)["status"])

    def test_pending_check_blocks_authorization(self) -> None:
        with self.assertRaises(CwError): validate_promotion_preflight(snapshot(checks=(Check("CI", "IN_PROGRESS", None),)), GovernanceMode.SOLO_MAINTAINER)

    def test_failed_check_blocks_authorization(self) -> None:
        with self.assertRaises(CwError): validate_promotion_preflight(snapshot(checks=(Check("CI", "COMPLETED", "FAILURE"),)), GovernanceMode.SOLO_MAINTAINER)

    def test_unmergeable_pr_blocks_authorization(self) -> None:
        with self.assertRaises(CwError): validate_promotion_preflight(snapshot(mergeable=False), GovernanceMode.SOLO_MAINTAINER)

    def test_authorization_invalidates_when_base_changes(self) -> None:
        self.configure(); evidence, _ = authorize_solo_promotion(self.root, snapshot(required_approvals=0))
        with self.assertRaises(CwError): validate_promotion_authorization(evidence, snapshot(base_branch="release", required_approvals=0))

    def test_authorization_invalidates_when_sha_changes(self) -> None:
        self.configure(); evidence, _ = authorize_solo_promotion(self.root, snapshot(required_approvals=0))
        with self.assertRaises(CwError): validate_promotion_authorization(evidence, snapshot(sha="b" * 40, required_approvals=0))

    def test_missing_policy_is_diagnosed_without_mutation(self) -> None:
        before = sorted(path.relative_to(self.root) for path in self.root.rglob("*")); result = governance_diagnosis(snapshot(), None)
        self.assertIsNone(result["configured_mode"]); self.assertIn("configuration", result["blockers"])
        self.assertEqual(before, sorted(path.relative_to(self.root) for path in self.root.rglob("*")))

    def test_existing_explicit_policy_is_preserved(self) -> None:
        self.configure(GovernanceMode.TEAM_REVIEWED)
        with self.assertRaises(CwError): configure_governance(self.root, GovernanceMode.SOLO_MAINTAINER, actor="owner", source="github-detection")
        self.assertEqual("team-reviewed", load_governance_policy(self.root)["mode"])

    def test_repeated_migration_is_idempotent(self) -> None:
        first, replay1 = configure_governance(self.root, GovernanceMode.SOLO_MAINTAINER, actor="owner", source="explicit")
        second, replay2 = configure_governance(self.root, GovernanceMode.SOLO_MAINTAINER, actor="other", source="explicit")
        self.assertFalse(replay1); self.assertTrue(replay2); self.assertEqual(first, second)

    def test_authorization_evidence_is_auditable_and_secret_free(self) -> None:
        self.configure(); evidence, replay = authorize_solo_promotion(self.root, snapshot(required_approvals=0))
        self.assertFalse(replay); self.assertEqual(SHA, evidence["sha"]); self.assertEqual("owner", evidence["authorizer"])
        self.assertFalse(evidence["remote_review_created"]); serialized = json.dumps(evidence).lower()
        self.assertNotIn("token", serialized); self.assertNotIn("secret", serialized)

    def test_second_authorization_is_idempotent(self) -> None:
        self.configure(); first, _ = authorize_solo_promotion(self.root, snapshot(required_approvals=0))
        second, replay = authorize_solo_promotion(self.root, snapshot(required_approvals=0)); self.assertTrue(replay); self.assertEqual(first, second)

    def test_remote_plan_has_no_effects_and_preserves_checks(self) -> None:
        plan = remote_protection_plan(snapshot(), GovernanceMode.SOLO_MAINTAINER)
        self.assertEqual(0, plan["desired_required_approvals"]); self.assertEqual(["CI", "Security"], plan["required_checks_preserved"])
        self.assertFalse(plan["remote_changes_made"])

    def test_team_remote_plan_keeps_independent_approval(self) -> None:
        self.assertEqual(1, remote_protection_plan(snapshot(), GovernanceMode.TEAM_REVIEWED)["desired_required_approvals"])

    def test_parser_supports_explicit_noninteractive_configuration(self) -> None:
        args = parse_args(["governance", "configure", "--mode", "solo-maintainer", "--non-interactive"])
        self.assertEqual("solo-maintainer", args.mode); self.assertTrue(args.non_interactive)

    def test_parser_supports_explicit_authorization_confirmation(self) -> None:
        args = parse_args(["governance", "authorize", "--pr", "34", "--yes", "--non-interactive"])
        self.assertTrue(args.yes); self.assertEqual(34, args.pr)

    def test_github_unavailable_does_not_create_policy(self) -> None:
        from cw.cli.commands.governance import _selected_mode
        from cw.ui.console import Console
        args = parse_args(["governance", "configure", "--mode", "detect", "--pr", "34"])
        with patch("cw.cli.commands.governance._client", side_effect=CwError("offline")):
            with self.assertRaises(CwError): _selected_mode(args, self.root, Console(no_color=True))
        self.assertIsNone(load_governance_policy(self.root))

    def test_token_without_permission_fails_closed(self) -> None:
        from cw.adapters.github import GitHubReadClient
        denied = subprocess.CompletedProcess(["gh"], 1, "", "HTTP 403: Resource not accessible")
        with patch("cw.adapters.github.subprocess.run", return_value=denied):
            with self.assertRaisesRegex(CwError, "lacks governance read permission"):
                GitHubReadClient(self.root)._json(["api", "user"])

    def test_interactive_selection_requires_explicit_answer(self) -> None:
        from cw.cli.commands.governance import _interactive_mode
        from cw.ui.console import Console
        args = parse_args(["governance", "configure"])
        with patch("builtins.input", return_value="1"):
            mode, source, _ = _interactive_mode(args, self.root, Console(stream=io.StringIO(), no_color=True))
        self.assertEqual(GovernanceMode.SOLO_MAINTAINER, mode)
        self.assertEqual("explicit", source)

    def test_diagnosis_marks_impossible_team_review(self) -> None:
        result = governance_diagnosis(snapshot(), {"mode": "team-reviewed"})
        self.assertIn("review", result["blockers"]); self.assertTrue(result["no_other_authorized_reviewer"])

    def test_component_boundaries_remain_independent(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        self.assertEqual("0.14.1", (repository / "VERSION").read_text().strip())
        self.assertEqual("0.1.0", (repository / "plugins/cw/VERSION").read_text().strip())
        self.assertIn("cw.remote.v1", (repository / "cw/remote/protocol.py").read_text())


if __name__ == "__main__": unittest.main()
