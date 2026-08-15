from __future__ import annotations

import uuid
from pathlib import Path

from cw.core.authorization import OperationContext
from cw.core.audit import audit_history
from cw.core.completion import (
    authorize_extension,
    contract_payload,
    latest_completion_review,
    load_extension_proposal,
    run_completion_review,
)
from cw.core.errors import CwError, ErrorCode
from cw.core.history import history_timeline
from cw.core.initialize import repair
from cw.core.locking import operation_lock
from cw.core.progress import derive_effective_workflow_state
from cw.core.state import load_state

from .capabilities import CAPABILITIES
from .context import load_project_context
from .models import OperationResult, OperationStatus, application_error
from .projects import ProjectResolver, ResolvedProject
from .status import explain_status, project_status


def _raw_context(root: Path):
    return load_project_context(root, validate=False)


class CWApplication:
    """Intentional public facade shared by CLI and future local adapters."""

    def __init__(self, *, allowed_roots: tuple[Path, ...] | list[Path]) -> None:
        self.projects = ProjectResolver(allowed_roots)

    def open_project(self, requested: Path | str) -> ResolvedProject:
        try:
            return self.projects.open(requested)
        except CwError as exc:
            raise application_error(exc) from exc

    def open_project_handle(self, repository_id: str) -> ResolvedProject:
        try:
            return self.projects.open_handle(repository_id)
        except CwError as exc:
            raise application_error(exc) from exc

    @staticmethod
    def _result(
        project: ResolvedProject, operation: str, capability: str, data: dict,
        operation_id: str | None = None, *, replay: bool = False,
        actor_origin: str | None = None,
    ) -> OperationResult:
        return OperationResult(
            operation_id=operation_id or uuid.uuid4().hex,
            operation=operation,
            capability=capability,
            project_id=project.handle.repository_id,
            status=OperationStatus.SUCCEEDED,
            data=data,
            idempotent_replay=replay,
            actor_origin=actor_origin,
        )

    @staticmethod
    def _read_request(
        capability: str, operation_id: str | None, request: OperationContext | None,
    ) -> tuple[str | None, str | None]:
        policy = CAPABILITIES.get(capability)
        if policy is None or policy.mutation or policy.classification.value != "READ":
            raise application_error(CwError(
                "Capability is unavailable to a read-only adapter",
                ErrorCode.AUTHORIZATION_REQUIRED,
            ))
        if request is None:
            return operation_id, None
        if request.requested_capability != capability:
            raise application_error(CwError(
                "Operation capability does not match",
                ErrorCode.AUTHORIZATION_REQUIRED,
            ))
        if operation_id is not None and operation_id != request.operation_id:
            raise application_error(CwError(
                "Operation identity does not match",
                ErrorCode.AUTHORIZATION_REQUIRED,
            ))
        return request.operation_id, request.actor.origin.value

    def status(
        self, project: ResolvedProject, *, operation_id: str | None = None,
        request: OperationContext | None = None,
    ) -> OperationResult:
        operation_id, actor_origin = self._read_request("project.read", operation_id, request)
        try:
            data = project_status(project.root, _raw_context)
        except CwError as exc:
            raise application_error(exc) from exc
        return self._result(
            project, "workflow.status", "project.read", data, operation_id,
            actor_origin=actor_origin,
        )

    def inspect(
        self, project: ResolvedProject, *, operation_id: str | None = None,
        request: OperationContext | None = None,
    ) -> OperationResult:
        operation_id, actor_origin = self._read_request("project.read", operation_id, request)
        try:
            status = project_status(project.root, _raw_context)
        except CwError as exc:
            raise application_error(exc) from exc
        data = {
            "schema_version": 1,
            "project": project.handle.to_dict(),
            "workflow": status,
            "evidence_summary": {
                "approved_gates": status["approved_count"],
                "invalid_gates": status["invalid_gates"],
                "completion_review": status["completion_review"],
                "extension_proposal": status["extension_proposal"],
            },
        }
        return self._result(
            project, "project.inspect", "project.read", data, operation_id,
            actor_origin=actor_origin,
        )

    def explain(
        self, project: ResolvedProject, *, operation_id: str | None = None,
        request: OperationContext | None = None,
    ) -> OperationResult:
        operation_id, actor_origin = self._read_request("project.read", operation_id, request)
        try:
            data = explain_status(project_status(project.root, _raw_context))
        except CwError as exc:
            raise application_error(exc) from exc
        return self._result(
            project, "workflow.explain", "project.read", data, operation_id,
            actor_origin=actor_origin,
        )

    def history(
        self, project: ResolvedProject, *, operation_id: str | None = None,
        request: OperationContext | None = None,
    ) -> OperationResult:
        operation_id, actor_origin = self._read_request("history.read", operation_id, request)
        try:
            _, state, workflow = load_project_context(project.root)
            audit_history(project.root, workflow, state)
        except CwError as exc:
            raise application_error(exc) from exc
        data = {
            "workflow": workflow.id,
            "phases": history_timeline(project.root, workflow, state),
            "events": list(state.get("history", [])),
        }
        return self._result(
            project, "history.inspect", "history.read", data, operation_id,
            actor_origin=actor_origin,
        )

    def completion(
        self, project: ResolvedProject, *, operation_id: str | None = None,
        request: OperationContext | None = None,
    ) -> OperationResult:
        operation_id, actor_origin = self._read_request("completion.read", operation_id, request)
        try:
            _, state, workflow = load_project_context(project.root, validate=False)
            effective = derive_effective_workflow_state(project.root, workflow, state) if workflow.phases else None
            latest = latest_completion_review(project.root) if workflow.completion_target is not None else None
        except CwError as exc:
            raise application_error(exc) from exc
        proposal = None
        if workflow.completion_target is not None and state.get("extension_proposal"):
            try:
                proposal = load_extension_proposal(project.root, state, workflow)
            except CwError:
                proposal = {"invalid_reference": state.get("extension_proposal")}
        data = {
            "schema_version": 1,
            "workflow": workflow.id,
            "mode": "CONTRACT_AWARE" if workflow.completion_target is not None else "LEGACY",
            "contract": contract_payload(workflow.completion_target) if workflow.completion_target else None,
            "planned_scope_complete": bool(effective and effective.planned_scope_complete),
            "completion_satisfied": bool(effective and effective.completion_satisfied),
            "review": latest,
            "proposal": proposal,
            "state": state.get("status"),
            "cycle": state.get("completion_cycle", 0),
        }
        return self._result(
            project, "completion.inspect", "completion.read", data, operation_id,
            actor_origin=actor_origin,
        )

    def gates(
        self, project: ResolvedProject, *, operation_id: str | None = None,
        request: OperationContext | None = None,
    ) -> OperationResult:
        operation_id, actor_origin = self._read_request("gate.read", operation_id, request)
        try:
            status = project_status(project.root, _raw_context)
        except CwError as exc:
            raise application_error(exc) from exc
        data = {
            "schema_version": 1,
            "workflow": status["project"],
            "approved_count": status["approved_count"],
            "phase_count": status["phase_count"],
            "approved_through": status["approved_through"],
            "current_phase": status["phase"],
            "gates": status["gates"],
            "gate_states": status["gate_states"],
            "invalid_gates": status["invalid_gates"],
            "consistent": status["consistent"],
            "issues": status["consistency_issues"],
        }
        return self._result(
            project, "gate.inspect", "gate.read", data, operation_id,
            actor_origin=actor_origin,
        )

    def repair(self, project: ResolvedProject, request: OperationContext) -> OperationResult:
        if request.requested_capability != "project.repair":
            from cw.core.errors import ErrorCode
            raise application_error(CwError("Operation capability does not match", ErrorCode.AUTHORIZATION_REQUIRED))
        try:
            with operation_lock(project.root, "application-repair"):
                backup = repair(project.root)
                data = {
                    "backup": backup.relative_to(project.root).as_posix(),
                    "state": load_state(project.root)["status"],
                }
        except CwError as exc:
            raise application_error(exc) from exc
        return self._result(
            project, "project.repair", "project.repair", data, request.operation_id,
        )

    def completion_review(
        self, project: ResolvedProject, request: OperationContext, backend: object,
    ) -> OperationResult:
        if request.requested_capability != "review.run":
            from cw.core.errors import ErrorCode
            raise application_error(CwError("Operation capability does not match", ErrorCode.AUTHORIZATION_REQUIRED))
        try:
            with operation_lock(project.root, "application-completion-review"):
                _, state, workflow = load_project_context(project.root)
                data = run_completion_review(project.root, workflow, state, backend)
        except CwError as exc:
            raise application_error(exc) from exc
        return self._result(
            project, "completion.review", "review.run", data, request.operation_id,
        )

    def authorize_extension(
        self, project: ResolvedProject, request: OperationContext, *, approve: bool,
    ) -> OperationResult:
        capability = CAPABILITIES["extension.authorize"]
        if request.requested_capability != capability.name:
            from cw.core.errors import ErrorCode
            raise application_error(CwError("Operation capability does not match", ErrorCode.AUTHORIZATION_REQUIRED))
        try:
            with operation_lock(project.root, "application-extension-authorization"):
                _, state, workflow = load_project_context(project.root)
                data = authorize_extension(
                    project.root,
                    workflow,
                    state,
                    approve=approve,
                    authorization=request.authorization,
                )
        except CwError as exc:
            raise application_error(exc) from exc
        return self._result(
            project, "completion.extension.authorize", capability.name, data,
            request.operation_id, replay=bool(data.get("idempotent_replay")),
        )
