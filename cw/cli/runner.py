from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import re
import sys
import traceback
from collections.abc import Callable, Mapping
from pathlib import Path

from cw.core.errors import CwError, ErrorCode
from cw.output_protocol import (
    OutputMode,
    OutputStatus,
    changed_for,
    command_name,
    envelope,
    parse_records,
    prepare_data,
    resolve_output_mode,
    result_status,
    sanitize_output,
    validate_machine_options,
)
from cw.ui.console import Console, emit_json, error_summary
from cw.ui.renderers import render_help, render_update_notice
from cw.update.service import automatic_update_notice

Command = Callable[[argparse.Namespace, Console], int]
ErrorRecorder = Callable[..., None]


_RETRYABLE_ERRORS = frozenset({
    ErrorCode.VERIFICATION_INFRASTRUCTURE_ERROR,
    ErrorCode.VERIFICATION_TIMEOUT,
    ErrorCode.REVIEWER_INFRASTRUCTURE_ERROR,
    ErrorCode.REVIEWER_INVALID_OUTPUT,
    ErrorCode.REVIEWER_NETWORK_ERROR,
    ErrorCode.REVIEWER_PROCESS_ERROR,
    ErrorCode.PLANNER_NETWORK_ERROR,
    ErrorCode.PLANNER_TRANSPORT_ERROR,
    ErrorCode.PLANNER_PROCESS_ERROR,
    ErrorCode.LOCKED,
    ErrorCode.UPDATE_CHECK_ERROR,
    ErrorCode.UPDATE_DOWNLOAD_ERROR,
})


def _correlation_id(command: str, code: str, message: str) -> str:
    return hashlib.sha256(f"{command}\0{code}\0{message}".encode()).hexdigest()[:16]


def _compact_error_text(value: str | None, *, maximum: int = 240) -> str | None:
    if value is None:
        return None
    without_markup = re.sub(r"<[^>]{1,200}>", " ", value)
    compact = " ".join(without_markup.split())
    return compact if len(compact) <= maximum else compact[: maximum - 1].rstrip() + "…"


def _structured_error(command: str, error: CwError) -> dict[str, object]:
    return {
        "code": error.code.value,
        "message": _compact_error_text(error.message) or error.code.value,
        "retryable": error.code in _RETRYABLE_ERRORS,
        "hint": _compact_error_text(error.hint.removeprefix("Run: ")) if error.hint else None,
        "correlation_id": _correlation_id(command, error.code.value, error.message),
    }


def _structured_gate(error: CwError) -> dict[str, object] | None:
    if not error.details:
        return None
    try:
        source = json.loads(error.details)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(source, dict):
        return None
    allowed = {
        "repository", "pr", "head_branch", "head_sha", "base_branch", "base_sha",
        "evidence_schema", "generation", "authorization_state", "final_state", "next_safe_action",
    }
    gate = {key: source[key] for key in allowed if key in source}
    return gate or None


