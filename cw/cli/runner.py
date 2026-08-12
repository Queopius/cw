from __future__ import annotations

import argparse
import json
import traceback
from collections.abc import Callable, Mapping

from cw.core.errors import CwError, ErrorCode
from cw.ui.console import Console, HELP, emit_json, error_summary


Command = Callable[[argparse.Namespace, Console], int]
ErrorRecorder = Callable[..., None]


def _render_cw_error(
    args: argparse.Namespace,
    console: Console,
    error: CwError,
    *,
    command: str,
    record_error: ErrorRecorder,
) -> int:
    record_error(error, source=command)
    if getattr(args, "hook", False):
        reason = f"{error.message}. {error.hint or 'Run: cw error'}"
        print(json.dumps({"continue": False, "stopReason": reason, "systemMessage": reason}))
        return 0
    if args.json:
        emit_json({
            "error": {
                "code": error.code.value,
                "message": error.message,
                "hint": error.hint,
                "details": error.details,
            },
        })
    elif not args.quiet:
        title, detail = error_summary(error.code.value, error.message)
        console.item("✕", title)
        console.wrapped(detail)
        if error.details and (args.verbose or error.code is ErrorCode.WORKFLOW_PROJECT_MISMATCH):
            console.line()
            for line in error.details.splitlines():
                console.wrapped(line)
        if error.hint:
            console.run(error.hint.removeprefix("Run: "))
    return error.exit_code


def _render_internal_error(
    args: argparse.Namespace,
    console: Console,
    error: Exception,
    *,
    command: str,
    record_error: ErrorRecorder,
) -> int:
    internal = CwError(
        "Unexpected internal failure",
        ErrorCode.INTERNAL_ERROR,
        "Run: cw error",
        details=f"{type(error).__name__}: {error}",
    )
    record_error(internal, source=command, traceback_text=traceback.format_exc())
    if args.json:
        emit_json({
            "error": {
                "code": internal.code.value,
                "message": internal.message,
                "hint": internal.hint,
            },
        })
    elif not args.quiet:
        title, detail = error_summary(internal.code.value, internal.message)
        console.item("✕", title)
        console.wrapped(detail)
        if args.verbose:
            console.line()
            console.wrapped(internal.details or "")
        console.run("cw error")
    return 1


def run(
    args: argparse.Namespace,
    *,
    commands: Mapping[str, Command],
    record_error: ErrorRecorder,
) -> int:
    command = args.command or "help"
    console = Console(no_color=args.no_color, quiet=args.quiet)
    if command == "help":
        if args.json:
            emit_json({"commands": [*commands, "help"]})
        elif not args.quiet:
            print(HELP, end="")
        return 0
    try:
        return commands[command](args, console)
    except CwError as error:
        return _render_cw_error(
            args, console, error, command=command, record_error=record_error,
        )
    except Exception as error:
        return _render_internal_error(
            args, console, error, command=command, record_error=record_error,
        )
    except KeyboardInterrupt:
        return 130
