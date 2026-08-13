"""Shared, offline-safe infrastructure for CW's public hero recording."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from cw.core.diagnostics import redact


SCHEMA_VERSION = 1
RECORDING_KIND = "real-workflow-recording"
GOAL = "Add a /health endpoint with automated tests in one development phase"
EVENT_TYPES = {
    "prompt", "command", "info", "active", "success", "warning",
    "phase", "validation", "review", "gate", "complete",
}
ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
WINDOWS_PATH_RE = re.compile(r"(?i)\b[A-Z]:\\+(?:Users|Documents and Settings|Temp)\\+")
POSIX_PRIVATE_PATH_RE = re.compile(r"/(?:home|Users|private/var/folders|tmp)/[^\s\"']+")
SECRET_PATTERNS = (
    re.compile(r"(?i)authorization\s*:\s*(?:bearer|basic)\s+(?!\[REDACTED\])\S+"),
    re.compile(r"(?i)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|password|secret)\s*[=:]\s*(?!\[REDACTED\])\S+"),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{12,}|sk-[A-Za-z0-9_-]{12,})\b"),
)


class HeroDemoError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    duration_ms: int

    def json_objects(self) -> list[dict[str, Any]]:
        objects: list[dict[str, Any]] = []
        for line in self.stdout.splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                objects.append(value)
        return objects


def source_root() -> Path:
    return _REPOSITORY_ROOT


def sha256_tree(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def sanitize_public_text(value: str, *, private_roots: Iterable[Path] = ()) -> str:
    """Redact diagnostics and remove machine-specific presentation content."""

    clean = ANSI_RE.sub("", redact(value) or "")
    roots = {str(Path.home()), *(str(path.resolve()) for path in private_roots)}
    for root in sorted((item for item in roots if item), key=len, reverse=True):
        clean = clean.replace(root, "~/demo-api")
        clean = clean.replace(root.replace("/", "\\"), "~/demo-api")
    clean = WINDOWS_PATH_RE.sub("~/demo-api/", clean)
    clean = POSIX_PRIVATE_PATH_RE.sub("~/demo-api", clean)
    return " ".join(clean.split())


def public_event(event_type: str, text: str, **extra: Any) -> dict[str, Any]:
    event = {"type": event_type, "text": text}
    event.update({key: value for key, value in extra.items() if value is not None})
    return event


def _run(
    argv: Sequence[str], *, cwd: Path, environment: dict[str, str], timeout: int,
) -> CommandResult:
    started = time.monotonic()
    completed = subprocess.run(
        list(argv), cwd=cwd, env=environment, text=True, capture_output=True,
        stdin=subprocess.DEVNULL, timeout=timeout, check=False,
    )
    return CommandResult(
        tuple(argv), completed.returncode, completed.stdout, completed.stderr,
        max(0, round((time.monotonic() - started) * 1000)),
    )


def _require_success(result: CommandResult, stage: str, private_root: Path) -> None:
    if result.returncode == 0:
        return
    detail = sanitize_public_text(
        (result.stderr or result.stdout)[-2000:], private_roots=(private_root,),
    )
    raise HeroDemoError(f"{stage} failed with exit code {result.returncode}: {detail}")


def _single_json(result: CommandResult, stage: str) -> dict[str, Any]:
    values = result.json_objects()
    if len(values) != 1:
        raise HeroDemoError(f"{stage} did not return one JSON object")
    return values[0]


def resolve_installed_cw(value: str | None = None) -> Path:
    resolved = shutil.which(value or "cw") if value is None or not Path(value).is_file() else value
    if not resolved:
        raise HeroDemoError("Installed CW executable was not found")
    executable = Path(resolved).resolve()
    if not executable.is_file():
        raise HeroDemoError("Resolved CW executable is not a file")
    return executable


def verify_installed_cw(executable: Path, expected_version: str) -> dict[str, Any]:
    environment = {**os.environ, "CW_NO_UPDATE_CHECK": "1", "NO_COLOR": "1"}
    result = _run(
        [str(executable), "version", "--json"], cwd=source_root(),
        environment=environment, timeout=30,
    )
    _require_success(result, "CW version check", source_root())
    payload = _single_json(result, "CW version check")
    if payload.get("version") != expected_version:
        raise HeroDemoError(
            f"Installed CW {payload.get('version')} does not match source {expected_version}"
        )
    return payload


class DemoWorkspace:
    """A disposable Git repository copied from the immutable demo template."""

    def __init__(self, template: Path, *, keep: bool = False) -> None:
        self.template = template
        self.keep = keep
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        self.parent: Path | None = None
        self.root: Path | None = None
        self._source_digest: str | None = None

    def __enter__(self) -> Path:
        self._source_digest = sha256_tree(self.template)
        if self.keep:
            self.parent = Path(tempfile.mkdtemp(prefix="cw-hero-demo-"))
        else:
            self._temporary = tempfile.TemporaryDirectory(prefix="cw-hero-demo-")
            self.parent = Path(self._temporary.name)
        self.root = self.parent / "demo-api"
        shutil.copytree(self.template, self.root)
        return self.root

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self._source_digest != sha256_tree(self.template):
            raise HeroDemoError("Hero template was modified during recording")
        if self._temporary is not None:
            self._temporary.cleanup()


def _git_initialize(root: Path, environment: dict[str, str]) -> None:
    for argv in (
        ["git", "init", "-q"],
        ["git", "add", "."],
        [
            "git", "-c", "user.name=CW Demo Recorder",
            "-c", "user.email=demo@invalid.example", "commit", "-q", "-m",
            "Initial demo fixture",
        ],
    ):
        result = _run(argv, cwd=root, environment=environment, timeout=30)
        _require_success(result, "Git fixture initialization", root)


def _read_run_events(root: Path) -> list[dict[str, Any]]:
    records: list[tuple[str, int, dict[str, Any]]] = []
    directory = root / ".cw" / "logs" / "runs"
    for path in sorted(directory.glob("run_*.jsonl")) if directory.is_dir() else ():
        for index, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                records.append((str(value.get("timestamp") or ""), index, value))
    records.sort(key=lambda item: (item[0], item[1]))
    return [value for _, _, value in records]


def _normalize_execution_events(
    records: list[dict[str, Any]], *, private_root: Path,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for record in records:
        source = str(record.get("event_type") or "")
        candidate: dict[str, Any] | None = None
        if source == "PROCESS_STARTED":
            candidate = public_event("success", "Codex process started")
        elif source == "SESSION_STARTED":
            candidate = public_event("success", "Codex session initialized")
        elif source == "TURN_STARTED" and not any(item["type"] == "active" for item in events):
            candidate = public_event("active", "Implementation active")
        elif source == "COMMAND_STARTED" and isinstance(record.get("command"), str):
            command = sanitize_public_text(record["command"], private_roots=(private_root,))
            candidate = public_event("active", f"Running {command}", command=command)
        elif source == "COMMAND_COMPLETED" and isinstance(record.get("command"), str):
            command = sanitize_public_text(record["command"], private_roots=(private_root,))
            exit_code = record.get("exit_code")
            candidate = public_event(
                "success" if exit_code in {0, None} else "warning",
                f"{command} completed" if exit_code in {0, None} else f"{command} failed",
                command=command,
                actual_duration_ms=record.get("duration_ms"),
            )
        elif source == "FILE_CHANGED":
            count = len(record.get("files") or [])
            candidate = public_event("info", f"Project files updated · {count} change{'s' if count != 1 else ''}")
        elif source == "VALIDATION_STARTED":
            candidate = public_event("validation", "Deterministic validation started")
        elif source == "VALIDATION_COMPLETED":
            text = sanitize_public_text(str(record.get("summary") or "Validation passed"))
            candidate = public_event("validation", text, result="passed")
        elif source == "REVIEW_STARTED":
            candidate = public_event("review", "Independent review started")
        elif source == "REVIEW_COMPLETED":
            decision = str(record.get("status") or record.get("summary") or "").upper().replace(" ", "_")
            candidate = public_event("review", decision, result=decision)
        elif source == "GATE_CREATED":
            candidate = public_event("gate", "Approval gate verified", result="verified")
        elif source == "PHASE_ADVANCED" and record.get("status") == "completed":
            candidate = public_event("complete", "Workflow complete")
        if candidate is None:
            continue
        key = (candidate["type"], candidate["text"])
        if key not in seen:
            events.append(candidate)
            seen.add(key)
    return events


def _source_commit(root: Path) -> str | None:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True,
        capture_output=True, timeout=5, check=False,
    )
    value = completed.stdout.strip()
    return value if completed.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", value) else None


def record_real_workflow(
    *, executable: Path, template: Path, expected_version: str, keep_temp: bool = False,
) -> tuple[dict[str, Any], Path | None]:
    """Run the real product and return a public artifact only after a valid gate."""

    verify_installed_cw(executable, expected_version)
    environment = {
        **os.environ,
        "CW_NO_UPDATE_CHECK": "1",
        "NO_COLOR": "1",
        "PYTHONUNBUFFERED": "1",
    }
    retained: Path | None = None
    with DemoWorkspace(template, keep=keep_temp) as project:
        if keep_temp:
            retained = project
        _git_initialize(project, environment)
        events: list[dict[str, Any]] = []

        def invoke(arguments: list[str], label: str, display: str, timeout: int = 1800) -> CommandResult:
            events.append(public_event("command", display, command=display))
            result = _run(
                [str(executable), *arguments], cwd=project,
                environment=environment, timeout=timeout,
            )
            _require_success(result, label, project)
            return result

        init = invoke(["init", "--json"], "CW initialization", "cw init", 60)
        _single_json(init, "CW initialization")
        events.append(public_event("success", "Project initialized"))

        plan = invoke(
            ["plan", "--goal", GOAL, "--json"], "CW planning",
            f'cw plan --goal "{GOAL}"',
        )
        plan_payload = _single_json(plan, "CW planning")
        if plan_payload.get("status") != "PROPOSED":
            raise HeroDemoError("Planner did not produce a proposed plan")
        events.append(public_event("success", "Plan proposed"))

        shown = invoke(["plan", "show", "--json"], "Plan inspection", "cw plan show", 60)
        shown_payload = _single_json(shown, "Plan inspection")
        phases = shown_payload.get("phases")
        if not isinstance(phases, list) or len(phases) != 1:
            raise HeroDemoError("Official hero recording requires exactly one real planned phase")
        phase = phases[0]
        if not isinstance(phase, dict) or not isinstance(phase.get("id"), str) or not isinstance(phase.get("name"), str):
            raise HeroDemoError("Planner returned invalid phase presentation data")
        events.append(public_event("phase", f"{phase['id']} · {phase['name']}"))

        approved = invoke(["plan", "approve", "--json"], "Plan approval", "cw plan approve", 60)
        if _single_json(approved, "Plan approval").get("status") != "READY":
            raise HeroDemoError("Plan approval did not make the workflow ready")
        events.append(public_event("success", "Plan approved"))

        invoke(["start", "--json"], "CW implementation", "cw start")
        start_events = _normalize_execution_events(_read_run_events(project), private_root=project)
        events.extend(start_events)
        status_result = _run(
            [str(executable), "status", "--json"], cwd=project,
            environment=environment, timeout=60,
        )
        status_objects = status_result.json_objects()
        status = status_objects[-1] if status_objects else {}
        if status.get("ready") and not status.get("is_complete"):
            invoke(["review", "--json"], "Independent review", "cw review")
            all_run_events = _normalize_execution_events(_read_run_events(project), private_root=project)
            events.extend(event for event in all_run_events if event not in start_events)

        final_status_result = _run(
            [str(executable), "status", "--json"], cwd=project,
            environment=environment, timeout=60,
        )
        _require_success(final_status_result, "Final workflow verification", project)
        final_status = _single_json(final_status_result, "Final workflow verification")
        approved_count = final_status.get("approved_count")
        phase_count = final_status.get("phase_count")
        gate_states = final_status.get("gate_states")
        valid_gates = sum(
            value == "approved" for value in gate_states.values()
        ) if isinstance(gate_states, dict) else 0
        if not (
            final_status.get("is_complete") is True
            and final_status.get("effective_state") == "COMPLETED"
            and isinstance(approved_count, int) and approved_count >= 1
            and approved_count == phase_count == valid_gates
        ):
            raise HeroDemoError("Real workflow did not reach canonical completion with valid gates")

        # A Stop hook can finish before all live events are persisted in one run;
        # the final status is authoritative, but the public narrative still must
        # contain observable validation/review/gate events from CW's run logs.
        required_types = {"active", "validation", "review", "gate", "complete"}
        missing = required_types - {event["type"] for event in events}
        if missing:
            raise HeroDemoError(f"Real execution did not emit required public events: {sorted(missing)}")

        artifact = {
            "schema_version": SCHEMA_VERSION,
            "product": "CW",
            "cw_version": expected_version,
            "recording_kind": RECORDING_KIND,
            "goal": GOAL,
            "brand": {
                "name": "CW", "product_name": "Codex Workflow", "maker": "Queopius",
            },
            "provenance": {
                "recorded_from_real_workflow": True,
                "source_commit": _source_commit(source_root()),
            },
            "events": events,
            "final_result": {
                "workflow_status": "COMPLETED",
                "approved_phases": approved_count,
                "valid_gates": valid_gates,
            },
        }
        validate_artifact(artifact, expected_version=expected_version)
        return artifact, retained


def _require_type(value: Any, expected: type, field: str) -> None:
    if not isinstance(value, expected) or expected is int and isinstance(value, bool):
        raise HeroDemoError(f"{field} has an invalid type")


def validate_artifact(value: Any, *, expected_version: str | None = None) -> dict[str, Any]:
    """Validate the stable public contract without network or optional packages."""

    if not isinstance(value, dict):
        raise HeroDemoError("Hero demo must be a JSON object")
    allowed_top = {
        "schema_version", "product", "cw_version", "recording_kind", "goal",
        "brand", "provenance", "events", "final_result",
    }
    if set(value) != allowed_top:
        raise HeroDemoError(f"Hero demo fields are invalid: {sorted(set(value) ^ allowed_top)}")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise HeroDemoError("Unsupported hero demo schema version")
    if value.get("product") != "CW" or value.get("recording_kind") != RECORDING_KIND:
        raise HeroDemoError("Hero demo product or recording kind is invalid")
    _require_type(value.get("cw_version"), str, "cw_version")
    _require_type(value.get("goal"), str, "goal")
    if expected_version is not None and value.get("cw_version") != expected_version:
        raise HeroDemoError(
            f"Hero recording version {value.get('cw_version')} does not match VERSION {expected_version}"
        )
    brand = value.get("brand")
    if brand != {"name": "CW", "product_name": "Codex Workflow", "maker": "Queopius"}:
        raise HeroDemoError("Hero demo brand metadata is invalid")
    provenance = value.get("provenance")
    if not isinstance(provenance, dict) or set(provenance) != {"recorded_from_real_workflow", "source_commit"}:
        raise HeroDemoError("Hero demo provenance is invalid")
    if provenance.get("recorded_from_real_workflow") is not True:
        raise HeroDemoError("Official hero demo must come from a real workflow")
    commit = provenance.get("source_commit")
    if commit is not None and (not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit)):
        raise HeroDemoError("Hero demo source commit is invalid")

    events = value.get("events")
    if not isinstance(events, list) or not events:
        raise HeroDemoError("Hero demo events are missing")
    allowed_event = {"type", "text", "command", "result", "actual_duration_ms", "emphasis"}
    for index, event in enumerate(events):
        if not isinstance(event, dict) or not {"type", "text"}.issubset(event) or not set(event) <= allowed_event:
            raise HeroDemoError(f"Event {index} has invalid fields")
        if event["type"] not in EVENT_TYPES:
            raise HeroDemoError(f"Event {index} has unknown type {event['type']!r}")
        if not isinstance(event["text"], str) or not event["text"].strip():
            raise HeroDemoError(f"Event {index} has invalid text")
        if "command" in event and not isinstance(event["command"], str):
            raise HeroDemoError(f"Event {index} has invalid command")
        if "actual_duration_ms" in event and (
            not isinstance(event["actual_duration_ms"], int)
            or isinstance(event["actual_duration_ms"], bool)
            or event["actual_duration_ms"] < 0
        ):
            raise HeroDemoError(f"Event {index} has invalid duration")

    public_strings = [
        item
        for event in events
        for item in (event.get("text"), event.get("command"))
        if isinstance(item, str)
    ]
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True)
    visible_text = "\n".join(public_strings)
    if ANSI_RE.search(visible_text):
        raise HeroDemoError("Hero demo contains ANSI terminal escapes")
    if POSIX_PRIVATE_PATH_RE.search(visible_text) or WINDOWS_PATH_RE.search(visible_text):
        raise HeroDemoError("Hero demo contains an absolute private path")
    if any(pattern.search(visible_text) for pattern in SECRET_PATTERNS):
        raise HeroDemoError("Hero demo contains a potential secret")
    for forbidden in ("AuthRequired", "invalid_token", "MCP startup", "mcp.vercel", "chain of thought"):
        if forbidden.lower() in serialized.lower():
            raise HeroDemoError(f"Hero demo contains forbidden diagnostic content: {forbidden}")

    def first(predicate: Any) -> int:
        return next((index for index, event in enumerate(events) if predicate(event)), -1)

    init_index = first(lambda event: event.get("type") == "command" and event.get("command") == "cw init")
    plan_index = first(lambda event: event.get("type") == "command" and str(event.get("command", "")).startswith("cw plan --goal"))
    approve_index = first(lambda event: event.get("type") == "command" and event.get("command") == "cw plan approve")
    implement_index = first(lambda event: event.get("type") == "active" and "Implementation" in event.get("text", ""))
    validation_index = first(lambda event: event.get("type") == "validation" and event.get("result") == "passed")
    review_index = first(lambda event: event.get("type") == "review" and event.get("result") == "APPROVE")
    gate_index = first(lambda event: event.get("type") == "gate" and event.get("result") == "verified")
    complete_indexes = [index for index, event in enumerate(events) if event.get("type") == "complete"]
    if not complete_indexes:
        raise HeroDemoError("Hero demo has no completion event")
    complete_index = complete_indexes[0]
    sequence = [init_index, plan_index, approve_index, implement_index, validation_index, review_index, gate_index, complete_index]
    if any(index < 0 for index in sequence) or sequence != sorted(sequence):
        raise HeroDemoError("Hero demo does not preserve PLAN → IMPLEMENT → VALIDATE → REVIEW → GATE → COMPLETE")
    if complete_index != len(events) - 1 or len(complete_indexes) != 1:
        raise HeroDemoError("Hero demo must end exactly once at workflow completion")

    final_result = value.get("final_result")
    if not isinstance(final_result, dict) or set(final_result) != {
        "workflow_status", "approved_phases", "valid_gates",
    }:
        raise HeroDemoError("Hero demo final result is invalid")
    if final_result.get("workflow_status") != "COMPLETED":
        raise HeroDemoError("Hero demo workflow did not complete")
    for field in ("approved_phases", "valid_gates"):
        _require_type(final_result.get(field), int, f"final_result.{field}")
        if final_result[field] < 1:
            raise HeroDemoError(f"final_result.{field} must be positive")
    if final_result["approved_phases"] != final_result["valid_gates"]:
        raise HeroDemoError("Approved phase and valid gate evidence disagree")
    return value


def load_and_validate(path: Path, *, expected_version: str | None = None) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise HeroDemoError(f"Hero demo artifact is missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise HeroDemoError(f"Hero demo JSON is malformed: {exc}") from exc
    return validate_artifact(value, expected_version=expected_version)


def atomic_write_artifact(path: Path, artifact: dict[str, Any]) -> None:
    """Validate a candidate and replace the last-known-good artifact atomically."""

    validate_artifact(artifact)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(artifact, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        load_and_validate(temporary, expected_version=str(artifact["cw_version"]))
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
