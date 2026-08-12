from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path
from typing import Any, Sequence

from cw.adapters.codex import CodexAdapter
from cw.agents.reviewer import human_approve, run_review
from cw.cli.commands import execution as execution_commands
from cw.cli.commands import lifecycle as lifecycle_commands
from cw.cli.commands import read as read_commands
from cw.core.config import apply_policy, load_policy
from cw.core.diagnostics import record_diagnostic
from cw.core.errors import CwError, ErrorCode
from cw.core.layout import validate_project_layout
from cw.core.project import load_project, repository_root
from cw.core.state import load_state, validate_state
from cw.core.workflow import load_workflow
from cw.ui.console import Console, HELP, emit_json, error_summary


MUTATING = {"init", "plan", "start", "review", "retry", "repair"}


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="Emit stable JSON")
    parser.add_argument("--verbose", action="store_true", help="Show diagnostic detail")
    parser.add_argument("--quiet", action="store_true", help="Suppress normal text output")
    parser.add_argument("--no-color", action="store_true", help="Disable ANSI color")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="cw", add_help=False)
    _common(root)
    subs = root.add_subparsers(dest="command")
    for name in ("init", "start", "status", "validate", "retry", "config", "version", "help"):
        item = subs.add_parser(name, add_help=True)
        _common(item)
    plan = subs.add_parser("plan", add_help=True)
    _common(plan)
    plan.add_argument("action", nargs="?", choices=("show", "approve", "rebuild"))
    plan.add_argument("--goal")
    review = subs.add_parser("review", add_help=True)
    _common(review)
    review.add_argument("--hook", action="store_true", help=argparse.SUPPRESS)
    review.add_argument("--human-approve", action="store_true", help="Approve a pending human gate")
    history = subs.add_parser("history", add_help=True)
    _common(history)
    history.add_argument("--phase")
    doctor = subs.add_parser("doctor", add_help=True)
    _common(doctor)
    doctor.add_argument("--reviewer", action="store_true", help="Include a live reviewer connectivity check")
    error = subs.add_parser("error", add_help=True)
    _common(error)
    error.add_argument("--raw", action="store_true")
    repair_parser = subs.add_parser("repair", add_help=True)
    _common(repair_parser)
    repair_parser.add_argument("--reopen", metavar="PHASE", help="Back up gates and explicitly reopen a phase")
    return root


def _root() -> Path:
    return repository_root(Path.cwd())


def _context(root: Path) -> tuple[Any, dict[str, Any], Any]:
    validate_project_layout(root)
    project = load_project(root)
    workflow = load_workflow(root)
    if workflow.id != project.project_id or workflow.repository != project.project_id:
        raise CwError("Project workflow mismatch", ErrorCode.WORKFLOW_PROJECT_MISMATCH, "Run: cw repair", details=f"Workflow: {workflow.repository or workflow.id}\nRepository: {project.project_id}")
    workflow = apply_policy(workflow, load_policy(root, workflow=workflow))
    state = load_state(root)
    if workflow.phases:
        validate_state(root, state, workflow)
    return project, state, workflow


def _git_branch(root: Path) -> str:
    return read_commands.git_branch(root)


def _status_payload(root: Path) -> dict[str, Any]:
    return read_commands.status_payload(root, _context)


def _render_status(console: Console, data: dict[str, Any], verbose: bool = False) -> None:
    read_commands.render_status(console, data, verbose)


def command_init(args: argparse.Namespace, console: Console) -> int:
    return lifecycle_commands.command_init(args, console, root_resolver=_root)


def command_plan(args: argparse.Namespace, console: Console) -> int:
    return lifecycle_commands.command_plan(
        args, console, root_resolver=_root, context=_context,
    )


def _current(workflow: Any, state: dict[str, Any]) -> Any:
    return execution_commands.current_phase(workflow, state)


def command_start(args: argparse.Namespace, console: Console) -> int:
    return execution_commands.command_start(
        args,
        console,
        root_resolver=_root,
        context=_context,
        current_resolver=_current,
        adapter_factory=CodexAdapter,
    )


def command_status(args: argparse.Namespace, console: Console) -> int:
    return read_commands.command_status(
        args, console, root_resolver=_root, context=_context, record_error=_record_error,
    )


def command_validate(args: argparse.Namespace, console: Console) -> int:
    return execution_commands.command_validate(
        args,
        console,
        root_resolver=_root,
        context=_context,
        current_resolver=_current,
        record_error=_record_error,
    )


