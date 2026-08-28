from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from cw.core.diagnostics import redact
from cw.core.errors import CwError, ErrorCode
from cw.update.config import load_global_document, write_global_document

OUTPUT_SCHEMA = "cw.output.v1"
LLM_DEFAULT_LIMIT = 10
MAX_LIMIT = 100
_SECRET_KEY = re.compile(
    r"(?i)(?:^|_)(?:authorization_header|credential|password|private_key|secret|access_token|refresh_token|id_token)(?:$|_)"
)
_HOME_PATH = re.compile(r"/(?:home|Users)/[^/\s\"']+")
_WINDOWS_HOME = re.compile(r"(?i)(?:\b[A-Z]:)?[\\/]+(?:Users|Documents and Settings)[\\/]+[^\\/\r\n]+")


class OutputMode(str, Enum):
    HUMAN = "human"
    JSON = "json"
    JSONL = "jsonl"
    LLM = "llm"


class OutputStatus(str, Enum):
    SUCCESS = "success"
    NOOP = "noop"
    ERROR = "error"
    AUTHORIZATION_REQUIRED = "authorization_required"
    BLOCKED = "blocked"
    PARTIAL = "partial"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class OutputSettings:
    mode: OutputMode = OutputMode.HUMAN


_FIELD_ALLOWLISTS: dict[str, frozenset[str]] = {
    "status": frozenset({
        "schema_version", "project", "branch", "workflow", "plan", "state", "phase", "position",
        "phase_count", "approved_count", "remaining_count", "active_count", "effective_state", "is_complete",
        "planned_scope_complete", "completion_mode", "completion_satisfied", "attempt", "revision_attempt",
        "validation_attempt", "active_plan_revision", "active_plan_revision_sha256", "superseded_plan_revisions",
        "superseded_reviews", "active_review", "rebaseline", "ready", "gate", "gate_states", "invalid_gates",
        "last_error", "infrastructure_error", "batch", "run", "consistent", "consistency_issues",
        "expected_phase", "approved_through", "phases",
    }),
    "doctor": frozenset({"checks", "result", "performance", "run", "process_alive"}),
    "doctor.reviewer": frozenset({"checks", "result", "performance", "run", "process_alive"}),
    "history": frozenset({"workflow", "phases", "events", "completion_cycles"}),
    "explain": frozenset({
        "consistent", "current_phase", "expected_phase", "approved_through", "issues", "recovery",
        "planned_scope_complete", "completion_mode", "completion_target", "completion_satisfied",
        "completion_review", "extension_proposal", "active_plan_revision", "superseded_plan_revisions",
        "superseded_reviews", "rebaseline", "rebaseline_explanation", "classification",
        "failed_operation", "retryable", "readiness_available", "semantic_attempt",
        "revision_attempt", "reason",
    }),
    "plan.show": frozenset({"workflow", "state", "revision", "proposal", "completion_contract", "phases"}),
    "completion.show": frozenset({
        "schema_version", "workflow", "mode", "contract", "planned_scope_complete", "completion_satisfied",
        "review", "proposal", "state", "cycle",
    }),
    "inspect": frozenset({"run", "events", "schema_version", "workflow", "mode", "contract", "review", "proposal", "state", "cycle"}),
    "logs": frozenset({"run_id", "events"}),
    "capabilities": frozenset({
        "core", "plugin", "remote_protocol", "schemas", "output", "commands", "plugin_compatibility",
    }),
    "schema.show": frozenset({"name", "schema"}),
    "plan.rebaseline.recover": frozenset({
        "changed", "idempotent_replay", "recovery_id", "operation_id", "phase",
        "review_reference", "review_sha256", "workflow_sha256", "state_sha256",
        "previous_status", "resulting_status", "backup", "recovery_receipt", "next_action",
    }),
    "review.recover-infrastructure": frozenset({
        "result", "changed", "mutation", "idempotent_replay", "retryable", "classification",
        "operation_id", "recovery_id", "phase", "review_reference", "review_sha256",
        "workflow_sha256", "state_sha256", "attempts_restored", "readiness_available",
        "backup", "recovery_receipt", "next_action",
    }),
    "review.authorize-retry": frozenset({
        "result", "changed", "idempotent_replay", "classification",
        "authorization_id", "authorization_status", "verification_required",
        "next_action",
    }),
    "retry": frozenset({
        "result", "decision", "changed", "mutation", "retryable", "classification",
        "retry_operation", "phase", "attempt", "revision_attempt", "review_reference",
        "validation_evidence", "readiness_available", "gate", "next_phase",
        "idempotent_replay", "next_action",
    }),
}

