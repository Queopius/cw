from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from .authorization import Actor, ActorOrigin, issue_user_authorization
from .errors import CwError, ErrorCode
from .layout import safe_directory, safe_file
from .utils import atomic_json, atomic_json_new, load_json, utc_now


class GovernanceMode(str, Enum):
    SOLO_MAINTAINER = "solo-maintainer"
    TEAM_REVIEWED = "team-reviewed"


@dataclass(frozen=True, slots=True)
class Collaborator:
    login: str
    permission: str

    @property
    def can_review(self) -> bool:
        return self.permission in {"write", "maintain", "admin"}


@dataclass(frozen=True, slots=True)
class Review:
    reviewer: str
    state: str
    commit_sha: str | None
    submitted_at: str


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    status: str
    conclusion: str | None


@dataclass(frozen=True, slots=True)
class PullRequestSnapshot:
    repository: str
    number: int
    author: str
    authenticated_user: str
    base_branch: str
    head_branch: str
    sha: str
    mergeable: bool
    merge_state: str
    collaborators: tuple[Collaborator, ...]
    requested_reviewers: tuple[str, ...]
    reviews: tuple[Review, ...]
    required_checks: tuple[str, ...]
    checks: tuple[Check, ...]
    required_approvals: int


POLICY_KEYS = {"schema_version", "mode", "configured_at", "configured_by", "source"}
EVIDENCE_KEYS = {
    "schema_version", "kind", "governance_mode", "repository", "pull_request",
    "base_branch", "head_branch", "sha", "authorizer", "authorized_at", "checks",
    "mergeable", "operation_id", "authorization", "remote_review_created", "result",
}


def governance_directory(root: Path, *, create: bool = False) -> Path:
    return safe_directory(root / ".cw" / "governance", ".cw/governance", create=create)


def policy_path(root: Path) -> Path:
    return root / ".cw" / "governance" / "policy.json"


def _validate_policy(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict) or set(data) != POLICY_KEYS or data.get("schema_version") != 1:
        raise CwError("Governance policy is invalid", ErrorCode.SCHEMA_VALIDATION_ERROR)
    try:
        GovernanceMode(str(data["mode"]))
    except (KeyError, ValueError) as exc:
        raise CwError("Governance mode is invalid", ErrorCode.SCHEMA_VALIDATION_ERROR) from exc
    for key in ("configured_at", "configured_by", "source"):
        if not isinstance(data.get(key), str) or not data[key]:
            raise CwError("Governance policy identity is invalid", ErrorCode.SCHEMA_VALIDATION_ERROR)
    return data


def load_governance_policy(root: Path) -> dict[str, Any] | None:
    governance_directory(root)
    path = policy_path(root)
    safe_file(path, ".cw/governance/policy.json")
    if not path.is_file():
        return None
    return _validate_policy(load_json(path))


def configure_governance(
    root: Path, mode: GovernanceMode, *, actor: str, source: str, replace: bool = False,
) -> tuple[dict[str, Any], bool]:
    existing = load_governance_policy(root)
    if existing is not None:
        if existing["mode"] == mode.value:
            return existing, True
        if not replace:
            raise CwError(
                "Governance is already configured", ErrorCode.AUTHORIZATION_REQUIRED,
                "Run again with --replace only after explicitly choosing the new mode.", exit_code=3,
            )
    governance_directory(root, create=True)
    payload = {
        "schema_version": 1, "mode": mode.value, "configured_at": utc_now(),
        "configured_by": actor, "source": source,
    }
    atomic_json(policy_path(root), payload)
    return payload, False


def eligible_reviewers(snapshot: PullRequestSnapshot) -> tuple[str, ...]:
    author = snapshot.author.casefold()
    return tuple(sorted({
        item.login for item in snapshot.collaborators
        if item.can_review and item.login.casefold() != author
    }, key=str.casefold))


def recommend_mode(snapshot: PullRequestSnapshot) -> GovernanceMode:
    return GovernanceMode.TEAM_REVIEWED if eligible_reviewers(snapshot) else GovernanceMode.SOLO_MAINTAINER


def _latest_reviews(snapshot: PullRequestSnapshot) -> dict[str, Review]:
    latest: dict[str, Review] = {}
    for review in sorted(snapshot.reviews, key=lambda item: item.submitted_at):
        latest[review.reviewer.casefold()] = review
    return latest


