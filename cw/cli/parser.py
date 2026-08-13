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
    for name in ("init", "start", "status", "validate", "retry", "version", "help", "changelog"):
        command = subcommands.add_parser(name, add_help=True)
        _common(command, suppress_defaults=True)
    plan = subcommands.add_parser("plan", add_help=True)
    _common(plan, suppress_defaults=True)
    plan.add_argument("action", nargs="?", choices=("show", "approve", "rebuild"))
    plan.add_argument("--goal")
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
