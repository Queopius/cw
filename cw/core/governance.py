from __future__ import annotations

import hashlib
import json
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
    base_sha: str
    unresolved_conversations: int


POLICY_KEYS = {"schema_version", "mode", "configured_at", "configured_by", "source"}
LEGACY_EVIDENCE_KEYS = {
    "schema_version", "kind", "governance_mode", "repository", "pull_request",
    "base_branch", "head_branch", "sha", "authorizer", "authorized_at", "checks",
    "mergeable", "operation_id", "authorization", "remote_review_created", "result",
}
EVIDENCE_KEYS = LEGACY_EVIDENCE_KEYS | {
    "base_sha", "required_checks", "observed_checks", "unresolved_conversations",
    "policy_fingerprint",
}
INVALIDATION_KEYS = {
    "schema_version", "kind", "repository", "pull_request", "head_sha", "base_sha",
    "target_evidence", "target_sha256", "reason", "operator", "invalidated_at",
    "operation_id", "result",
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
        elif (check.conclusion or "").upper() != "SUCCESS":
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
        "base_sha": snapshot.base_sha, "unresolved_conversations": snapshot.unresolved_conversations,
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
    if snapshot.unresolved_conversations:
        raise CwError("Governance blocked by unresolved conversations", ErrorCode.AUTHORIZATION_REQUIRED,
                      "Resolve every PR conversation before promotion.", exit_code=3)
    if mode is GovernanceMode.TEAM_REVIEWED:
        review = review_diagnosis(snapshot)
        if review["status"] != "valid_approval":
            raise CwError(f"Independent review is not valid: {review['status']}", ErrorCode.AUTHORIZATION_REQUIRED,
                          "Obtain a current approval from another authorized account.", exit_code=3)
    return checks


def _require_sha(value: str, label: str) -> None:
    if re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise CwError(f"{label} is invalid", ErrorCode.SCHEMA_VALIDATION_ERROR)


def _policy_fingerprint(snapshot: PullRequestSnapshot, mode: GovernanceMode) -> str:
    payload = {
        "governance_mode": mode.value,
        "required_approvals": snapshot.required_approvals,
        "required_checks": sorted(snapshot.required_checks),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _evidence_path(root: Path, snapshot: PullRequestSnapshot, fingerprint: str, generation: int) -> Path:
    _require_sha(snapshot.sha, "Pull request head SHA")
    _require_sha(snapshot.base_sha, "Pull request base SHA")
    return (root / ".cw" / "governance" / "authorizations" /
            f"pr-{snapshot.number}-{snapshot.sha}-{snapshot.base_sha}-{fingerprint[:12]}-r{generation}.json")


def classify_authorization_evidence(data: Any) -> str:
    if not isinstance(data, dict):
        raise CwError("Governance authorization evidence is invalid", ErrorCode.SCHEMA_VALIDATION_ERROR)
    if data.get("schema_version") == 1 and set(data) == LEGACY_EVIDENCE_KEYS:
        return "LEGACY_INCOMPLETE_EVIDENCE"
    if data.get("schema_version") == 2 and set(data) == EVIDENCE_KEYS:
        return "CURRENT_AUTHORIZATION_EVIDENCE"
    raise CwError("Governance authorization evidence is invalid", ErrorCode.SCHEMA_VALIDATION_ERROR)


def _validate_evidence(data: Any) -> dict[str, Any]:
    classification = classify_authorization_evidence(data)
    if classification == "LEGACY_INCOMPLETE_EVIDENCE":
        raise CwError("Incomplete governance authorization evidence", ErrorCode.AUTHORIZATION_REQUIRED,
                      "Invalidate the legacy evidence explicitly, then authorize the live PR again.", exit_code=3)
    if data.get("kind") != "promotion_authorization" or data.get("remote_review_created") is not False:
        raise CwError("Governance authorization semantics are invalid", ErrorCode.SCHEMA_VALIDATION_ERROR)
    for key in ("repository", "base_branch", "base_sha", "head_branch", "sha", "authorizer",
                "authorized_at", "policy_fingerprint", "operation_id", "result"):
        if not isinstance(data.get(key), str) or not data[key]:
            raise CwError("Governance authorization identity is invalid", ErrorCode.SCHEMA_VALIDATION_ERROR)
    _require_sha(data["sha"], "Authorization head SHA")
    _require_sha(data["base_sha"], "Authorization base SHA")
    if not isinstance(data.get("required_checks"), list) or not isinstance(data.get("observed_checks"), list):
        raise CwError("Governance authorization checks are invalid", ErrorCode.SCHEMA_VALIDATION_ERROR)
    if not isinstance(data.get("unresolved_conversations"), int):
        raise CwError("Governance authorization conversation count is invalid", ErrorCode.SCHEMA_VALIDATION_ERROR)
    authorization = data.get("authorization")
    if not isinstance(authorization, dict) or not isinstance(authorization.get("expires_at"), str):
        raise CwError("Governance authorization grant is invalid", ErrorCode.SCHEMA_VALIDATION_ERROR)
    return data


def _authorization_candidates(root: Path, pull_request: int, head_sha: str,
                              base_sha: str | None = None) -> list[tuple[Path, dict[str, Any], str]]:
    _require_sha(head_sha, "Authorization target head SHA")
    if base_sha is not None: _require_sha(base_sha, "Authorization target base SHA")
    directory = root / ".cw" / "governance" / "authorizations"
    safe_directory(directory, ".cw/governance/authorizations")
    if not directory.exists(): return []
    matches: list[tuple[Path, dict[str, Any], str]] = []
    for path in sorted(directory.glob("*.json")):
        safe_file(path, path.relative_to(root).as_posix(), required=True)
        data = load_json(path); classification = classify_authorization_evidence(data)
        if data.get("pull_request") != pull_request or data.get("sha") != head_sha: continue
        if base_sha is not None and data.get("base_sha") != base_sha: continue
        matches.append((path, data, classification))
    return matches


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _validate_invalidation(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict) or set(data) != INVALIDATION_KEYS or data.get("schema_version") != 1:
        raise CwError("Governance invalidation evidence is invalid", ErrorCode.SCHEMA_VALIDATION_ERROR)
    if data.get("kind") != "authorization_invalidation" or data.get("result") != "INVALIDATED":
        raise CwError("Governance invalidation semantics are invalid", ErrorCode.SCHEMA_VALIDATION_ERROR)
    return data


def _invalidations(root: Path) -> list[dict[str, Any]]:
    directory = root / ".cw" / "governance" / "invalidations"
    safe_directory(directory, ".cw/governance/invalidations")
    if not directory.exists(): return []
    result: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        safe_file(path, path.relative_to(root).as_posix(), required=True)
        result.append(_validate_invalidation(load_json(path)))
    return result


def _is_invalidated(root: Path, target_sha256: str) -> bool:
    return any(item["target_sha256"] == target_sha256 for item in _invalidations(root))


def invalidate_governance_authorization(
    root: Path, *, pull_request: int, head_sha: str, reason: str, operator: str,
    base_sha: str | None = None,
) -> tuple[dict[str, Any], bool]:
    reason = reason.strip(); operator = operator.strip()
    if not reason:
        raise CwError("Governance invalidation reason is required", ErrorCode.USAGE_ERROR, exit_code=2)
    if not operator:
        raise CwError("Governance invalidation operator is required", ErrorCode.USAGE_ERROR, exit_code=2)
    matches = _authorization_candidates(root, pull_request, head_sha, base_sha)
    if not matches:
        raise CwError("Governance authorization evidence was not found", ErrorCode.AUTHORIZATION_REQUIRED, exit_code=3)
    if len(matches) != 1:
        raise CwError("Governance authorization target is ambiguous", ErrorCode.AUTHORIZATION_REQUIRED,
                      "Provide --base-sha to identify one exact authorization.", exit_code=3)
    path, evidence, _ = matches[0]; target_hash = _file_sha256(path)
    for existing in _invalidations(root):
        if existing["target_sha256"] != target_hash: continue
        if existing["reason"] == reason and existing["operator"] == operator: return existing, True
        raise CwError("Governance authorization is already invalidated", ErrorCode.AUTHORIZATION_REQUIRED,
                      "Preserve the existing invalidation evidence.", exit_code=3)
    governance_directory(root, create=True)
    directory = root / ".cw" / "governance" / "invalidations"
    safe_directory(directory, ".cw/governance/invalidations", create=True)
    identity = hashlib.sha256(f"{target_hash}\0{reason}\0{operator}".encode()).hexdigest()
    operation_id = f"governance-invalidate-{identity[:16]}"
    payload = {
        "schema_version": 1, "kind": "authorization_invalidation",
        "repository": evidence["repository"], "pull_request": pull_request,
        "head_sha": head_sha, "base_sha": evidence.get("base_sha"),
        "target_evidence": path.relative_to(root).as_posix(), "target_sha256": target_hash,
        "reason": reason, "operator": operator, "invalidated_at": utc_now(),
        "operation_id": operation_id, "result": "INVALIDATED",
    }
    destination = directory / f"{operation_id}.json"
    atomic_json_new(destination, payload)
    return payload, False


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
    fingerprint = _policy_fingerprint(snapshot, GovernanceMode.SOLO_MAINTAINER)
    directory = root / ".cw" / "governance" / "authorizations"
    safe_directory(directory, ".cw/governance/authorizations", create=True)
    generations = 0
    for candidate_path, candidate, classification in _authorization_candidates(root, snapshot.number, snapshot.sha):
        target_hash = _file_sha256(candidate_path)
        if _is_invalidated(root, target_hash): continue
        if classification == "LEGACY_INCOMPLETE_EVIDENCE":
            raise CwError("Incomplete governance authorization evidence", ErrorCode.AUTHORIZATION_REQUIRED,
                          "Run cw governance invalidate for the exact legacy evidence before authorizing again.", exit_code=3)
        if (candidate.get("base_sha") == snapshot.base_sha
                and candidate.get("base_branch") == snapshot.base_branch
                and candidate.get("head_branch") == snapshot.head_branch
                and candidate.get("policy_fingerprint") == fingerprint):
            generations += 1
            existing = _validate_evidence(candidate)
            validate_promotion_authorization(existing, snapshot, root=root)
            return existing, True
    generations = sum(
        1 for _, candidate, classification in _authorization_candidates(root, snapshot.number, snapshot.sha)
        if classification == "CURRENT_AUTHORIZATION_EVIDENCE"
        and candidate.get("base_sha") == snapshot.base_sha
        and candidate.get("base_branch") == snapshot.base_branch
        and candidate.get("head_branch") == snapshot.head_branch
        and candidate.get("policy_fingerprint") == fingerprint
    )
    path = _evidence_path(root, snapshot, fingerprint, generations)
    safe_file(path, path.relative_to(root).as_posix())
    operation_id = (f"governance-pr-{snapshot.number}-{snapshot.sha[:8]}-"
                    f"{snapshot.base_sha[:8]}-{fingerprint[:8]}-r{generations}")
    grant = issue_user_authorization(
        action="release.promote",
        resource_id=f"{snapshot.repository}#{snapshot.number}@{snapshot.sha}:{snapshot.base_sha}",
        operation_id=operation_id,
        actor=Actor(snapshot.authenticated_user.casefold(), ActorOrigin.HUMAN_CLI, explicit_user_intent=True),
    )
    payload = {
        "schema_version": 2, "kind": "promotion_authorization",
        "governance_mode": GovernanceMode.SOLO_MAINTAINER.value,
        "repository": snapshot.repository, "pull_request": snapshot.number,
        "base_branch": snapshot.base_branch, "base_sha": snapshot.base_sha,
        "head_branch": snapshot.head_branch, "sha": snapshot.sha,
        "authorizer": snapshot.authenticated_user, "authorized_at": grant.issued_at,
        "checks": checks, "required_checks": list(snapshot.required_checks),
        "observed_checks": [{"name": item.name, "status": item.status, "conclusion": item.conclusion}
                            for item in snapshot.checks],
        "mergeable": True, "unresolved_conversations": snapshot.unresolved_conversations,
        "policy_fingerprint": fingerprint, "operation_id": operation_id,
        "authorization": grant.as_evidence(), "remote_review_created": False,
        "result": "AUTHORIZED_REMOTE_BLOCKED" if snapshot.required_approvals > 0 else "AUTHORIZED",
    }
    atomic_json_new(path, payload)
    return payload, False


def validate_promotion_authorization(evidence: dict[str, Any], snapshot: PullRequestSnapshot, *, root: Path) -> None:
    evidence = _validate_evidence(evidence)
    if evidence["authorization"]["expires_at"] <= utc_now():
        raise CwError("Governance authorization has expired", ErrorCode.AUTHORIZATION_REQUIRED,
                      "Invalidate expired evidence and obtain a fresh human authorization.", exit_code=3)
    for invalidation in _invalidations(root):
        target = root / invalidation["target_evidence"]
        safe_file(target, invalidation["target_evidence"], required=True)
        if (_file_sha256(target) == invalidation["target_sha256"]
                and load_json(target).get("operation_id") == evidence["operation_id"]):
            raise CwError("Governance authorization is invalidated", ErrorCode.AUTHORIZATION_REQUIRED,
                          "Obtain a separate fresh human authorization.", exit_code=3)
    expected = {"repository": snapshot.repository, "pull_request": snapshot.number,
                "base_branch": snapshot.base_branch, "base_sha": snapshot.base_sha,
                "head_branch": snapshot.head_branch, "sha": snapshot.sha,
                "governance_mode": GovernanceMode.SOLO_MAINTAINER.value}
    if any(evidence.get(key) != value for key, value in expected.items()):
        raise CwError("Governance authorization is stale", ErrorCode.AUTHORIZATION_REQUIRED,
                      "Authorize the current PR base, head, and SHA again.", exit_code=3)
    checks = validate_promotion_preflight(snapshot, GovernanceMode.SOLO_MAINTAINER)
    observed = [{"name": item.name, "status": item.status, "conclusion": item.conclusion}
                for item in snapshot.checks]
    fingerprint = _policy_fingerprint(snapshot, GovernanceMode.SOLO_MAINTAINER)
    if (evidence["checks"] != checks or evidence["required_checks"] != list(snapshot.required_checks)
            or evidence["observed_checks"] != observed or evidence["policy_fingerprint"] != fingerprint
            or evidence["unresolved_conversations"] != snapshot.unresolved_conversations
            or evidence["result"] != "AUTHORIZED"):
        raise CwError("Governance authorization live state changed", ErrorCode.AUTHORIZATION_REQUIRED,
                      "Invalidate stale evidence and authorize the verified PR state again.", exit_code=3)


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
