from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Sequence

from cw.adapters.codex import CodexAdapter
from cw.application.context import load_project_context
from cw.agents.reviewer import human_approve, run_review
from cw.cli.commands import config as config_commands
from cw.cli.commands import completion as completion_commands
from cw.cli.commands import execution as execution_commands
from cw.cli.commands import lifecycle as lifecycle_commands
from cw.cli.commands import read as read_commands
from cw.cli.commands import update as update_commands
from cw.cli.commands import batch as batch_commands
from cw.core.session import readiness_path
from cw.cli.parser import build_parser, parse_args
from cw.cli.runner import run
from cw.core.config import apply_policy, load_policy
from cw.core.diagnostics import record_diagnostic, record_global_diagnostic
from cw.core.errors import CwError, ErrorCode
from cw.core.layout import validate_project_layout
from cw.core.platform import interrupt_bridge
from cw.core.project import load_project, repository_root
from cw.core.state import load_state, validate_state
from cw.core.workflow import load_workflow
from cw.ui.console import Console


def parser() -> argparse.ArgumentParser:
    return build_parser()


def _root() -> Path:
    return repository_root(Path.cwd())


def _context(root: Path) -> tuple[Any, dict[str, Any], Any]:
    project, state, workflow = _raw_context(root)
    if workflow.phases:
        validate_state(root, state, workflow)
    return project, state, workflow


def _raw_context(root: Path) -> tuple[Any, dict[str, Any], Any]:
    return load_project_context(root, validate=False)


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


def command_completion(args: argparse.Namespace, console: Console) -> int:
    return completion_commands.command_completion(
        args, console, root_resolver=_root, context=_raw_context,
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
        args, console, root_resolver=_root, context=_raw_context, record_error=_record_error,
    )


def command_explain(args: argparse.Namespace, console: Console) -> int:
    return read_commands.command_explain(
        args, console, root_resolver=_root, context=_raw_context,
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
    from cw.core.completion import run_completion_review

    return execution_commands.command_review(
        args,
        console,
        root_resolver=_root,
        context=_context,
        current_resolver=_current,
        reviewer=run_review,
        human_approver=human_approve,
        completion_reviewer=lambda root, workflow, state: run_completion_review(
            root, workflow, state, CodexAdapter(),
        ),
    )


def command_retry(args: argparse.Namespace, console: Console) -> int:
    return execution_commands.command_retry(
        args,
        console,
        root_resolver=_root,
        context=_context,
        current_resolver=_current,
        review_command=command_review,
        start_command=command_start,
        plan_command=command_plan,
        completion_command=command_completion,
    )


def command_history(args: argparse.Namespace, console: Console) -> int:
    return read_commands.command_history(args, console, root_resolver=_root, context=_context)


def _doctor(
    root: Path | None, reviewer: bool, integrations: bool = False, codex: bool = False,
) -> list[dict[str, Any]]:
    return read_commands.doctor_checks(
        root, reviewer, integrations, codex, context=_raw_context, current_resolver=_current,
    )


def command_doctor(args: argparse.Namespace, console: Console) -> int:
    return read_commands.command_doctor(
        args, console, root_resolver=_root, checks_provider=_doctor,
    )


def command_error(args: argparse.Namespace, console: Console) -> int:
    return read_commands.command_error(args, console, root_resolver=_root)


def command_repair(args: argparse.Namespace, console: Console) -> int:
    return lifecycle_commands.command_repair(
        args, console, root_resolver=_root, context=_raw_context,
    )


def command_config(args: argparse.Namespace, console: Console) -> int:
    return config_commands.command_config(args, console, root_resolver=_root)


def command_version(args: argparse.Namespace, console: Console) -> int:
    return read_commands.command_version(args, console)


def command_update(args: argparse.Namespace, console: Console) -> int:
    return update_commands.command_update(args, console)


def command_changelog(args: argparse.Namespace, console: Console) -> int:
    return update_commands.command_changelog(args, console)


def command_integrations(args: argparse.Namespace, console: Console) -> int:
    return read_commands.command_integrations(args, console, root_resolver=_root, context=_context)


def command_inspect(args: argparse.Namespace, console: Console) -> int:
    return read_commands.command_inspect(args, console, root_resolver=_root)


def command_logs(args: argparse.Namespace, console: Console) -> int:
    return read_commands.command_logs(args, console, root_resolver=_root)


def command_run(args: argparse.Namespace, console: Console) -> int:
    def execute_phase(phase_id: str, remaining_seconds: float) -> int:
        root = _root()
        _, state, _ = _context(root)
        phase_args = argparse.Namespace(**vars(args))
        phase_args.json = False
        phase_args.hook = False
        phase_args.human_approve = False
        phase_args._batch_mode = True
        phase_args._batch_agent_timeout = max(1, int(remaining_seconds))
        if readiness_path(root).exists() and state.get("current_phase") == phase_id:
            return command_review(phase_args, console)
        code = command_start(phase_args, console)
        _, after, _ = _context(root)
        if (
            code == 0 and readiness_path(root).exists()
            and after.get("current_phase") == phase_id
        ):
            return command_review(phase_args, console)
        return code

    return batch_commands.command_run(
        args, console, root_resolver=_root, context=_context, executor=execute_phase,
    )


COMMANDS = {
    "init": command_init, "plan": command_plan, "completion": command_completion,
    "start": command_start, "status": command_status,
    "validate": command_validate, "review": command_review, "retry": command_retry,
    "history": command_history, "doctor": command_doctor, "error": command_error,
    "repair": command_repair, "config": command_config, "version": command_version,
    "update": command_update, "changelog": command_changelog,
    "integrations": command_integrations,
    "explain": command_explain,
    "run": command_run,
    "inspect": command_inspect,
    "logs": command_logs,
}


def _record_error(exc: CwError, *, source: str | None = None, traceback_text: str | None = None) -> None:
    if source == "update":
        try:
            record_global_diagnostic(exc, source=source, traceback_text=traceback_text)
        except Exception:
            pass
        return
    try:
        root = repository_root(Path.cwd())
        record = record_diagnostic(root, exc, source=source, traceback_text=traceback_text)
        if record is None:
            record_global_diagnostic(exc, source=source, traceback_text=traceback_text)
    except Exception:
        try:
            record_global_diagnostic(exc, source=source, traceback_text=traceback_text)
        except Exception:
            pass


def main(argv: Sequence[str] | None = None) -> int:
    with interrupt_bridge():
        return run(parse_args(argv), commands=COMMANDS, record_error=_record_error)


if __name__ == "__main__":
    raise SystemExit(main())
