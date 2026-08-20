from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from cw.core.errors import CwError, ErrorCode
from cw.core.project import Project, load_project, repository_root


@dataclass(frozen=True, slots=True)
class ProjectHandle:
    project_id: str
    repository_id: str
    display_name: str

    def to_dict(self) -> dict[str, str]:
        return {
            "project_id": self.project_id,
            "repository_id": self.repository_id,
            "display_name": self.display_name,
        }


@dataclass(frozen=True, slots=True)
class ResolvedProject:
    root: Path
    project: Project
    handle: ProjectHandle


def _within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


class ProjectResolver:
    """Resolve explicit repositories without allowing callers to escape trusted roots."""

    def __init__(self, allowed_roots: tuple[Path, ...] | list[Path]) -> None:
        if not allowed_roots:
            raise CwError("At least one authorized project root is required", ErrorCode.PROJECT_SCOPE_VIOLATION)
        self.allowed_roots = tuple(Path(item).resolve(strict=True) for item in allowed_roots)
        self._handles: dict[str, Path] = {}

    def open(self, requested: Path | str) -> ResolvedProject:
        try:
            candidate = Path(requested).resolve(strict=True)
        except OSError as exc:
            raise CwError(
                "Project path does not exist",
                ErrorCode.PROJECT_SCOPE_VIOLATION,
            ) from exc
        if not any(_within(candidate, allowed) for allowed in self.allowed_roots):
            raise CwError("Project path is outside the authorized root", ErrorCode.PROJECT_SCOPE_VIOLATION)
        root = repository_root(candidate)
        if not any(_within(root, allowed) for allowed in self.allowed_roots):
            raise CwError("Repository resolves outside the authorized root", ErrorCode.PROJECT_SCOPE_VIOLATION)
        project = load_project(root)
        handle = ProjectHandle(
            project_id=project.project_id,
            repository_id=project.fingerprint[:20],
            display_name=root.name,
        )
        self._handles[handle.repository_id] = root
        return ResolvedProject(root, project, handle)

    def open_handle(self, repository_id: str) -> ResolvedProject:
        root = self._handles.get(repository_id)
        if root is None:
            raise CwError("Unknown project handle", ErrorCode.PROJECT_SCOPE_VIOLATION)
        return self.open(root)