def review_diagnosis(snapshot: PullRequestSnapshot) -> dict[str, Any]:
    authorized = {value.casefold() for value in eligible_reviewers(snapshot)}
    valid: list[str] = []
    stale: list[str] = []
    invalid: list[str] = []
    changes_requested: list[str] = []
    for review in _latest_reviews(snapshot).values():
        state = review.state.upper()
        reviewer = review.reviewer
        if state == "CHANGES_REQUESTED":
            changes_requested.append(reviewer)
        elif state == "APPROVED" and reviewer.casefold() == snapshot.author.casefold():
            invalid.append(reviewer)
        elif state == "APPROVED" and reviewer.casefold() not in authorized:
            invalid.append(reviewer)
        elif state == "APPROVED" and review.commit_sha != snapshot.sha:
            stale.append(reviewer)
        elif state == "APPROVED":
            valid.append(reviewer)
    if valid:
        status = "valid_approval"
    elif changes_requested:
        status = "changes_requested"
    elif stale:
        status = "stale_approval"
    elif invalid:
        status = "invalid_approval"
    elif snapshot.requested_reviewers:
        status = "review_pending"
    else:
        status = "review_missing"
    return {
        "status": status, "valid": sorted(valid, key=str.casefold),
        "stale": sorted(stale, key=str.casefold), "invalid": sorted(invalid, key=str.casefold),
        "changes_requested": sorted(changes_requested, key=str.casefold),
        "requested": sorted(snapshot.requested_reviewers, key=str.casefold),
    }


def check_diagnosis(snapshot: PullRequestSnapshot) -> dict[str, Any]:
    observed = {check.name: check for check in snapshot.checks}
    missing: list[str] = []
    pending: list[str] = []
    failed: list[str] = []
    passed: list[str] = []
    for name in snapshot.required_checks:
        check = observed.get(name)
        if check is None:
            missing.append(name)
        elif check.status.upper() != "COMPLETED":
            pending.append(name)
        elif (check.conclusion or "").upper() not in {"SUCCESS", "NEUTRAL", "SKIPPED"}:
            failed.append(name)
        else:
            passed.append(name)
    return {
        "required": len(snapshot.required_checks), "passed": len(passed),
        "missing": missing, "pending": pending, "failed": failed,
        "status": "PASS" if snapshot.required_checks and not (missing or pending or failed) else "BLOCKED",
    }


def governance_diagnosis(snapshot: PullRequestSnapshot, configured: dict[str, Any] | None) -> dict[str, Any]:
    recommended = recommend_mode(snapshot)
    selected = configured["mode"] if configured else None
    review = review_diagnosis(snapshot)
    checks = check_diagnosis(snapshot)
    blockers: list[str] = []
    if selected is None: blockers.append("configuration")
    if checks["status"] != "PASS": blockers.append("ci")
    if not snapshot.mergeable: blockers.append("mergeability")
    if selected == GovernanceMode.TEAM_REVIEWED.value and review["status"] != "valid_approval": blockers.append("review")
    if selected == GovernanceMode.SOLO_MAINTAINER.value and snapshot.required_approvals > 0: blockers.append("remote_configuration")
    no_other_reviewer = not eligible_reviewers(snapshot)
    if no_other_reviewer and snapshot.required_approvals > 0: blockers.append("no_authorized_reviewer")
    return {
        "repository": snapshot.repository, "pull_request": snapshot.number, "sha": snapshot.sha,
        "base_branch": snapshot.base_branch, "head_branch": snapshot.head_branch,
        "author": snapshot.author, "authenticated_user": snapshot.authenticated_user,
        "configured_mode": selected, "recommended_mode": recommended.value,
        "eligible_reviewers": list(eligible_reviewers(snapshot)),
        "no_other_authorized_reviewer": no_other_reviewer, "checks": checks, "review": review,
        "mergeable": snapshot.mergeable, "merge_state": snapshot.merge_state,
        "required_approvals": snapshot.required_approvals, "blockers": blockers,
        "remote_changes_made": False,
    }


def validate_promotion_preflight(snapshot: PullRequestSnapshot, mode: GovernanceMode) -> dict[str, Any]:
    checks = check_diagnosis(snapshot)
    if checks["status"] != "PASS":
        raise CwError("Governance blocked by required checks", ErrorCode.AUTHORIZATION_REQUIRED,
                      "Resolve missing, pending, or failed required checks before promotion.", exit_code=3)
    if not snapshot.mergeable:
        raise CwError("Governance blocked because the PR is not mergeable", ErrorCode.AUTHORIZATION_REQUIRED,
                      "Resolve merge conflicts or repository policy blockers before promotion.", exit_code=3)
    if mode is GovernanceMode.TEAM_REVIEWED:
        review = review_diagnosis(snapshot)
        if review["status"] != "valid_approval":
            raise CwError(f"Independent review is not valid: {review['status']}", ErrorCode.AUTHORIZATION_REQUIRED,
                          "Obtain a current approval from another authorized account.", exit_code=3)
    return checks


