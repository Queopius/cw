from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence


def _common(parser: argparse.ArgumentParser, *, suppress_defaults: bool = False) -> None:
    default = argparse.SUPPRESS if suppress_defaults else False
    parser.add_argument("--json", action="store_true", default=default, help="Emit stable JSON")
    parser.add_argument("--verbose", action="store_true", default=default, help="Show diagnostic detail")
    parser.add_argument("--quiet", action="store_true", default=default, help="Suppress normal text output")
    parser.add_argument("--no-color", action="store_true", default=default, help="Disable ANSI color")


def build_parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="cw", add_help=False)
    _common(root)
    subcommands = root.add_subparsers(dest="command")
    for name in ("init", "start", "status", "validate", "retry", "version", "help", "changelog", "explain"):
        command = subcommands.add_parser(name, add_help=True)
        _common(command, suppress_defaults=True)
    plan = subcommands.add_parser(
        "plan",
        add_help=True,
        description=(
            "Propose, inspect, amend, approve, rebuild, or rebaseline a plan. "
            "Amend is available only in PLAN_PROPOSED and never approves execution."
        ),
        epilog=(
            "Amend example: cw plan amend --file corrected-phases.yaml "
            "--expected-workflow-sha256 sha256:<current-hash>. "
            "It validates before writing, creates a backup, preserves the Completion Contract, "
            "and still requires: cw plan approve."
        ),
    )
    _common(plan, suppress_defaults=True)
    plan.add_argument("action", nargs="?", choices=("show", "approve", "rebuild", "rebaseline", "amend"))
    plan.add_argument("--goal")
    plan.add_argument(
        "--file",
        help="Repository-relative corrected workflow (JSON or safe single-document YAML) for plan amend",
    )
    plan.add_argument(
        "--expected-workflow-sha256",
        help="Required optimistic-concurrency SHA-256 for plan amend",
    )
    plan.add_argument("--reason", help="Mandatory audit reason for a plan rebaseline")
    plan.add_argument("--proposal", help="Repository-relative proposed workflow document")
    plan.add_argument("--apply", metavar="PROPOSAL_ID", help="Apply an immutable rebaseline proposal")
    plan.add_argument("--authorize", action="store_true", help="Confirm exact human authorization for --apply")
    plan.add_argument("--operation-id", help="Stable idempotency identifier for rebaseline apply")
    completion = subcommands.add_parser("completion", add_help=True)
    _common(completion, suppress_defaults=True)
    completion.add_argument("action", nargs="?", choices=("show", "review", "approve", "reject", "adopt"))
    completion.add_argument(
        "--target",
        choices=("proof-of-concept", "functional-prototype", "internal-tool", "controlled-pilot", "production", "public-release"),
        help="Readiness template for explicit legacy adoption",
    )
    review = subcommands.add_parser("review", add_help=True)
    _common(review, suppress_defaults=True)
    review.add_argument("--hook", action="store_true", help=argparse.SUPPRESS)
    review.add_argument("--human-approve", action="store_true", help="Approve a pending human gate")
    history = subcommands.add_parser("history", add_help=True)
    _common(history, suppress_defaults=True)
    history.add_argument("--phase")
    doctor = subcommands.add_parser("doctor", add_help=True)
    _common(doctor, suppress_defaults=True)
    doctor.add_argument("--reviewer", action="store_true", help="Include a live reviewer connectivity check")
    doctor.add_argument("--integrations", action="store_true", help="Check configured Codex integrations")
    doctor.add_argument("--codex", action="store_true", help="Show the latest sanitized managed Codex invocation")
    doctor.add_argument("--performance", action="store_true", help="Show measured managed Codex startup timings")
    doctor.add_argument("--processes", action="store_true", help="Show CW-managed process state")
    error = subcommands.add_parser("error", add_help=True)
    _common(error, suppress_defaults=True)
    error.add_argument("--raw", action="store_true")
    repair = subcommands.add_parser("repair", add_help=True)
    _common(repair, suppress_defaults=True)
    repair.add_argument("--reopen", metavar="PHASE", help="Back up gates and explicitly reopen a phase")
    config = subcommands.add_parser("config", add_help=True)
    _common(config, suppress_defaults=True)
    config.add_argument("action", nargs="?", choices=("set",))
    config.add_argument("key", nargs="?")
    config.add_argument("value", nargs="?")
    update = subcommands.add_parser("update", add_help=True)
    _common(update, suppress_defaults=True)
    update.add_argument("action", nargs="?", choices=("rollback",))
    update.add_argument("--check", action="store_true", help="Check for a release without installing")
    update.add_argument("--info", action="store_true", help="Show release information without installing")
    update.add_argument("--version", help="Install an explicit version from the configured channel")
    update.add_argument("--channel", choices=("stable", "beta", "dev"), help="Use a channel for this invocation")
    integrations = subcommands.add_parser("integrations", add_help=True)
    _common(integrations, suppress_defaults=True)
    integrations.add_argument("action", nargs="?", choices=("status", "check", "info"), default="status")
    integrations.add_argument("name", nargs="?")
    inspect = subcommands.add_parser("inspect", add_help=True)
    _common(inspect, suppress_defaults=True)
    inspect.add_argument("action", nargs="?", choices=("run", "session", "completion"), default="session")
    inspect.add_argument("run_id", nargs="?")
    logs = subcommands.add_parser("logs", add_help=True)
    _common(logs, suppress_defaults=True)
    logs.add_argument("--run", dest="run_id")
    mcp = subcommands.add_parser("mcp", add_help=True)
    _common(mcp, suppress_defaults=True)
    mcp.add_argument(
        "action", nargs="?", choices=("serve", "chatgpt-dev"), default="serve",
    )
    mcp.add_argument(
        "--project", action="append", dest="projects", metavar="PATH",
        help="Authorize an initialized CW project (repeatable; startup-only)",
    )
    mcp.add_argument(
        "--allowed-root", action="append", dest="allowed_roots", metavar="PATH",
        help="Constrain configured projects to a canonical local root (repeatable)",
    )
    mcp.add_argument(
        "--surface", choices=("read-only", "controlled-actions"), default="read-only",
        help="ChatGPT development capability profile (chatgpt-dev only)",
    )
    remote = subcommands.add_parser("remote", add_help=True)
    _common(remote, suppress_defaults=True)
    remote.add_argument("action", choices=("gateway", "pair", "grant", "agent"))
    remote.add_argument("--gateway-url", help="Canonical HTTPS CW gateway origin")
    remote.add_argument("--issuer-url", help="OAuth authorization-server issuer (gateway)")
    remote.add_argument("--resource-url", help="Canonical public MCP resource URL (gateway)")
    remote.add_argument("--jwks-url", help="OAuth authorization-server JWKS URL (gateway)")
    remote.add_argument("--database", metavar="PATH", help="Gateway transactional metadata database")
    remote.add_argument("--host", default="127.0.0.1", help="Gateway bind host (gateway)")
    remote.add_argument("--port", type=int, default=8765, help="Gateway bind port (gateway)")
    remote.add_argument("--credentials", metavar="PATH", help="Local device credential file")
    remote.add_argument("--state", metavar="PATH", help="Local opaque project-grant mapping")
    remote.add_argument("--device-name", default="CW local agent", help="Pairing display name")
    remote.add_argument("--project", action="append", dest="projects", metavar="PATH")
    remote.add_argument("--allowed-root", action="append", dest="allowed_roots", metavar="PATH")
    run = subcommands.add_parser("run", add_help=True)
    _common(run, suppress_defaults=True)
    run.add_argument("phase_count", nargs="?", type=int)
    run.add_argument("--phases", type=int)
    run.add_argument("--max-time")
    run.add_argument("--until")
    run.add_argument("--resume", action="store_true")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--yes", action="store_true")
    run.add_argument("--non-interactive", action="store_true")
    return root


def normalized_argv(argv: Sequence[str] | None = None) -> list[str]:
    values = list(argv if argv is not None else sys.argv[1:])
    if not values:
        return ["start"]
    if values in (["-h"], ["--help"]):
        return ["help"]
    return values


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    args = build_parser().parse_args(normalized_argv(argv))
    if args.command == "update":
        args.rollback = args.action == "rollback"
        selected = sum(bool(value) for value in (args.rollback, args.check, args.info, args.version))
        if selected > 1:
            build_parser().error("cw update accepts only one action")
    return args