_INVARIANT_KEYS = frozenset({
    "repository", "project", "pr", "head_branch", "base_branch", "head_sha", "base_sha",
    "workflow_sha256", "state_sha256", "evidence_schema", "generation", "authorization_state",
    "final_state", "next_safe_action", "operation_id",
})

_PAGINATED_KEYS = {
    "history": "phases",
    "plan.show": "phases",
    "logs": "events",
    "inspect": "events",
    "changelog": "releases",
}

_LLM_FIELDS = {
    "status": (
        "project", "branch", "plan", "state", "phase", "approved_count", "remaining_count",
        "planned_scope_complete", "completion_satisfied", "ready", "gate", "consistent",
    ),
    "doctor": ("result", "checks"),
    "history": ("workflow", "phases", "completion_cycles"),
    "explain": (
        "consistent", "current_phase", "expected_phase", "approved_through", "issues", "recovery",
        "planned_scope_complete", "completion_satisfied", "active_plan_revision", "rebaseline",
        "classification", "failed_operation", "retryable", "readiness_available",
        "semantic_attempt", "revision_attempt", "reason",
    ),
    "review.recover-infrastructure": (
        "result", "changed", "mutation", "retryable", "classification", "phase",
        "review_reference", "review_sha256", "workflow_sha256", "state_sha256",
        "attempts_restored", "readiness_available", "backup", "recovery_receipt",
        "idempotent_replay", "next_action",
    ),
    "review.authorize-retry": (
        "result", "changed", "idempotent_replay", "classification",
        "authorization_id", "authorization_status", "verification_required",
        "next_action",
    ),
    "retry": (
        "result", "decision", "changed", "mutation", "retryable", "classification",
        "retry_operation", "phase", "attempt", "revision_attempt", "readiness_available",
        "gate", "idempotent_replay", "next_action",
    ),
}

_READ_COMMANDS = frozenset({
    "status", "doctor", "history", "explain", "plan.show", "completion.show", "inspect", "logs",
    "capabilities", "schema.show", "version", "changelog", "config", "integrations", "error",
})


def load_output_settings() -> OutputSettings:
    section = load_global_document().get("output", {})
    if not isinstance(section, dict):
        raise CwError("[output] configuration must be a table", ErrorCode.USAGE_ERROR, exit_code=2)
    unknown = set(section) - {"mode"}
    if unknown:
        raise CwError("Unknown output setting", ErrorCode.USAGE_ERROR, details=", ".join(sorted(unknown)), exit_code=2)
    raw = section.get("mode", "human")
    try:
        return OutputSettings(OutputMode(str(raw).lower()))
    except ValueError as exc:
        raise CwError(
            "Output mode must be human, json, jsonl, or llm", ErrorCode.USAGE_ERROR, exit_code=2,
        ) from exc


def set_output_setting(key: str, raw: str) -> tuple[str, OutputSettings]:
    if key != "output.mode":
        raise CwError(f"Unknown output setting: {key}", ErrorCode.USAGE_ERROR, exit_code=2)
    try:
        value = OutputMode(raw.lower()).value
    except ValueError as exc:
        raise CwError(
            "output.mode must be human, json, jsonl, or llm", ErrorCode.USAGE_ERROR, exit_code=2,
        ) from exc
    document = load_global_document()
    section = document.setdefault("output", {})
    if not isinstance(section, dict):
        raise CwError("[output] configuration must be a table", ErrorCode.USAGE_ERROR, exit_code=2)
    section["mode"] = value
    write_global_document(document)
    return value, load_output_settings()


def resolve_output_mode(args: argparse.Namespace) -> OutputMode:
    selected = getattr(args, "output", None)
    llm = bool(getattr(args, "llm", False))
    legacy_json = bool(getattr(args, "json", False))
    if llm and selected is not None:
        raise CwError("--llm cannot be combined with --output", ErrorCode.USAGE_ERROR, exit_code=2)
    if llm and legacy_json:
        raise CwError("--llm cannot be combined with --json", ErrorCode.USAGE_ERROR, exit_code=2)
    if legacy_json and selected is not None:
        raise CwError("--json cannot be combined with --output", ErrorCode.USAGE_ERROR, exit_code=2)
    if llm:
        return OutputMode.LLM
    if selected is not None:
        return OutputMode(selected)
    if legacy_json:
        # Compatibility surface: historical --json keeps its pre-0.16 payload.
        # The versioned envelope is selected explicitly with --output=json or --llm.
        return OutputMode.HUMAN
    environment = os.environ.get("CW_OUTPUT_MODE")
    if environment:
        try:
            return OutputMode(environment.lower())
        except ValueError as exc:
            raise CwError(
                "CW_OUTPUT_MODE must be human, json, jsonl, or llm", ErrorCode.USAGE_ERROR, exit_code=2,
            ) from exc
    return load_output_settings().mode


