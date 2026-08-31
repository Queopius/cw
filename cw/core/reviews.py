from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import CwError, ErrorCode
from .models import Phase, ReviewDecision
from .severity import CriterionSeverity
from .utils import safe_project_path

_EVIDENCE_REFERENCE = re.compile(
    r"^(?P<path>.+?)(?::(?P<line>[1-9][0-9]*(?:-[1-9][0-9]*)?))?$"
)
_EVIDENCE_TOKEN = re.compile(r"[^\s,;]+")
_CRITERION_ID = re.compile(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+){2,}")
_FILE_SUFFIXES = frozenset({
    ".cfg", ".css", ".html", ".ini", ".js", ".json", ".jsx", ".lock",
    ".md", ".py", ".rst", ".sh", ".sql", ".toml", ".ts", ".tsx",
    ".txt", ".xml", ".yaml", ".yml",
})
_LEADING_MARKUP = "`'\"([{<"
_TRAILING_MARKUP = "`'\")]}>,.!?"
_MAX_REPORTED_REFERENCE = 200


@dataclass(frozen=True, slots=True)
class _EvidenceReference:
    canonical: str
    path: str
    start: int
    end: int


def _canonical_reference(token: str, known_paths: frozenset[str]) -> tuple[str, str] | None:
    """Return a canonical repository reference for an explicit path-like token."""
    candidate = token.strip().lstrip(_LEADING_MARKUP).rstrip(_TRAILING_MARKUP)
    if candidate.endswith(":"):
        candidate = candidate[:-1]
    if not candidate or candidate.lower().startswith(("http://", "https://")):
        return None

    match = _EVIDENCE_REFERENCE.fullmatch(candidate)
    if match is None:
        return None
    raw_path = match.group("path")
    normalized_path = raw_path.replace("\\", "/")
    explicitly_rooted = (
        raw_path.startswith(("/", "./", "../", ".\\", "..\\"))
        or re.match(r"^[A-Za-z]:[\\/]", raw_path) is not None
        or raw_path.endswith(("/", "\\"))
    )
    while normalized_path.startswith("./"):
        normalized_path = normalized_path[2:]

    basename = normalized_path.rsplit("/", 1)[-1]
    suffix = Path(basename).suffix.lower()
    path_like = (
        normalized_path in known_paths
        or explicitly_rooted
        or "/" in normalized_path
        or (suffix in _FILE_SUFFIXES and not _CRITERION_ID.fullmatch(basename))
    )
    if not path_like:
        return None

    line = match.group("line")
    if line and "-" in line:
        first, last = map(int, line.split("-", 1))
        if last < first:
            return candidate, normalized_path
    canonical = normalized_path + (f":{line}" if line else "")
    return canonical, normalized_path


def _line_references(line: str, known_paths: frozenset[str]) -> list[_EvidenceReference]:
    """Parse only an explicit leading reference sequence.

    A scalar evidence item historically starts with its path. Multiple paths
    may extend that prefix when separated by comma, semicolon, ``and`` or
    ``or``. Once ordinary explanation begins, later path-looking prose is not
    reinterpreted as evidence.
    """
    tokens = list(_EVIDENCE_TOKEN.finditer(line))
    if not tokens:
        return []
    first = _canonical_reference(tokens[0].group(0), known_paths)
    if first is None:
        return []
    references = [
        _EvidenceReference(first[0], first[1], tokens[0].start(), tokens[0].end())
    ]
    previous_end = tokens[0].end()
    index = 1
    while index < len(tokens):
        token = tokens[index]
        parsed = _canonical_reference(token.group(0), known_paths)
        separator = line[previous_end:token.start()]
        if parsed is not None and re.fullmatch(r"\s*[,;]\s*", separator):
            references.append(
                _EvidenceReference(parsed[0], parsed[1], token.start(), token.end())
            )
            previous_end = token.end()
            index += 1
            continue

        connector = token.group(0).strip(_LEADING_MARKUP + _TRAILING_MARKUP).lower()
        if connector in {"and", "or"} and re.fullmatch(r"\s*(?:[,;]\s*)?", separator):
            next_index = index + 1
            if next_index < len(tokens):
                next_token = tokens[next_index]
                next_parsed = _canonical_reference(next_token.group(0), known_paths)
                connector_separator = line[token.end():next_token.start()]
                if next_parsed is not None and re.fullmatch(r"\s*", connector_separator):
                    references.append(_EvidenceReference(
                        next_parsed[0], next_parsed[1],
                        next_token.start(), next_token.end(),
                    ))
                    previous_end = next_token.end()
                    index = next_index + 1
                    continue
        break
    return references


