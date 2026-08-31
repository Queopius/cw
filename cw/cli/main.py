from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from cw.adapters.codex import CodexAdapter
from cw.application.context import load_project_context
from cw.agents.reviewer import human_approve, run_review
from cw.cli.commands import config as config_commands
from cw.cli.commands import completion as completion_commands
from cw.cli.commands import execution as execution_commands
from cw.cli.commands import governance as governance_commands
from cw.cli.commands import lifecycle as lifecycle_commands
from cw.cli.commands import read as read_commands
from cw.cli.commands import update as update_commands
from cw.cli.commands import batch as batch_commands
from cw.core.session import readiness_path
from cw.cli.parser import build_parser, parse_args
from cw.cli.runner import run
from cw.core.diagnostics import record_diagnostic, record_global_diagnostic
from cw.core.errors import CwError
from cw.core.platform import interrupt_bridge
from cw.core.project import repository_root
from cw.core.state import validate_state
from cw.ui.console import Console, emit_json
from cw.output_protocol import output_schema_document


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
    from cw.core.plan_amendment import ensure_no_pending_plan_amendment

    ensure_no_pending_plan_amendment(root)
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


def command_capabilities(args: argparse.Namespace, console: Console) -> int:
    from cw import __version__
    from cw.adapters.mcp.compatibility import load_plugin_compatibility
    from cw.adapters.mcp.runtime import TOOLS
    from cw.remote.protocol import PROTOCOL_VERSION

    plugin = load_plugin_compatibility()
    payload = {
        "core": __version__,
        "plugin": plugin["plugin_version"],
        "remote_protocol": PROTOCOL_VERSION,
        "schemas": {
            "project": 1,
            "governance_evidence": 2,
            "output": "cw.output.v1",
        },
        "output": {
            "modes": ["human", "json", "jsonl", "llm"],
            "environment": "CW_OUTPUT_MODE",
            "fields": True,
            "expansion": True,
            "pagination": {"default_llm_limit": 10, "maximum_limit": 100},
        },
        "commands": sorted([*COMMANDS, "help"]),
        "plugin_compatibility": {
            "minimum_core": plugin["core"]["minimum"],
            "maximum_core_exclusive": plugin["core"]["maximum_exclusive"],
            "tool_count": len(TOOLS),
        },
    }
    if args.json:
        emit_json(payload)
    else:
        console.header("Capabilities")
        console.field("Core", payload["core"])
        console.field("Output schema", "cw.output.v1")
        console.field("Formats", ", ".join(payload["output"]["modes"]))
        console.field("Plugin", f"{payload['plugin']} · {len(TOOLS)} tools")
        console.field("Remote", payload["remote_protocol"])
    return 0


def command_schema(args: argparse.Namespace, console: Console) -> int:
    payload = {"name": args.schema_name, "schema": output_schema_document()}
    if args.json:
        emit_json(payload)
    else:
        console.header("Schema")
        console.field("Name", args.schema_name)
        console.line(json.dumps(payload["schema"], ensure_ascii=False, sort_keys=True, indent=2))
    return 0


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


def command_governance(args: argparse.Namespace, console: Console) -> int:
    return governance_commands.command_governance(args, console, root_resolver=_root)


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


def command_mcp(args: argparse.Namespace, console: Console) -> int:
    # Lazy import preserves ordinary CLI operation without the optional MCP SDK.
    from cw.adapters.mcp import (
        ChatGPTSurface,
        RuntimeConfig,
        chatgpt_development_config,
    )
    from cw.adapters.mcp.server import serve

    if args.action == "chatgpt-dev" and not args.projects:
        print(
            "cw mcp chatgpt-dev requires at least one explicit --project grant",
            file=sys.stderr,
            flush=True,
        )
        return 2
    projects = [Path(item) for item in (args.projects or [Path.cwd()])]
    allowed_roots = [Path(item) for item in args.allowed_roots] if args.allowed_roots else projects
    if args.action == "chatgpt-dev":
        config = chatgpt_development_config(
            projects,
            allowed_roots,
            surface=ChatGPTSurface(args.surface),
        )
    else:
        config = RuntimeConfig.create(projects, allowed_roots)
    return serve(config)


