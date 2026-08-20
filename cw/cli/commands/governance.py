from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path
from typing import Callable

from cw.adapters.github import GitHubReadClient
from cw.core.errors import CwError, ErrorCode
from cw.core.governance import (GovernanceMode, authorize_solo_promotion, configure_governance,
                                governance_diagnosis, invalidate_governance_authorization,
                                load_governance_policy, recommend_mode,
                                remote_protection_plan, validate_promotion_preflight)
from cw.core.locking import operation_lock
from cw.core.project import load_project
from cw.ui.console import Console, emit_json

RootResolver = Callable[[], Path]


def _client(root: Path) -> GitHubReadClient:
    return GitHubReadClient(root)


def _required_pr(args: argparse.Namespace) -> int:
    if not args.pr or args.pr < 1: raise CwError("A positive --pr is required", ErrorCode.USAGE_ERROR, exit_code=2)
    return args.pr


def _interactive_mode(args: argparse.Namespace, root: Path, console: Console) -> tuple[GovernanceMode, str, str]:
    detected = None; warning = None
    if args.pr:
        try: detected = recommend_mode(_client(root).snapshot(args.pr))
        except CwError as exc: warning = exc.message
    console.header("Governance configuration")
    console.wrapped("How will protected changes be reviewed?")
    console.line("1. Individual maintainer")
    console.line("   The owner authorizes promotion after required checks pass.")
    console.line("2. Team with independent review")
    console.line("   Another authorized account must approve the PR.")
    console.line("3. Detect from GitHub")
    console.line("   CW inspects authorized collaborators and proposes the mode.")
    if detected: console.wrapped(f"Recommended from GitHub evidence: {detected.value}")
    elif warning: console.wrapped(f"GitHub detection unavailable: {warning}. Choose explicitly.")
    answer = input("\nSelect governance mode [1/2/3]: ").strip()
    if answer == "1": return GovernanceMode.SOLO_MAINTAINER, "explicit", getpass.getuser()
    if answer == "2": return GovernanceMode.TEAM_REVIEWED, "explicit", getpass.getuser()
    if answer == "3":
        snapshot = _client(root).snapshot(_required_pr(args))
        return recommend_mode(snapshot), "github-detection", snapshot.authenticated_user
    raise CwError("Governance selection cancelled", ErrorCode.AUTHORIZATION_REQUIRED, exit_code=3)


def _selected_mode(args: argparse.Namespace, root: Path, console: Console) -> tuple[GovernanceMode, str, str]:
    if args.mode in {mode.value for mode in GovernanceMode}: return GovernanceMode(args.mode), "explicit", getpass.getuser()
    if args.mode == "detect":
        snapshot = _client(root).snapshot(_required_pr(args))
        return recommend_mode(snapshot), "github-detection", snapshot.authenticated_user
    if args.non_interactive or not sys.stdin.isatty():
        raise CwError("Non-interactive governance configuration requires --mode", ErrorCode.USAGE_ERROR,
                      "Use --mode solo-maintainer, team-reviewed, or detect.", exit_code=2)
    return _interactive_mode(args, root, console)


def _render_diagnosis(console: Console, payload: dict[str, object]) -> None:
    console.header("Release governance")
    console.field("PR", f"#{payload['pull_request']}"); console.field("SHA", payload["sha"])
    checks = payload["checks"]
    if isinstance(checks, dict): console.field("Checks", f"{checks['passed']}/{checks['required']} {checks['status']}")
    console.field("Governance", payload["configured_mode"] or "UNCONFIGURED")
    console.field("Recommended", payload["recommended_mode"])
    if payload["no_other_authorized_reviewer"]:
        console.wrapped("No other authorized collaborator can approve this PR.")
        console.wrapped("Requiring independent approval will block every promotion. Solo-maintainer governance is recommended.")
    if payload["blockers"]: console.item("!", f"BLOCKED: {', '.join(payload['blockers'])}")
    console.wrapped("No repository settings were changed.")


