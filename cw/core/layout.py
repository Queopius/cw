from __future__ import annotations

from pathlib import Path

from .errors import CwError, ErrorCode


MUTABLE_DIRECTORIES = (
    "runtime", "reviews", "gates", "completion", "validation", "logs", "locks", "backups",
    "plan-revisions", "plan-proposals", "supersessions",
)
STATIC_DIRECTORIES = ("hooks", "schemas", "prompts", "workflow")
CRITICAL_FILES = (
    ".cw/project.json",
    ".cw/state.json",
    ".cw/config.toml",
    ".cw/runtime/implementer-session.json",
    ".cw/runtime/active-run.json",
    ".cw/runtime/READY_FOR_REVIEW.json",
    ".cw/runtime/plan-rebaseline-transaction.json",
    ".cw/runtime/plan-amend-transaction.json",
    ".codex/hooks.json",
    ".codex/hooks/phase_gate.py",
    ".codex/workflow/phases.yaml",
    "AGENTS.md",
)


def safe_directory(path: Path, label: str, *, create: bool = False) -> Path:
    if path.is_symlink():
        raise CwError(f"{label} cannot be a symlink", ErrorCode.SCHEMA_VALIDATION_ERROR)
    if path.exists() and not path.is_dir():
        raise CwError(f"{label} must be a directory", ErrorCode.SCHEMA_VALIDATION_ERROR)
    if create and not path.exists():
        path.mkdir(parents=False)
    return path


def safe_file(path: Path, label: str, *, required: bool = False) -> Path:
    if path.is_symlink():
        raise CwError(f"{label} cannot be a symlink", ErrorCode.SCHEMA_VALIDATION_ERROR)
    if path.exists() and not path.is_file():
        raise CwError(f"{label} must be a regular file", ErrorCode.SCHEMA_VALIDATION_ERROR)
    if required and not path.is_file():
        raise CwError(f"{label} is missing", ErrorCode.SCHEMA_VALIDATION_ERROR)
    return path


def validate_project_layout(root: Path, *, create: bool = False) -> None:
    runtime = root / ".cw"
    static = root / ".codex"
    safe_directory(runtime, ".cw", create=create)
    if runtime.exists():
        for name in MUTABLE_DIRECTORIES:
            safe_directory(runtime / name, f".cw/{name}", create=create)
    safe_directory(static, ".codex", create=create)
    if static.exists():
        for name in STATIC_DIRECTORIES:
            safe_directory(static / name, f".codex/{name}", create=create)
    for relative in CRITICAL_FILES:
        safe_file(root / relative, relative)


def validate_tree(path: Path, label: str) -> None:
    safe_directory(path, label)
    for entry in path.rglob("*"):
        relative = entry.relative_to(path).as_posix()
        if entry.is_symlink():
            raise CwError(f"{label} contains a symlink: {relative}", ErrorCode.SCHEMA_VALIDATION_ERROR)
        if not entry.is_dir() and not entry.is_file():
            raise CwError(f"{label} contains a special file: {relative}", ErrorCode.SCHEMA_VALIDATION_ERROR)
