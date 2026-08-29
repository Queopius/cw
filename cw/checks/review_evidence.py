from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from cw.core.completion import contract_payload
from cw.core.errors import CwError, ErrorCode
from cw.core.models import Phase, Workflow
from cw.core.utils import sha256_bytes

EVIDENCE_BUNDLE_SCHEMA = "cw.semantic-review-evidence.v1"
MAX_ARTIFACT_BYTES = 1_048_576
MAX_BUNDLE_ARTIFACT_BYTES = 4_194_304
_READ_CHUNK_BYTES = 65_536


@dataclass(frozen=True, slots=True)
class SemanticReviewEvidenceBundle:
    """Immutable, canonical evidence passed to the semantic reviewer."""

    canonical_json: str
    sha256: str
    artifact_paths: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        payload = json.loads(self.canonical_json)
        if not isinstance(payload, dict):  # pragma: no cover - construction invariant
            raise TypeError("Semantic review evidence bundle is not an object")
        return payload


def _unavailable(reason: str, *, artifact: str | None = None) -> CwError:
    details = f"reason={reason}"
    if artifact is not None:
        details += f"; artifact={artifact}"
    return CwError(
        "Semantic review evidence is unavailable",
        ErrorCode.REVIEW_EVIDENCE_UNAVAILABLE,
        "Correct the declared artifact evidence, then run: cw review",
        details=details,
    )


def _relative_artifact(value: str) -> Path:
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if (
        not value
        or "\x00" in value
        or posix.is_absolute()
        or windows.is_absolute()
        or bool(windows.drive)
        or ".." in posix.parts
        or ".." in windows.parts
    ):
        raise _unavailable("unsafe_relative_path", artifact=value)
    path = Path(value)
    if not path.parts or path == Path("."):
        raise _unavailable("unsafe_relative_path", artifact=value)
    return path