def command_name(args: argparse.Namespace) -> str:
    command = str(getattr(args, "command", None) or "help")
    action = getattr(args, "action", None)
    if command == "plan" and action == "rebaseline" and getattr(args, "rebaseline_action", None):
        return f"plan.rebaseline.{args.rebaseline_action}"
    if command in {"plan", "completion", "governance", "schema", "review"} and action:
        return f"{command}.{action}"
    if command == "doctor" and getattr(args, "reviewer", False):
        return "doctor.reviewer"
    return command


def validate_machine_options(args: argparse.Namespace, command: str, mode: OutputMode) -> None:
    fields = parse_fields(getattr(args, "fields", None))
    if fields and mode is OutputMode.HUMAN:
        raise CwError("--fields requires --output=json, --output=jsonl, or --llm", ErrorCode.USAGE_ERROR, exit_code=2)
    if getattr(args, "expand", False) and mode is OutputMode.HUMAN:
        raise CwError("--expand requires --output=json, --output=jsonl, or --llm", ErrorCode.USAGE_ERROR, exit_code=2)
    if fields and command not in _FIELD_ALLOWLISTS:
        raise CwError(f"--fields is not supported for {command}", ErrorCode.USAGE_ERROR, exit_code=2)
    if fields:
        allowed = _FIELD_ALLOWLISTS[command]
        unknown = sorted({field.split(".", 1)[0] for field in fields} - allowed)
        if unknown:
            raise CwError(
                "Unknown output field", ErrorCode.USAGE_ERROR,
                details=", ".join(unknown), exit_code=2,
            )
    has_page_option = any((getattr(args, "limit", None), getattr(args, "cursor", None), getattr(args, "all", False)))
    if has_page_option and command not in _PAGINATED_KEYS:
        raise CwError(f"Pagination is not supported for {command}", ErrorCode.USAGE_ERROR, exit_code=2)
    limit = getattr(args, "limit", None)
    if limit is not None and (isinstance(limit, bool) or limit < 1 or limit > MAX_LIMIT):
        raise CwError(f"--limit must be between 1 and {MAX_LIMIT}", ErrorCode.USAGE_ERROR, exit_code=2)
    if getattr(args, "all", False) and (limit is not None or getattr(args, "cursor", None)):
        raise CwError("--all cannot be combined with --limit or --cursor", ErrorCode.USAGE_ERROR, exit_code=2)
    if mode is not OutputMode.HUMAN and command in {"mcp", "remote"}:
        raise CwError(
            f"{command} transport commands do not support the output protocol",
            ErrorCode.USAGE_ERROR, exit_code=2,
        )


