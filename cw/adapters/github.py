from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from cw.core.errors import CwError, ErrorCode
from cw.core.governance import Check, Collaborator, PullRequestSnapshot, Review


class GitHubReadClient:
    """Read GitHub governance state through the authenticated gh CLI."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _json(self, arguments: list[str], *, allow_not_found: bool = False) -> Any:
        try:
            result = subprocess.run(["gh", *arguments], cwd=self.root, stdin=subprocess.DEVNULL,
                                    text=True, encoding="utf-8", errors="replace", capture_output=True,
                                    check=False, timeout=20)
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            raise CwError("GitHub governance discovery is unavailable", ErrorCode.AUTHORIZATION_REQUIRED,
                          "Choose an explicit governance mode or restore GitHub access.", exit_code=3) from exc
        if result.returncode:
            detail = result.stderr.strip()
            if allow_not_found and ("HTTP 404" in detail or "not found" in detail.lower()): return None
            message = "GitHub token lacks governance read permission" if "HTTP 403" in detail else "GitHub governance discovery failed"
            raise CwError(message, ErrorCode.AUTHORIZATION_REQUIRED, "No repository settings were changed.",
                          details=detail, exit_code=3)
        try: return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise CwError("GitHub returned invalid governance data", ErrorCode.SCHEMA_VALIDATION_ERROR) from exc

    def snapshot(self, number: int) -> PullRequestSnapshot:
        repository = self._json(["repo", "view", "--json", "nameWithOwner"])["nameWithOwner"]
        user = self._json(["api", "user"])["login"]
        collaborators = self._json(["api", f"repos/{repository}/collaborators?affiliation=direct&per_page=100"])
        pr = self._json(["pr", "view", str(number), "--repo", repository, "--json",
                         "author,baseRefName,baseRefOid,headRefName,headRefOid,mergeable,mergeStateStatus,reviewRequests,statusCheckRollup"])
        owner, name = str(repository).split("/", 1)
        thread_data = self._json([
            "api", "graphql", "-f", f"owner={owner}", "-f", f"name={name}", "-F", f"number={number}",
            "-f", "query=query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){pullRequest(number:$number){reviewThreads(first:100){nodes{isResolved}pageInfo{hasNextPage}}}}}",
        ])
        review_data = self._json(["api", f"repos/{repository}/pulls/{number}/reviews?per_page=100"])
        protection = self._json(["api", f"repos/{repository}/branches/{pr['baseRefName']}/protection"], allow_not_found=True) or {}
        review_rule = protection.get("required_pull_request_reviews") or {}
        status_rule = protection.get("required_status_checks") or {}
        required_checks = list(status_rule.get("contexts") or [])
        required_checks.extend(item.get("context") for item in status_rule.get("checks") or [] if isinstance(item, dict))
        requested = tuple(str(item.get("login") or item.get("name")) for item in pr.get("reviewRequests") or []
                          if isinstance(item, dict) and (item.get("login") or item.get("name")))
        reviews: list[Review] = []
        for item in review_data:
            if not isinstance(item, dict): continue
            author = item.get("user") or {}
            reviews.append(Review(str(author.get("login") or ""), str(item.get("state") or ""),
                                  str(item.get("commit_id")) if item.get("commit_id") else None,
                                  str(item.get("submitted_at") or "")))
        checks = tuple(Check(str(item.get("name") or item.get("context") or ""), str(item.get("status") or ""),
                             item.get("conclusion")) for item in pr.get("statusCheckRollup") or [] if isinstance(item, dict))
        direct = tuple(Collaborator(str(item.get("login") or ""), str(item.get("role_name") or "read"))
                       for item in collaborators if isinstance(item, dict))
        threads = (((thread_data.get("data") or {}).get("repository") or {}).get("pullRequest") or {}).get("reviewThreads") or {}
        if (threads.get("pageInfo") or {}).get("hasNextPage"):
            raise CwError("GitHub conversation discovery exceeded the safe page limit",
                          ErrorCode.AUTHORIZATION_REQUIRED, "Resolve or reduce PR conversations, then retry.", exit_code=3)
        unresolved = sum(1 for item in threads.get("nodes") or [] if isinstance(item, dict) and not item.get("isResolved"))
        return PullRequestSnapshot(
            str(repository), number, str((pr.get("author") or {}).get("login") or ""), str(user),
            str(pr.get("baseRefName") or ""), str(pr.get("headRefName") or ""), str(pr.get("headRefOid") or ""),
            pr.get("mergeable") == "MERGEABLE", str(pr.get("mergeStateStatus") or "UNKNOWN"), direct, requested,
            tuple(reviews), tuple(dict.fromkeys(value for value in required_checks if value)), checks,
            int(review_rule.get("required_approving_review_count") or 0),
            str(pr.get("baseRefOid") or ""), unresolved,
        )