def _emit_protocol(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")), flush=True)


def _debug(error: CwError, *, roots: tuple[Path, ...] = ()) -> None:
    detail = sanitize_output(error.details or error.message, private_roots=roots)
    print(f"{error.code.value}: {detail}", file=sys.stderr, flush=True)


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
        warning = error.code in {
            ErrorCode.PLAN_UNCLEAR, ErrorCode.PLAN_REQUIRED,
            ErrorCode.NOTHING_TO_VALIDATE, ErrorCode.EXECUTION_INTERRUPTED,
        } or error.exit_code == 3
        console.item("!" if warning else "✕", title)
        console.wrapped(detail)
        if error.details and (
            args.verbose
            or error.code in {ErrorCode.WORKFLOW_PROJECT_MISMATCH, ErrorCode.BATCH_TOO_LARGE}
        ):
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
    identifier = command_name(args)
    try:
        mode = resolve_output_mode(args)
        validate_machine_options(args, identifier, mode)
    except CwError as error:
        if getattr(args, "hook", False):
            reason = f"{error.message}. {error.hint or 'Run: cw error'}"
            print(json.dumps({"continue": False, "stopReason": reason, "systemMessage": reason}))
            return 0
        requested_machine = bool(
            getattr(args, "json", False) or getattr(args, "llm", False)
            or getattr(args, "output", None) in {"json", "jsonl"}
            or os.environ.get("CW_OUTPUT_MODE") in {"json", "jsonl", "llm"}
        )
        if requested_machine:
            _emit_protocol(envelope(
                identifier, status=OutputStatus.ERROR, changed=False,
                error=sanitize_output(
                    _structured_error(identifier, error), private_roots=(Path.cwd(), Path.home()),
                ), gate=_structured_gate(error),
            ))
        else:
            console = Console(no_color=getattr(args, "no_color", False), quiet=getattr(args, "quiet", False))
            title, detail = error_summary(error.code.value, error.message)
            console.item("✕", title)
            console.wrapped(detail)
        if getattr(args, "debug", False):
            _debug(error)
        return error.exit_code

    if mode is not OutputMode.HUMAN:
        return _run_machine(
            args, identifier=identifier, mode=mode, commands=commands, record_error=record_error,
        )

    console = Console(no_color=args.no_color, quiet=args.quiet)
    if command == "help":
        if args.json:
            emit_json({"commands": [*commands, "help"]})
        elif not args.quiet:
            render_help(console)
        return 0
    try:
        result = commands[command](args, console)
        if result == 0 and command == "status" and not args.json and not args.quiet:
            notice = automatic_update_notice()
            if notice is not None:
                render_update_notice(console, {
                    "latest": str(notice.latest), "installed": str(notice.installed),
                    "level": notice.level,
                })
        return result
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


def _run_machine(
    args: argparse.Namespace,
    *,
    identifier: str,
    mode: OutputMode,
    commands: Mapping[str, Command],
    record_error: ErrorRecorder,
) -> int:
    command = args.command or "help"
    args.json = True
    args.no_color = True
    args.quiet = False
    captured = io.StringIO()
    diagnostics = io.StringIO()
    console = Console(stream=captured, no_color=True, quiet=False)
    try:
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(diagnostics):
            if command == "help":
                result = 0
                emit_json({"commands": [*commands, "help"]})
            else:
                result = commands[command](args, console)
        records = parse_records(captured.getvalue())
        data: object
        if not records:
            data = {}
        elif len(records) == 1:
            data = records[0]
        else:
            data = {"events": records}
        data = sanitize_output(data, private_roots=(Path.cwd(), Path.home()))
        operation_id = data.get("operation_id") if isinstance(data, dict) and isinstance(data.get("operation_id"), str) else None
        data, page, truncation_reason = prepare_data(identifier, data, args, mode)
        status = result_status(identifier, result, data)
        if mode is OutputMode.JSONL and len(records) > 1:
            for index, record in enumerate(records):
                line_status = status if index == len(records) - 1 else OutputStatus.PARTIAL
                _emit_protocol(envelope(
                    identifier,
                    status=line_status,
                    changed=changed_for(identifier, line_status, record),
                    data=sanitize_output(record, private_roots=(Path.cwd(), Path.home())),
                    operation_id=operation_id,
                    truncation_reason=truncation_reason,
                ))
        else:
            _emit_protocol(envelope(
                identifier,
                status=status,
                changed=changed_for(identifier, status, data),
                data=data,
                operation_id=operation_id,
                page=page,
                truncation_reason=truncation_reason,
            ))
        if getattr(args, "debug", False) and diagnostics.getvalue().strip():
            print(
                sanitize_output(diagnostics.getvalue(), private_roots=(Path.cwd(), Path.home())),
                file=sys.stderr,
                flush=True,
            )
        return result
    except CwError as error:
        record_error(error, source=command)
        _emit_protocol(envelope(
            identifier,
            status=(
                OutputStatus.AUTHORIZATION_REQUIRED
                if error.code is ErrorCode.AUTHORIZATION_REQUIRED
                else OutputStatus.BLOCKED if error.exit_code == 3
                else OutputStatus.ERROR
            ),
            changed=False,
            error=sanitize_output(
                _structured_error(identifier, error), private_roots=(Path.cwd(), Path.home()),
            ),
            gate=sanitize_output(
                _structured_gate(error), private_roots=(Path.cwd(), Path.home()),
            ) if _structured_gate(error) else None,
        ))
        if getattr(args, "debug", False):
            _debug(error, roots=(Path.cwd(), Path.home()))
        return error.exit_code
    except KeyboardInterrupt:
        _emit_protocol(envelope(
            identifier,
            status=OutputStatus.CANCELLED,
            changed=False,
            error={
                "code": "CANCELLED", "message": "Operation cancelled", "retryable": False,
                "hint": None, "correlation_id": _correlation_id(identifier, "CANCELLED", "Operation cancelled"),
            },
        ))
        return 130
    except Exception as error:
        internal = CwError(
            "Unexpected internal failure", ErrorCode.INTERNAL_ERROR, "Run: cw error",
            details=f"{type(error).__name__}: {error}",
        )
        record_error(internal, source=command, traceback_text=traceback.format_exc())
        _emit_protocol(envelope(
            identifier,
            status=OutputStatus.ERROR,
            changed=False,
            error=_structured_error(identifier, internal),
        ))
        if getattr(args, "debug", False):
            _debug(internal, roots=(Path.cwd(), Path.home()))
        return 1