def _review_output(console: Console, phase: Any, report: dict[str, Any], workflow: Any) -> None:
    execution_commands.render_review(console, phase, report, workflow)


def command_review(args: argparse.Namespace, console: Console) -> int:
    return execution_commands.command_review(
        args,
        console,
        root_resolver=_root,
        context=_context,
        current_resolver=_current,
        reviewer=run_review,
        human_approver=human_approve,
    )


def command_retry(args: argparse.Namespace, console: Console) -> int:
    return execution_commands.command_retry(
        args,
        console,
        root_resolver=_root,
        context=_context,
        review_command=command_review,
        start_command=command_start,
        plan_command=command_plan,
    )


def command_history(args: argparse.Namespace, console: Console) -> int:
    return read_commands.command_history(args, console, root_resolver=_root, context=_context)


def _doctor(root: Path | None, reviewer: bool) -> list[dict[str, Any]]:
    return read_commands.doctor_checks(root, reviewer, context=_context, current_resolver=_current)


def command_doctor(args: argparse.Namespace, console: Console) -> int:
    return read_commands.command_doctor(
        args, console, root_resolver=_root, checks_provider=_doctor,
    )


def command_error(args: argparse.Namespace, console: Console) -> int:
    return read_commands.command_error(args, console, root_resolver=_root)


def command_repair(args: argparse.Namespace, console: Console) -> int:
    return lifecycle_commands.command_repair(
        args, console, root_resolver=_root, context=_context,
    )


def command_config(args: argparse.Namespace, console: Console) -> int:
    return read_commands.command_config(args, console, root_resolver=_root)


def command_version(args: argparse.Namespace, console: Console) -> int:
    return read_commands.command_version(args, console)


COMMANDS = {
    "init": command_init, "plan": command_plan, "start": command_start, "status": command_status,
    "validate": command_validate, "review": command_review, "retry": command_retry,
    "history": command_history, "doctor": command_doctor, "error": command_error,
    "repair": command_repair, "config": command_config, "version": command_version,
}


def _record_error(exc: CwError, *, source: str | None = None, traceback_text: str | None = None) -> None:
    try:
        root = repository_root(Path.cwd())
        record_diagnostic(root, exc, source=source, traceback_text=traceback_text)
    except Exception:
        pass


def main(argv: Sequence[str] | None = None) -> int:
    values = list(argv if argv is not None else sys.argv[1:])
    if not values:
        values = ["start"]
    elif values in (["-h"], ["--help"]):
        values = ["help"]
    args = parser().parse_args(values)
    command = args.command or "help"
    console = Console(no_color=args.no_color, quiet=args.quiet)
    if command == "help":
        if args.json:
            emit_json({"commands": list(COMMANDS) + ["help"]})
        elif not args.quiet:
            print(HELP, end="")
        return 0
    try:
        return COMMANDS[command](args, console)
    except CwError as exc:
        _record_error(exc, source=command)
        if getattr(args, "hook", False):
            reason = f"{exc.message}. {exc.hint or 'Run: cw error'}"
            print(json.dumps({"continue": False, "stopReason": reason, "systemMessage": reason}))
            return 0
        if args.json:
            emit_json({"error": {"code": exc.code.value, "message": exc.message, "hint": exc.hint, "details": exc.details}})
        elif not args.quiet:
            title, detail = error_summary(exc.code.value, exc.message)
            console.item("✕", title)
            console.wrapped(detail)
            if exc.details and (args.verbose or exc.code is ErrorCode.WORKFLOW_PROJECT_MISMATCH):
                console.line()
                for line in exc.details.splitlines():
                    console.wrapped(line)
            if exc.hint:
                console.run(exc.hint.removeprefix("Run: "))
        return exc.exit_code
    except Exception as exc:
        internal = CwError(
            "Unexpected internal failure", ErrorCode.INTERNAL_ERROR,
            "Run: cw error", details=f"{type(exc).__name__}: {exc}",
        )
        _record_error(internal, source=command, traceback_text=traceback.format_exc())
        if args.json:
            emit_json({"error": {"code": internal.code.value, "message": internal.message, "hint": internal.hint}})
        elif not args.quiet:
            title, detail = error_summary(internal.code.value, internal.message)
            console.item("✕", title)
            console.wrapped(detail)
            if args.verbose:
                console.line()
                console.wrapped(internal.details or "")
            console.run("cw error")
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