def parse_fields(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    fields = tuple(item.strip() for item in value.split(",") if item.strip())
    if not fields or any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*", item) for item in fields):
        raise CwError("--fields contains an invalid field path", ErrorCode.USAGE_ERROR, exit_code=2)
    return tuple(dict.fromkeys(fields))


def parse_records(text: str) -> list[Any]:
    records: list[Any] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise CwError(
                "Command produced invalid structured output", ErrorCode.INTERNAL_ERROR,
                "Run: cw error", details=str(exc),
            ) from exc
    return records


def sanitize_output(value: Any, *, private_roots: Iterable[Path] = ()) -> Any:
    roots = tuple(
        item for root in private_roots
        for item in (str(root), str(root).replace("/", "\\")) if item
    )

    def clean(item: Any) -> Any:
        if isinstance(item, dict):
            result: dict[str, Any] = {}
            for key, nested in item.items():
                label = str(key)
                if label in {"repository_root", "local_path", "executable", "runtime"}:
                    continue
                if _SECRET_KEY.search(label):
                    result[label] = nested if nested in (None, "", [], {}) else "[REDACTED]"
                else:
                    result[label] = clean(nested)
            return result
        if isinstance(item, (list, tuple)):
            return [clean(child) for child in item]
        if isinstance(item, str):
            redacted = redact(item) or ""
            for root in sorted(set(roots), key=len, reverse=True):
                redacted = re.sub(re.escape(root), "<PROJECT_ROOT>", redacted, flags=re.IGNORECASE)
            redacted = _WINDOWS_HOME.sub("~", redacted)
            return _HOME_PATH.sub("~", redacted)
        return item

    return clean(value)


def _project_path(value: Any, path: str) -> tuple[bool, Any]:
    current = value
    for component in path.split("."):
        if not isinstance(current, dict) or component not in current:
            return False, None
        current = current[component]
    return True, current


def _assign_path(target: dict[str, Any], path: str, value: Any) -> None:
    components = path.split(".")
    cursor = target
    for component in components[:-1]:
        cursor = cursor.setdefault(component, {})
    cursor[components[-1]] = value


def project_fields(data: Any, fields: tuple[str, ...]) -> Any:
    if not fields or not isinstance(data, dict):
        return data
    projected: dict[str, Any] = {}
    selected = list(fields)
    selected.extend(_invariant_paths(data))
    for field in dict.fromkeys(selected):
        present, value = _project_path(data, field)
        if not present:
            raise CwError(f"Output field is unavailable: {field}", ErrorCode.USAGE_ERROR, exit_code=2)
        _assign_path(projected, field, value)
    return projected


def _invariant_paths(value: Any, prefix: str = "") -> tuple[str, ...]:
    """Return every governance invariant without evaluating arbitrary selectors."""
    if not isinstance(value, dict):
        return ()
    paths: list[str] = []
    for key, nested in value.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if key in _INVARIANT_KEYS:
            paths.append(path)
        if isinstance(nested, dict):
            paths.extend(_invariant_paths(nested, path))
    return tuple(paths)


def compact_llm_data(command: str, data: Any) -> Any:
    if not isinstance(data, dict):
        return data
    if command == "plan.rebaseline.recover":
        compact = {
            key: data[key] for key in (
                "phase", "review_reference", "review_sha256", "workflow_sha256",
                "state_sha256", "previous_status", "resulting_status", "last_gate",
                "changed", "idempotent_replay", "recovery_id", "backup", "recovery_receipt", "next_action",
            ) if key in data and data[key] is not None
        }
        if compact.get("idempotent_replay") is True:
            compact["mutation"] = "none"
        if "next_action" in compact:
            compact["next_action"] = (
                "Create a separate rebaseline proposal; apply requires independent authorization."
            )
        return compact
    if command == "doctor" or command == "doctor.reviewer":
        compact = dict(data)
        checks = compact.get("checks")
        if isinstance(checks, list):
            compact["checks"] = [
                item for item in checks
                if isinstance(item, dict) and item.get("status") not in {"pass", "neutral"}
            ]
        return compact
    if command == "history":
        compact = dict(data)
        phases = compact.get("phases")
        if isinstance(phases, list):
            compact["phases"] = [
                {key: item[key] for key in ("phase", "name", "current", "approved") if key in item}
                for item in phases if isinstance(item, dict)
            ]
        return compact
    if command == "plan.show":
        compact = {
            key: data[key] for key in (
                "project", "workflow", "status", "current_phase", "goal", "state",
                "active_plan_revision", "active_plan_revision_sha256", "completion_contract",
            ) if key in data
        }
        phases = data.get("phases")
        if isinstance(phases, list):
            compact["phases"] = [
                {key: item[key] for key in ("id", "name", "depends_on", "artifacts", "requires_human_approval") if key in item}
                for item in phases if isinstance(item, dict)
            ]
        return compact
    fields = _LLM_FIELDS.get(command)
    if fields:
        available = tuple(field for field in fields if field in data)
        return project_fields(data, available)
    return data


def _cursor(command: str, offset: int, fingerprint: str) -> str:
    body = json.dumps(
        {"v": 1, "c": command, "o": offset, "s": fingerprint},
        separators=(",", ":"), sort_keys=True,
    ).encode()
    digest = hashlib.sha256(b"cw.output.v1:" + body).hexdigest()[:16]
    return base64.urlsafe_b64encode(body + b"." + digest.encode()).decode().rstrip("=")


def _cursor_offset(command: str, token: str | None, fingerprint: str) -> int:
    if not token:
        return 0
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        body, digest = raw.rsplit(b".", 1)
        expected = hashlib.sha256(b"cw.output.v1:" + body).hexdigest()[:16].encode()
        payload = json.loads(body)
        if digest != expected or payload != {
            "v": 1, "c": command, "o": payload.get("o"), "s": fingerprint,
        }:
            raise ValueError
        offset = payload["o"]
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError
        return offset
    except (ValueError, KeyError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise CwError("Invalid pagination cursor", ErrorCode.USAGE_ERROR, exit_code=2) from exc


def paginate(command: str, data: Any, args: argparse.Namespace, mode: OutputMode) -> tuple[Any, dict[str, Any] | None]:
    key = _PAGINATED_KEYS.get(command)
    if key is None or not isinstance(data, dict) or not isinstance(data.get(key), list):
        return data, None
    if getattr(args, "all", False):
        return data, {"limit": len(data[key]), "has_more": False, "next_cursor": None}
    requested = getattr(args, "limit", None)
    if requested is None and mode is not OutputMode.LLM and not getattr(args, "cursor", None):
        return data, None
    limit = requested or LLM_DEFAULT_LIMIT
    items = data[key]
    fingerprint = hashlib.sha256(
        json.dumps(items, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:16]
    offset = _cursor_offset(command, getattr(args, "cursor", None), fingerprint)
    if offset > len(items):
        raise CwError("Pagination cursor is outside the current result", ErrorCode.USAGE_ERROR, exit_code=2)
    end = min(len(items), offset + limit)
    projected = dict(data)
    projected[key] = items[offset:end]
    has_more = end < len(items)
    return projected, {
        "limit": limit,
        "has_more": has_more,
        "next_cursor": _cursor(command, end, fingerprint) if has_more else None,
    }


def result_status(command: str, exit_code: int, data: Any) -> OutputStatus:
    if exit_code == 130:
        return OutputStatus.CANCELLED
    if isinstance(data, dict):
        code = str((data.get("error") or {}).get("code", "")) if isinstance(data.get("error"), dict) else ""
        status = str(data.get("status", ""))
        if code == "AUTHORIZATION_REQUIRED" or status == "AUTHORIZATION_REQUIRED":
            return OutputStatus.AUTHORIZATION_REQUIRED
        if status in {"BLOCKED", "FAILED"} or exit_code == 3:
            return OutputStatus.BLOCKED
        if data.get("idempotent_replay") is True and status in {"SUCCEEDED", "success", "SUCCESS"}:
            return OutputStatus.NOOP
    return OutputStatus.SUCCESS if exit_code == 0 else OutputStatus.ERROR


def envelope(
    command: str,
    *,
    status: OutputStatus,
    changed: bool,
    data: Any | None = None,
    error: dict[str, Any] | None = None,
    operation_id: str | None = None,
    page: dict[str, Any] | None = None,
    gate: dict[str, Any] | None = None,
    truncation_reason: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema": OUTPUT_SCHEMA,
        "ok": status in {OutputStatus.SUCCESS, OutputStatus.NOOP, OutputStatus.PARTIAL},
        "command": command,
        "status": status.value,
        "changed": changed,
    }
    if operation_id:
        result["operation_id"] = operation_id
    if gate:
        result["gate"] = gate
    if error is not None:
        result["error"] = error
    else:
        result["data"] = {} if data is None else data
    if page is not None:
        result["page"] = page
        result["truncation"] = {
            "truncated": bool(page.get("has_more")) or truncation_reason is not None,
            "reason": "page_limit" if page.get("has_more") else truncation_reason,
        }
    else:
        result["truncation"] = {
            "truncated": truncation_reason is not None,
            "reason": truncation_reason,
        }
    return result


def changed_for(command: str, status: OutputStatus, data: Any) -> bool:
    if status in {OutputStatus.ERROR, OutputStatus.BLOCKED, OutputStatus.AUTHORIZATION_REQUIRED, OutputStatus.CANCELLED, OutputStatus.NOOP}:
        return False
    if command in _READ_COMMANDS or command.endswith((".show", ".diagnose", ".remote-plan")):
        return False
    if command == "plan.amend" and isinstance(data, dict) and data.get("dry_run") is True:
        return False
    if command == "plan.rebaseline.recover" and isinstance(data, dict):
        # `changed` describes persistence performed by this invocation. An
        # exact replay reports the recovered domain result but performs no write.
        if data.get("idempotent_replay") is True:
            return False
        return data.get("changed") is True
    if command == "review.recover-infrastructure" and isinstance(data, dict):
        return data.get("changed") is True and data.get("idempotent_replay") is not True
    if command == "review.authorize-retry" and isinstance(data, dict):
        return data.get("changed") is True and data.get("idempotent_replay") is not True
    return True


def prepare_data(
    command: str, data: Any, args: argparse.Namespace, mode: OutputMode,
) -> tuple[Any, dict[str, Any] | None, str | None]:
    fields = parse_fields(getattr(args, "fields", None))
    projection = False
    if mode is OutputMode.LLM and not fields and not getattr(args, "expand", False):
        compact = compact_llm_data(command, data)
        projection = compact != data
        data = compact
    if fields:
        data = project_fields(data, fields)
    data, page = paginate(command, data, args, mode)
    return data, page, "llm_projection" if projection else None


def output_schema_document() -> dict[str, Any]:
    path = Path(__file__).resolve().parent / "schemas" / "output-v1.schema.json"
    return json.loads(path.read_text(encoding="utf-8"))