def _evidence_explanation(line: str, references: list[_EvidenceReference]) -> str:
    fragments: list[str] = []
    cursor = 0
    for reference in references:
        fragments.append(line[cursor:reference.start])
        cursor = reference.end
    fragments.append(line[cursor:])
    explanation = " ".join("".join(fragments).split())
    explanation = re.sub(r"^[\s:,.\-+*\u2022\u2013\u2014]+", "", explanation)
    explanation = re.sub(r"^(?:and|or)\b[\s:,.\-]*", "", explanation, flags=re.IGNORECASE)
    explanation = re.sub(r"[\s:,.\-]+$", "", explanation)
    return explanation


def normalize_evidence_references(
    values: list[str],
    *,
    evidence_paths: frozenset[str] | None = None,
) -> tuple[list[str], list[tuple[str, str]]]:
    """Normalize model evidence into one stable item per filesystem reference.

    The second return value contains ``(canonical_reference, path)`` pairs so
    every recognized reference can be validated independently. Identical
    canonical references are deduplicated in first-seen order.
    """
    known_paths = evidence_paths or frozenset()
    groups: list[tuple[list[_EvidenceReference], list[str]]] = []

    for value in values:
        pending_prose: list[str] = []
        value_groups: list[tuple[list[_EvidenceReference], list[str]]] = []
        for raw_line in value.splitlines() or [value]:
            line = re.sub(r"^\s*(?:[-+*\u2022]|[0-9]+[.)])\s+", "", raw_line).strip()
            if not line:
                continue
            references = _line_references(line, known_paths)
            if not references:
                if value_groups:
                    value_groups[-1][1].append(line)
                else:
                    pending_prose.append(line)
                continue
            explanation = _evidence_explanation(line, references)
            prose = [*pending_prose]
            pending_prose.clear()
            if explanation:
                prose.append(explanation)
            group = (references, prose)
            groups.append(group)
            value_groups.append(group)

    normalized: list[str] = []
    recognized: list[tuple[str, str]] = []
    seen: set[str] = set()
    for references, prose in groups:
        explanation = " ".join(part.strip() for part in prose if part.strip())
        for reference in references:
            recognized.append((reference.canonical, reference.path))
            if reference.canonical in seen:
                continue
            seen.add(reference.canonical)
            normalized.append(
                reference.canonical + (f" {explanation}" if explanation else "")
            )
    return normalized, recognized


def _evidence_path(
    root: Path,
    phase: Phase,
    value: str,
    *,
    evidence_paths: frozenset[str] | None = None,
) -> bool:
    token = value.strip().split(maxsplit=1)[0]
    match = _EVIDENCE_REFERENCE.fullmatch(token)
    if match is None:
        return False
    line = match.group("line")
    if line and "-" in line:
        first, last = map(int, line.split("-", 1))
        if last < first:
            return False
    relative = match.group("path").replace("\\", "/").removeprefix("./")
    if evidence_paths is not None and relative not in evidence_paths:
        return False
    try:
        path = safe_project_path(root, relative, must_exist=True)
    except CwError:
        return False
    if not path.is_file() or path.is_symlink():
        return False
    patterns = (*phase.artifacts, *phase.review_paths)
    for pattern in patterns:
        normalized = pattern.replace("\\", "/").removeprefix("./")
        if relative == normalized or fnmatch.fnmatchcase(relative, normalized):
            return True
        if normalized.endswith("/") and relative.startswith(normalized):
            return True
        if "/**/" in normalized:
            prefix = normalized.split("/**/", 1)[0].rstrip("/")
            if relative.startswith(f"{prefix}/"):
                return True
    return False


