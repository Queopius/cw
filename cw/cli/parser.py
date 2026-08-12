from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="Emit stable JSON")
    parser.add_argument("--verbose", action="store_true", help="Show diagnostic detail")
    parser.add_argument("--quiet", action="store_true", help="Suppress normal text output")
    parser.add_argument("--no-color", action="store_true", help="Disable ANSI color")


def build_parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="cw", add_help=False)
    _common(root)
    subcommands = root.add_subparsers(dest="command")
    for name in ("init", "start", "status", "validate", "retry", "config", "version", "help"):
        command = subcommands.add_parser(name, add_help=True)
        _common(command)
    plan = subcommands.add_parser("plan", add_help=True)
    _common(plan)
    plan.add_argument("action", nargs="?", choices=("show", "approve", "rebuild"))
    plan.add_argument("--goal")
    review = subcommands.add_parser("review", add_help=True)
    _common(review)
    review.add_argument("--hook", action="store_true", help=argparse.SUPPRESS)
    review.add_argument("--human-approve", action="store_true", help="Approve a pending human gate")
    history = subcommands.add_parser("history", add_help=True)
    _common(history)
    history.add_argument("--phase")
    doctor = subcommands.add_parser("doctor", add_help=True)
    _common(doctor)
    doctor.add_argument("--reviewer", action="store_true", help="Include a live reviewer connectivity check")
    error = subcommands.add_parser("error", add_help=True)
    _common(error)
    error.add_argument("--raw", action="store_true")
    repair = subcommands.add_parser("repair", add_help=True)
    _common(repair)
    repair.add_argument("--reopen", metavar="PHASE", help="Back up gates and explicitly reopen a phase")
    return root


def normalized_argv(argv: Sequence[str] | None = None) -> list[str]:
    values = list(argv if argv is not None else sys.argv[1:])
    if not values:
        return ["start"]
    if values in (["-h"], ["--help"]):
        return ["help"]
    return values


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    return build_parser().parse_args(normalized_argv(argv))