def _evidence_path(root: Path, snapshot: PullRequestSnapshot) -> Path:
    if re.fullmatch(r"[0-9a-f]{40}", snapshot.sha) is None:
        raise CwError("Pull request SHA is invalid", ErrorCode.SCHEMA_VALIDATION_ERROR)
    return root / ".cw" / "governance" / "authorizations" / f"pr-{snapshot.number}-{snapshot.sha}.json"


def _validate_evidence(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict) or set(data) != EVIDENCE_KEYS or data.get("schema_version") != 1:
        raise CwError("Governance authorization evidence is invalid", ErrorCode.SCHEMA_VALIDATION_ERROR)
    if data.get("kind") != "promotion_authorization" or data.get("remote_review_created") is not False:
        raise CwError("Governance authorization semantics are invalid", ErrorCode.SCHEMA_VALIDATION_ERROR)
    return data


def authorize_solo_promotion(root: Path, snapshot: PullRequestSnapshot) -> tuple[dict[str, Any], bool]:
    policy = load_governance_policy(root)
    if policy is None or policy["mode"] != GovernanceMode.SOLO_MAINTAINER.value:
        raise CwError("Solo-maintainer governance is not configured", ErrorCode.AUTHORIZATION_REQUIRED,
                      "Run: cw governance configure --mode solo-maintainer", exit_code=3)
    owner = snapshot.repository.split("/", 1)[0]
    if snapshot.authenticated_user.casefold() != owner.casefold():
        raise CwError("Only the repository owner can authorize solo-maintainer promotion",
                      ErrorCode.AUTHORIZATION_REQUIRED, exit_code=3)
    checks = validate_promotion_preflight(snapshot, GovernanceMode.SOLO_MAINTAINER)
    path = _evidence_path(root, snapshot)
    safe_directory(path.parent, ".cw/governance/authorizations", create=True)
    safe_file(path, path.relative_to(root).as_posix())
    if path.is_file():
        return _validate_evidence(load_json(path)), True
    operation_id = f"governance-pr-{snapshot.number}-{snapshot.sha[:12]}"
    grant = issue_user_authorization(
        action="release.promote", resource_id=f"{snapshot.repository}#{snapshot.number}@{snapshot.sha}",
        operation_id=operation_id,
        actor=Actor(snapshot.authenticated_user.casefold(), ActorOrigin.HUMAN_CLI, explicit_user_intent=True),
    )
    payload = {
        "schema_version": 1, "kind": "promotion_authorization",
        "governance_mode": GovernanceMode.SOLO_MAINTAINER.value,
        "repository": snapshot.repository, "pull_request": snapshot.number,
        "base_branch": snapshot.base_branch, "head_branch": snapshot.head_branch, "sha": snapshot.sha,
        "authorizer": snapshot.authenticated_user, "authorized_at": grant.issued_at,
        "checks": checks, "mergeable": True, "operation_id": operation_id,
        "authorization": grant.as_evidence(), "remote_review_created": False,
        "result": "AUTHORIZED_REMOTE_BLOCKED" if snapshot.required_approvals > 0 else "AUTHORIZED",
    }
    atomic_json_new(path, payload)
    return payload, False


def validate_promotion_authorization(evidence: dict[str, Any], snapshot: PullRequestSnapshot) -> None:
    evidence = _validate_evidence(evidence)
    expected = {"repository": snapshot.repository, "pull_request": snapshot.number,
                "base_branch": snapshot.base_branch, "head_branch": snapshot.head_branch, "sha": snapshot.sha}
    if any(evidence.get(key) != value for key, value in expected.items()):
        raise CwError("Governance authorization is stale", ErrorCode.AUTHORIZATION_REQUIRED,
                      "Authorize the current PR base, head, and SHA again.", exit_code=3)
    validate_promotion_preflight(snapshot, GovernanceMode.SOLO_MAINTAINER)


def remote_protection_plan(snapshot: PullRequestSnapshot, mode: GovernanceMode) -> dict[str, Any]:
    desired = 0 if mode is GovernanceMode.SOLO_MAINTAINER else max(1, snapshot.required_approvals)
    return {
        "repository": snapshot.repository, "branch": snapshot.base_branch, "governance_mode": mode.value,
        "current_required_approvals": snapshot.required_approvals, "desired_required_approvals": desired,
        "required_checks_preserved": list(snapshot.required_checks), "pull_request_required": True,
        "force_pushes_allowed": False, "branch_deletion_allowed": False, "remote_changes_made": False,
        "instructions": (f"Open GitHub Settings > Rules > Rulesets for {snapshot.repository}; edit only the "
                         f"approval count for {snapshot.base_branch} from {snapshot.required_approvals} to {desired}. "
                         "Preserve every required check and protection."),
    }