def _reported_reference(reference: str) -> str:
    bounded = reference[:_MAX_REPORTED_REFERENCE]
    return bounded + ("..." if len(reference) > _MAX_REPORTED_REFERENCE else "")


def _normalize_and_validate_evidence(
    root: Path,
    phase: Phase,
    evidence: list[str],
    *,
    evidence_paths: frozenset[str] | None,
) -> tuple[list[str], list[str]]:
    normalized, references = normalize_evidence_references(
        evidence, evidence_paths=evidence_paths,
    )
    missing_references = [
        value.strip()
        for value in evidence
        if not normalize_evidence_references(
            [value], evidence_paths=evidence_paths,
        )[1]
    ]
    invalid = [
        canonical
        for canonical, _path in references
        if not _evidence_path(
            root,
            phase,
            canonical,
            evidence_paths=evidence_paths,
        )
    ]
    invalid.extend(missing_references)
    return normalized, invalid


def validate_reviewer_result(
    phase: Phase,
    payload: dict[str, Any],
    *,
    require_blocking_criteria: bool = False,
    strict: bool = False,
    root: Path | None = None,
    evidence_paths: tuple[str, ...] | None = None,
) -> tuple[ReviewDecision, list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    allowed = {"decision", "summary", "blocking_issues", "criteria", "blocking_criteria"}
    if strict and set(payload) != allowed:
        raise CwError("Reviewer result schema is invalid", ErrorCode.SCHEMA_VALIDATION_ERROR, "Run: cw retry")
    try:
        decision = ReviewDecision(str(payload["decision"]))
        results = payload["criteria"]
        issues = payload.get("blocking_issues", [])
    except (KeyError, ValueError, TypeError) as exc:
        raise CwError("Reviewer result schema is invalid", ErrorCode.SCHEMA_VALIDATION_ERROR, "Run: cw retry") from exc
    summary = payload.get("summary")
    if (strict or "summary" in payload) and (not isinstance(summary, str) or not summary.strip()):
        raise CwError("Reviewer result schema is invalid", ErrorCode.SCHEMA_VALIDATION_ERROR, "Run: cw retry")
    if (
        not isinstance(results, list)
        or not isinstance(issues, list)
        or not all(isinstance(v, str) and v.strip() for v in issues)
    ):
        raise CwError("Reviewer result schema is invalid", ErrorCode.SCHEMA_VALIDATION_ERROR, "Run: cw retry")
    expected = {criterion.id: criterion for criterion in phase.acceptance_criteria}
    received: dict[str, dict[str, Any]] = {}
    consistency: list[str] = []
    for result in results:
        result_fields = set(result) if isinstance(result, dict) else set()
        if (
            not isinstance(result, dict)
            or (
                result_fields != {"id", "status", "evidence"}
                and (strict or result_fields != {"id", "status", "evidence", "severity"})
            )
            or not isinstance(result.get("id"), str)
        ):
            consistency.append("Malformed criterion result")
            continue
        criterion_id = result["id"]
        if criterion_id in received:
            consistency.append(f"Duplicate criterion: {criterion_id}")
        elif criterion_id not in expected:
            consistency.append(f"Invented criterion: {criterion_id}")
        else:
            received[criterion_id] = dict(result)
            configured_severity = expected[criterion_id].severity.value
            if "severity" in result and result["severity"] != configured_severity:
                consistency.append(f"Criterion severity mismatch: {criterion_id}")
    for criterion_id in expected:
        if criterion_id not in received:
            consistency.append(f"Missing criterion: {criterion_id}")
    valid_status = {"PASS", "FAIL", "UNKNOWN"}
    validate_paths = strict or "blocking_criteria" in payload
    normalize_paths = strict or evidence_paths is not None
    bundled_paths = (
        frozenset(
            path.replace("\\", "/").removeprefix("./")
            for path in evidence_paths
        )
        if evidence_paths is not None
        else None
    )
    for criterion_id, result in received.items():
        evidence = result.get("evidence")
        if (
            result.get("status") not in valid_status
            or not isinstance(evidence, list)
            or not evidence
            or not all(isinstance(item, str) and item.strip() for item in evidence)
        ):
            consistency.append(f"Insufficient result evidence: {criterion_id}")
        elif validate_paths:
            if root is None:
                consistency.append(f"Evidence is outside review scope: {criterion_id}")
                continue
            if not normalize_paths:
                if not all(
                    _evidence_path(root, phase, item, evidence_paths=bundled_paths)
                    for item in evidence
                ):
                    consistency.append(f"Evidence is outside review scope: {criterion_id}")
                continue
            normalized, invalid = _normalize_and_validate_evidence(
                root, phase, evidence, evidence_paths=bundled_paths,
            )
            result["evidence"] = normalized
            if invalid:
                for reference in dict.fromkeys(invalid):
                    suffix = f": {_reported_reference(reference)}" if reference else ""
                    consistency.append(
                        f"Evidence is outside review scope: {criterion_id}{suffix}"
                    )

    blocking_results = payload.get("blocking_criteria")
    if blocking_results is None and not require_blocking_criteria:
        blocking_results = []
    if not isinstance(blocking_results, list):
        raise CwError("Reviewer blocking criteria are missing", ErrorCode.SCHEMA_VALIDATION_ERROR, "Run: cw retry")
    expected_blocking = set(phase.blocking_criteria)
    received_blocking: dict[str, dict[str, Any]] = {}
    for result in blocking_results:
        if (
            not isinstance(result, dict)
            or set(result) != {"description", "status", "evidence"}
            or not isinstance(result.get("description"), str)
        ):
            consistency.append("Malformed blocking criterion result")
            continue
        description = result["description"]
        if description in received_blocking:
            consistency.append(f"Duplicate blocking criterion: {description}")
        elif description not in expected_blocking:
            consistency.append(f"Invented blocking criterion: {description}")
        else:
            received_blocking[description] = dict(result)
    if require_blocking_criteria or "blocking_criteria" in payload:
        for description in phase.blocking_criteria:
            if description not in received_blocking:
                consistency.append(f"Missing blocking criterion: {description}")
    for description, result in received_blocking.items():
        evidence = result.get("evidence")
        if (
            result.get("status") not in valid_status
            or not isinstance(evidence, list)
            or not evidence
            or not all(isinstance(item, str) and item.strip() for item in evidence)
        ):
            consistency.append(f"Insufficient blocking evidence: {description}")
        elif validate_paths:
            if root is None:
                consistency.append(f"Blocking evidence is outside review scope: {description}")
                continue
            if not normalize_paths:
                if not all(
                    _evidence_path(root, phase, item, evidence_paths=bundled_paths)
                    for item in evidence
                ):
                    consistency.append(
                        f"Blocking evidence is outside review scope: {description}"
                    )
                continue
            normalized, invalid = _normalize_and_validate_evidence(
                root, phase, evidence, evidence_paths=bundled_paths,
            )
            result["evidence"] = normalized
            if invalid:
                for reference in dict.fromkeys(invalid):
                    suffix = f": {_reported_reference(reference)}" if reference else ""
                    consistency.append(
                        f"Blocking evidence is outside review scope: {description}{suffix}"
                    )
    blocking_failed = [
        criterion_id for criterion_id, criterion in expected.items()
        if criterion.severity == CriterionSeverity.BLOCKING
        and received.get(criterion_id, {}).get("status") != "PASS"
    ]
    configured_blockers = [
        description for description in phase.blocking_criteria
        if received_blocking.get(description, {}).get("status") != "PASS"
    ] if require_blocking_criteria or "blocking_criteria" in payload else []
    if consistency or blocking_failed or configured_blockers or issues:
        if decision is ReviewDecision.APPROVE:
            decision = ReviewDecision.REVISE
        issues = [*issues, *blocking_failed, *configured_blockers, *consistency]
    elif decision is ReviewDecision.REVISE:
        issues.append("Reviewer requested revision without a blocking criterion failure")
    return decision, list(received.values()), list(received_blocking.values()), list(dict.fromkeys(issues))
