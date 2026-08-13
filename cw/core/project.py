from __future__ import annotations

import hashlib
import re
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

from cw import __version__
from .errors import CwError, ErrorCode
from .layout import safe_file
from .schema import SCHEMA_VERSION, schema_version
from .utils import atomic_json, load_json, utc_now


@dataclass(frozen=True, slots=True)
class Project:
    root: Path
    project_id: str
    fingerprint: str

    @property
    def codex_dir(self) -> Path:
        return self.root / ".codex"

    @property
    def cw_dir(self) -> Path:
        return self.root / ".cw"


def repository_root(cwd: Path | None = None) -> Path:
    result = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd, text=True, capture_output=True, check=False)
    if result.returncode or not result.stdout.strip():
        raise CwError("Current directory is not inside a Git repository.", ErrorCode.USAGE_ERROR, "Run CW from a Git repository.", exit_code=2)
    return Path(result.stdout.strip()).resolve(strict=True)


def project_id(root: Path) -> str:
    value = re.sub(r"[^a-z0-9._-]+", "-", root.name.lower()).strip("-.")
    return value or "project"


def repository_fingerprint(root: Path) -> str:
    # A non-secret repository-local UUID survives ordinary directory moves while preventing two
    # unrelated repositories with the same basename from accepting each other's CW metadata.
    identity = subprocess.run(["git", "config", "--local", "--get", "cw.repository-id"], cwd=root, text=True, capture_output=True, check=False).stdout.strip()
    if not identity:
        identity = str(uuid.uuid4())
        completed = subprocess.run(["git", "config", "--local", "cw.repository-id", identity], cwd=root, text=True, capture_output=True, check=False)
        if completed.returncode:
            raise CwError("Unable to store repository identity", ErrorCode.RUNTIME_NOT_WRITABLE, details=completed.stderr.strip())
    return hashlib.sha256(f"cw-repository\0{identity}".encode()).hexdigest()


def create_identity(root: Path) -> Project:
    project = Project(root, project_id(root), repository_fingerprint(root))
    atomic_json(root / ".cw" / "project.json", {
        "schema_version": SCHEMA_VERSION, "project_id": project.project_id,
        "repository_root_fingerprint": project.fingerprint,
        "initialized_at": utc_now(),
        "created_with_cw_version": __version__,
        "cw_version": __version__,
    })
    return project


def stamp_project_metadata(data: dict[str, object]) -> dict[str, object]:
    """Stamp CW-owned mutable metadata without losing its origin version.

    ``created_with_cw_version`` is historical. ``cw_version`` identifies the
    last CW writer/migrator of the current document representation.
    """
    stamped = dict(data)
    original = stamped.get("created_with_cw_version") or stamped.get("cw_version")
    if isinstance(original, str) and original:
        stamped["created_with_cw_version"] = original
    else:
        stamped["created_with_cw_version"] = __version__
    stamped["cw_version"] = __version__
    return stamped


def load_project(root: Path, *, allow_moved: bool = True) -> Project:
    path = root / ".cw" / "project.json"
    safe_file(path, ".cw/project.json")
    if not path.is_file():
        raise CwError("CW is not initialized in this repository.", ErrorCode.INVALID_STATE, "Run: cw init")
    data = load_json(path)
    schema_version(data, "Project identity")
    configured = str(data.get("project_id", ""))
    current = project_id(root)
    if configured != current:
        raise CwError(
            "Project workflow mismatch", ErrorCode.WORKFLOW_PROJECT_MISMATCH,
            "Run: cw repair", details=f"Workflow: {configured or 'unknown'}\nRepository: {current}",
        )
    current_fingerprint = repository_fingerprint(root)
    saved = str(data.get("repository_root_fingerprint", ""))
    if saved and saved != current_fingerprint:
        raise CwError("Repository identity changed", ErrorCode.WORKFLOW_PROJECT_MISMATCH, "Run: cw repair", details="The workflow fingerprint does not match this Git repository.")
    return Project(root, current, current_fingerprint)