def command_governance(args: argparse.Namespace, console: Console, *, root_resolver: RootResolver) -> int:
    root = root_resolver(); load_project(root)
    if args.action == "configure":
        mode, source, actor = _selected_mode(args, root, console)
        with operation_lock(root, "governance-configure"):
            policy, idempotent = configure_governance(root, mode, actor=actor, source=source, replace=args.replace)
        payload = {**policy, "idempotent": idempotent, "remote_changes_made": False}
        if args.json: emit_json(payload)
        else:
            console.item("✓", f"Governance configured: {mode.value}")
            console.wrapped("No repository settings were changed.")
        return 0
    if args.action == "invalidate":
        pull_request = _required_pr(args)
        if not args.head_sha:
            raise CwError("Governance invalidation requires --head-sha", ErrorCode.USAGE_ERROR, exit_code=2)
        if not args.reason or not args.reason.strip():
            raise CwError("Governance invalidation requires --reason", ErrorCode.USAGE_ERROR, exit_code=2)
        if not args.yes:
            if args.non_interactive or args.json or not sys.stdin.isatty():
                raise CwError("Governance invalidation requires explicit confirmation",
                              ErrorCode.AUTHORIZATION_REQUIRED,
                              "Run again with --yes after reviewing the exact evidence and reason.", exit_code=3)
            console.header("Authorization invalidation")
            console.field("PR", f"#{pull_request}"); console.field("Head SHA", args.head_sha)
            if input("\nInvalidate this authorization evidence? [y/N] ").strip().lower() not in {"y", "yes"}:
                raise CwError("Governance invalidation cancelled", ErrorCode.AUTHORIZATION_REQUIRED, exit_code=3)
        with operation_lock(root, "governance-invalidate"):
            payload, idempotent = invalidate_governance_authorization(
                root, pull_request=pull_request, head_sha=args.head_sha, base_sha=args.base_sha,
                reason=args.reason, operator=getpass.getuser(),
            )
        result = {**payload, "idempotent": idempotent, "original_evidence_preserved": True}
        if args.json: emit_json(result)
        else:
            console.item("✓", f"Authorization evidence invalidated: {payload['target_sha256']}")
            console.wrapped("The original evidence was preserved. A new human authorization is required.")
        return 0
    snapshot = _client(root).snapshot(_required_pr(args)); policy = load_governance_policy(root)
    if args.action == "diagnose":
        payload = governance_diagnosis(snapshot, policy)
        if args.json: emit_json(payload)
        else: _render_diagnosis(console, payload)
        return 3 if payload["blockers"] else 0
    if policy is None:
        raise CwError("Governance mode is not configured", ErrorCode.AUTHORIZATION_REQUIRED,
                      "Run: cw governance configure", exit_code=3)
    mode = GovernanceMode(policy["mode"])
    if args.action == "remote-plan":
        payload = remote_protection_plan(snapshot, mode)
        if args.json: emit_json(payload)
        else:
            console.header("Remote protection plan")
            console.field("Approvals", f"{payload['current_required_approvals']} -> {payload['desired_required_approvals']}")
            console.wrapped(str(payload["instructions"])); console.wrapped("No repository settings were changed.")
        return 0
    if mode is GovernanceMode.TEAM_REVIEWED:
        validate_promotion_preflight(snapshot, mode)
        raise CwError("Team-reviewed governance uses the valid GitHub approval", ErrorCode.AUTHORIZATION_REQUIRED,
                      "CW does not replace or impersonate the independent reviewer.", exit_code=3)
    checks = validate_promotion_preflight(snapshot, mode)
    if not args.yes:
        if args.non_interactive or args.json or not sys.stdin.isatty():
            raise CwError("Promotion authorization requires explicit confirmation", ErrorCode.AUTHORIZATION_REQUIRED,
                          "Run again with --yes after reviewing the exact SHA.", exit_code=3)
        console.header("Promotion authorization")
        console.field("PR", f"#{snapshot.number}"); console.field("SHA", snapshot.sha)
        console.field("Checks", f"{checks['passed']}/{checks['required']} PASS")
        console.field("Governance", "SOLO MAINTAINER")
        if input(f"\nAuthorize promotion toward {snapshot.base_branch}? [y/N] ").strip().lower() not in {"y", "yes"}:
            raise CwError("Promotion authorization cancelled", ErrorCode.AUTHORIZATION_REQUIRED, exit_code=3)
    with operation_lock(root, "governance-authorize"):
        evidence, idempotent = authorize_solo_promotion(root, snapshot)
    payload = {**evidence, "idempotent": idempotent}
    if args.json: emit_json(payload)
    else:
        console.item("✓", f"Promotion authorized for {snapshot.sha}")
        console.field("Base SHA", snapshot.base_sha)
        console.wrapped("This is CW authorization evidence, not a GitHub review.")
        if evidence["result"] == "AUTHORIZED_REMOTE_BLOCKED":
            console.wrapped("BLOCKED: GitHub still requires an impossible independent approval. Run: cw governance remote-plan --pr " + str(snapshot.number))
    return 3 if evidence["result"] == "AUTHORIZED_REMOTE_BLOCKED" else 0