def command_remote(args: argparse.Namespace, console: Console) -> int:
    """Bootstrap the optional remote adapter without moving policy into CLI."""

    from cw.core.platform import global_config_dir
    from cw.remote.agent import (
        HTTPAgentClient,
        LocalAgentRuntime,
        LocalAgentState,
        register_project_grant,
        request_pairing,
    )
    from cw.remote.auth import OAuthResourceConfig, OAuthTokenVerifier
    from cw.remote.device import DeviceCredential
    from cw.remote.errors import RemoteError
    from cw.remote.gateway import GatewayService
    from cw.remote.persistence import RemoteStore

    directory = global_config_dir() / "remote"
    credential_path = Path(args.credentials) if args.credentials else directory / "device.json"
    state_path = Path(args.state) if args.state else directory / "projects.json"

    def required(value: str | None, option: str) -> str:
        if not value:
            raise ValueError(f"cw remote {args.action} requires {option}")
        return value

    try:
        if args.action == "gateway":
            from cw.remote.server import create_gateway_app, serve_gateway

            config = OAuthResourceConfig(
                issuer=required(args.issuer_url, "--issuer-url"),
                resource=required(args.resource_url, "--resource-url"),
                jwks_uri=required(args.jwks_url, "--jwks-url"),
                documentation_url="https://docs.cwcli.dev/en/stable/remote-auth/",
            )
            database = Path(required(args.database, "--database"))
            store = RemoteStore(database)
            try:
                verifier = OAuthTokenVerifier(config, store)
                return serve_gateway(
                    create_gateway_app(GatewayService(store, verifier), config),
                    host=args.host,
                    port=args.port,
                )
            finally:
                store.close()

        gateway = required(args.gateway_url, "--gateway-url")
        if args.action == "pair":
            if credential_path.exists():
                credential = DeviceCredential.load(credential_path)
            else:
                credential = DeviceCredential.generate()
                credential.save(credential_path)
            payload = asyncio.run(request_pairing(
                gateway_url=gateway,
                credential=credential,
                display_name=args.device_name,
            ))
            if args.json:
                print(json.dumps(payload, sort_keys=True))
            elif not args.quiet:
                pair_url = gateway.rstrip("/") + "/remote/pair"
                console.item("✓", "Pairing requested")
                console.wrapped(f"Device: {payload['device_id']}")
                console.wrapped(f"Open: {pair_url}")
                console.wrapped(f"Enter code: {payload['user_code']}")
                console.wrapped(f"Expires: {payload['expires_at']}")
                console.wrapped("Approve or reject this exact device after signing in.")
            return 0

        credential = DeviceCredential.load(credential_path)
        state = LocalAgentState.load(state_path)
        if args.action == "grant":
            if not args.projects or len(args.projects) != 1:
                raise ValueError("cw remote grant requires exactly one --project")
            project = Path(args.projects[0]).resolve(strict=True)
            allowed = [Path(item).resolve(strict=True) for item in (args.allowed_roots or [project])]
            # Opening the runtime proves initialized state and canonical root
            # containment before any grant metadata crosses the network.
            probe = LocalAgentRuntime(
                project_paths=[project], allowed_roots=allowed,
                grant_handles={project: "cwp_" + "A" * 24},
            )
            probe.shutdown()
            payload = asyncio.run(register_project_grant(
                gateway_url=gateway, credential=credential, project=project,
            ))
            grants = dict(state.grants)
            grants[payload["project_handle"]] = {
                "project_path": str(project),
                "principal_id": payload["principal_id"],
                "workspace_id": payload["workspace_id"],
                "device_id": payload["device_id"],
                "display_name": payload["display_name"],
            }
            LocalAgentState(grants).save(state_path)
            if args.json:
                print(json.dumps({
                    "project_handle": payload["project_handle"],
                    "display_name": payload["display_name"],
                }, sort_keys=True))
            elif not args.quiet:
                console.item("✓", "Project grant created")
                console.wrapped(f"Handle: {payload['project_handle']}")
                console.wrapped(f"Project: {payload['display_name']}")
            return 0

        if not state.grants:
            raise ValueError("cw remote agent requires at least one locally authorized project grant")
        project_paths = [Path(record["project_path"]) for record in state.grants.values()]
        allowed = [Path(item) for item in args.allowed_roots] if args.allowed_roots else project_paths
        runtime = LocalAgentRuntime(
            project_paths=project_paths,
            allowed_roots=allowed,
            grant_handles={Path(record["project_path"]): handle for handle, record in state.grants.items()},
            grant_identities={
                handle: (record["principal_id"], record["workspace_id"], record["device_id"])
                for handle, record in state.grants.items()
            },
        )
        async def run_agent() -> None:
            stop = asyncio.Event()
            await HTTPAgentClient(
                gateway_url=gateway, credential=credential, runtime=runtime,
            ).run(stop)
        try:
            asyncio.run(run_agent())
            return 0
        finally:
            runtime.shutdown(wait=True)
    except (RemoteError, ValueError, OSError, json.JSONDecodeError) as exc:
        if args.json:
            print(json.dumps({"error": {"code": getattr(getattr(exc, "code", None), "value", "INVALID_REQUEST"), "message": str(exc)}}))
        else:
            print(str(exc), file=sys.stderr, flush=True)
        return 2


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
    "repair": command_repair, "config": command_config, "governance": command_governance,
    "version": command_version,
    "update": command_update, "changelog": command_changelog,
    "integrations": command_integrations,
    "explain": command_explain,
    "run": command_run,
    "inspect": command_inspect,
    "logs": command_logs,
    "mcp": command_mcp,
    "remote": command_remote,
    "capabilities": command_capabilities,
    "schema": command_schema,
}


def _record_error(
    exc: CwError,
    *,
    source: str | None = None,
    traceback_text: str | None = None,
    correlation_id: str | None = None,
    safe_traceback: dict[str, Any] | None = None,
) -> None:
    if source == "update":
        try:
            record_global_diagnostic(
                exc, source=source, traceback_text=traceback_text, correlation_id=correlation_id, safe_traceback=safe_traceback,
            )
        except Exception:
            pass
        return
    try:
        root = repository_root(Path.cwd())
        record = record_diagnostic(
            root, exc, source=source, traceback_text=traceback_text, correlation_id=correlation_id, safe_traceback=safe_traceback,
        )
        if record is None:
            record_global_diagnostic(
                exc, source=source, traceback_text=traceback_text, correlation_id=correlation_id,
                safe_traceback=safe_traceback,
            )
    except Exception:
        try:
            record_global_diagnostic(
                exc, source=source, traceback_text=traceback_text, correlation_id=correlation_id,
                safe_traceback=safe_traceback,
            )
        except Exception:
            pass


def main(argv: Sequence[str] | None = None) -> int:
    with interrupt_bridge():
        return run(parse_args(argv), commands=COMMANDS, record_error=_record_error)


if __name__ == "__main__":
    raise SystemExit(main())