def _read_artifact(
    root: Path,
    value: str,
    expected_sha256: str,
    *,
    max_file_bytes: int,
) -> tuple[bytes, os.stat_result]:
    relative = _relative_artifact(value)
    try:
        project_root = root.resolve(strict=True)
        candidate = root / relative
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(project_root)
        metadata = candidate.lstat()
    except (OSError, ValueError) as exc:
        raise _unavailable("missing_or_outside_project", artifact=value) from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise _unavailable("not_a_regular_file", artifact=value)
    if metadata.st_size > max_file_bytes:
        raise _unavailable("per_file_size_limit", artifact=value)

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(candidate, flags)
    except OSError as exc:
        raise _unavailable("safe_open_failed", artifact=value) from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino)
        ):
            raise _unavailable("file_identity_changed", artifact=value)
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(descriptor, min(_READ_CHUNK_BYTES, max_file_bytes + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > max_file_bytes:
                raise _unavailable("per_file_size_limit", artifact=value)
        after = os.fstat(descriptor)
        if (after.st_dev, after.st_ino, after.st_size) != (
            opened.st_dev,
            opened.st_ino,
            size,
        ):
            raise _unavailable("file_changed_during_read", artifact=value)
    finally:
        os.close(descriptor)

    raw = b"".join(chunks)
    if sha256_bytes(raw) != expected_sha256:
        raise _unavailable("hash_mismatch", artifact=value)
    return raw, opened


def _normalize_text(raw: bytes, *, artifact: str) -> str:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _unavailable("artifact_is_not_utf8_text", artifact=artifact) from exc
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _criteria(phase: Phase) -> list[dict[str, str]]:
    return [
        {
            "id": criterion.id,
            "description": criterion.description,
            "severity": criterion.severity.value,
        }
        for criterion in phase.acceptance_criteria
    ]


def build_semantic_review_evidence_bundle(
    root: Path,
    workflow: Workflow,
    phase: Phase,
    readiness: dict[str, Any],
    receipt_reference: dict[str, Any],
    receipt: dict[str, Any],
    *,
    max_file_bytes: int = MAX_ARTIFACT_BYTES,
    max_total_bytes: int = MAX_BUNDLE_ARTIFACT_BYTES,
) -> SemanticReviewEvidenceBundle:
    """Build a bounded evidence bundle without executing project commands."""

    if max_file_bytes < 1 or max_total_bytes < 1:
        raise ValueError("Semantic review evidence limits must be positive")
    if readiness.get("verification_receipt") != receipt_reference:
        raise _unavailable("readiness_receipt_identity_mismatch")
    if readiness.get("phase") != phase.id or readiness.get("status") != "READY_FOR_REVIEW":
        raise _unavailable("readiness_identity_mismatch")
    if readiness.get("artifacts") != list(phase.artifacts):
        raise _unavailable("readiness_artifact_inventory_mismatch")
    identities = receipt.get("artifact_identities")
    if not isinstance(identities, dict) or set(identities) != set(phase.artifacts):
        raise _unavailable("receipt_artifact_inventory_mismatch")

    artifacts: list[dict[str, Any]] = []
    total_bytes = 0
    for value in phase.artifacts:
        expected_sha256 = identities.get(value)
        if not isinstance(expected_sha256, str):
            raise _unavailable("artifact_hash_missing", artifact=value)
        raw, _ = _read_artifact(
            root,
            value,
            expected_sha256,
            max_file_bytes=max_file_bytes,
        )
        total_bytes += len(raw)
        if total_bytes > max_total_bytes:
            raise _unavailable("global_size_limit")
        artifacts.append(
            {
                "path": value,
                "sha256": expected_sha256,
                "size_bytes": len(raw),
                "text_encoding": "utf-8",
                "text_normalization": "line-endings-to-lf",
                "content": _normalize_text(raw, artifact=value),
            }
        )

    completion = (
        {
            **contract_payload(workflow.completion_target),
            "relevant_requirement_ids": list(phase.completion_requirements),
        }
        if workflow.completion_target is not None
        else None
    )
    payload: dict[str, Any] = {
        "schema": EVIDENCE_BUNDLE_SCHEMA,
        "workflow": {
            "id": workflow.id,
            "repository": workflow.repository,
            "version": workflow.version,
            "status": workflow.status,
            "goal": workflow.goal,
        },
        "phase": {
            "id": phase.id,
            "name": phase.name,
            "objective": phase.objective,
            "depends_on": list(phase.depends_on),
            "review_paths": list(phase.review_paths),
            "declared_artifacts": list(phase.artifacts),
            "acceptance_criteria": _criteria(phase),
            "blocking_criteria": list(phase.blocking_criteria),
            "completion_requirements": list(phase.completion_requirements),
            "requires_human_approval": phase.requires_human_approval,
        },
        "completion_contract": completion,
        "readiness": {
            "session_id": readiness.get("session_id"),
            "phase": readiness.get("phase"),
            "status": readiness.get("status"),
            "artifacts": list(readiness.get("artifacts", [])),
            "checks_executed": list(readiness.get("checks_executed", [])),
            "verification_receipt": receipt_reference,
        },
        "artifacts": artifacts,
        "deterministic_verification": {
            "result": receipt.get("result"),
            "commands": receipt.get("commands"),
            "artifact_hashes": identities,
            "preflight": receipt.get("preflight"),
        },
        "verification_receipt": {
            "reference": receipt_reference.get("reference"),
            "file_sha256": receipt_reference.get("sha256"),
            "receipt_sha256": receipt_reference.get("digest"),
            "receipt_id": receipt_reference.get("receipt_id"),
            "correlation_id": receipt.get("correlation_id"),
            "created_at": receipt.get("created_at"),
            "workflow_sha256": receipt.get("workflow_sha256"),
            "state_sha256_before": receipt.get("state_sha256_before"),
            "plan_revision_id": receipt.get("plan_revision_id"),
            "completion_contract_sha256": receipt.get(
                "completion_contract_sha256"
            ),
        },
        "reviewer_mandate": {
            "all_authorized_evidence_is_included": True,
            "evaluate_only_presented_criteria_and_artifacts": True,
            "forbidden": [
                "execute_commands",
                "calculate_hashes",
                "explore_filesystem",
                "reconstruct_readiness",
                "request_additional_evidence",
            ],
            "output": "one structured semantic result compatible with the supplied schema",
        },
    }
    canonical_json = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return SemanticReviewEvidenceBundle(
        canonical_json=canonical_json,
        sha256=sha256_bytes(canonical_json.encode("utf-8")),
        artifact_paths=phase.artifacts,
    )
