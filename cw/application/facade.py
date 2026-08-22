from __future__ import annotations

import uuid
from pathlib import Path

from cw.core.authorization import OperationContext
from cw.core.authorization import ActorOrigin
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
from cw.core.revisions import apply_rebaseline

from .capabilities import CAPABILITIES
from .actions import (
    request_current_review,
    retry_current_operation,
    start_current_phase,
    validate_current_phase,
)
from .context import load_project_context
from .models import (
    ApplicationError,
    ApplicationErrorCode,
    OperationResult,
    OperationStatus,
    application_error,
)
from .operations import OperationSupervisor
from .projects import ProjectResolver, ResolvedProject
from .status import explain_status, project_status


def _raw_context(root: Path):
    return load_project_context(root, validate=False)


class CWApplication:
    """Intentional public facade shared by CLI and future local adapters."""

    def __init__(
        self,
        *,
        allowed_roots: tuple[Path, ...] | list[Path],
        review_backend_factory: object | None = None,
        operation_workers: int = 2,
    ) -> None:
        self.projects = ProjectResolver(allowed_roots)
        if review_backend_factory is None:
            from cw.adapters.codex import CodexAdapter

            review_backend_factory = CodexAdapter
        self._review_backend_factory = review_backend_factory
        self._operations = OperationSupervisor(max_workers=operation_workers)

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

    @staticmethod
    def _controlled_request(request: OperationContext, capability: str) -> None:
        policy = CAPABILITIES.get(capability)
        if (
            policy is None
            or request.requested_capability != capability
            or policy.classification.value not in {"EXECUTION", "CONTROLLED_STATE_MUTATION"}
        ):
            raise ApplicationError(
                ApplicationErrorCode.AUTHORIZATION_REQUIRED,
                "Operation capability does not match the controlled action",
            )
        if request.authorization is not None:
            raise ApplicationError(
                ApplicationErrorCode.AUTHORIZATION_REQUIRED,
                "Controlled actions do not accept caller-forged authorization grants",
            )
        if request.actor.origin in {
            ActorOrigin.PLANNER, ActorOrigin.REVIEWER, ActorOrigin.INTERNAL_SUPERVISOR,
        }:
            raise ApplicationError(
                ApplicationErrorCode.AUTHORIZATION_REQUIRED,
                "Internal planning and review actors cannot request controlled actions",
            )

    @staticmethod
    def _phase_hint(project: ResolvedProject) -> str | None:
        try:
            _, state, _ = load_project_context(project.root, validate=False)
        except CwError as exc:
            raise application_error(exc) from exc
        value = state.get("current_phase")
        return value if isinstance(value, str) else None

    def phase_start(
        self, project: ResolvedProject, request: OperationContext,
    ) -> OperationResult:
        self._controlled_request(request, "phase.start")
        phase = self._phase_hint(project)
        return self._operations.submit(
            project, request,
            operation="phase.start", capability="phase.start", phase=phase,
            payload={}, executor=lambda: start_current_phase(project),
        )

    def validate(
        self, project: ResolvedProject, request: OperationContext,
    ) -> OperationResult:
        self._controlled_request(request, "validation.run")
        phase = self._phase_hint(project)
        return self._operations.submit(
            project, request,
            operation="validation.run", capability="validation.run", phase=phase,
            payload={},
            executor=lambda: validate_current_phase(project, request.operation_id),
        )

    def request_review(
        self, project: ResolvedProject, request: OperationContext,
    ) -> OperationResult:
        self._controlled_request(request, "review.run")
        backend_factory = self._review_backend_factory
        if not callable(backend_factory):
            raise ApplicationError(
                ApplicationErrorCode.INFRASTRUCTURE_FAILURE,
                "The independent reviewer backend is unavailable",
                retryable=True,
            )
        phase = self._phase_hint(project)
        return self._operations.submit(
            project, request,
            operation="review.request", capability="review.run", phase=phase,
            payload={},
            executor=lambda: request_current_review(project, backend_factory),
        )

    def retry(
        self, project: ResolvedProject, request: OperationContext,
    ) -> OperationResult:
        self._controlled_request(request, "retry.run")
        backend_factory = self._review_backend_factory
        if not callable(backend_factory):
            raise ApplicationError(
                ApplicationErrorCode.INFRASTRUCTURE_FAILURE,
                "The independent reviewer backend is unavailable",
                retryable=True,
            )
        phase = self._phase_hint(project)
        return self._operations.submit(
            project, request,
            operation="operation.retry", capability="retry.run", phase=phase,
            payload={},
            executor=lambda: retry_current_operation(project, backend_factory),
        )

    def operation_status(
        self, project: ResolvedProject, *, target_operation_id: str,
        request: OperationContext,
    ) -> OperationResult:
        self._read_request("operation.read", request.operation_id, request)
        return self._operations.status(project, target_operation_id)

    def cancel_operation(
        self, project: ResolvedProject, *, target_operation_id: str,
        request: OperationContext,
    ) -> OperationResult:
        self._controlled_request(request, "operation.cancel")
        return self._operations.cancel(project, target_operation_id)

    def shutdown(self, *, wait: bool = True) -> None:
        self._operations.shutdown(wait=wait)

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

    def rebaseline_plan(
        self, project: ResolvedProject, proposal_id: str, request: OperationContext,
    ) -> OperationResult:
        """Apply one exact plan proposal through the high-consequence boundary.

        This capability is intentionally absent from MCP/remote registries. A
        trusted host must create the typed human authorization ceremony.
        """
        capability_name = CAPABILITIES["plan.rebaseline"].name
        if request.requested_capability != capability_name:
            raise application_error(CwError(
                "Operation capability does not match", ErrorCode.AUTHORIZATION_REQUIRED,
            ))
        try:
            with operation_lock(project.root, "application-plan-rebaseline"):
                _, state, workflow = load_project_context(project.root)
                data = apply_rebaseline(
                    project.root, workflow, state, proposal_id, request,
                )
        except CwError as exc:
            raise application_error(exc) from exc
        return self._result(
            project, "plan.rebaseline", capability_name, data,
            request.operation_id, replay=bool(data.get("idempotent_replay")),
        )
