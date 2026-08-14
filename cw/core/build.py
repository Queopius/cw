from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


def runtime_root() -> Path:
    return Path(__file__).resolve().parents[2]


def git_build(root: Path) -> str | None:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True, encoding="utf-8", errors="replace",
        capture_output=True, timeout=5, check=False,
    )
    value = completed.stdout.strip()
    if completed.returncode != 0 or not value:
        return None
    dirty = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=normal"], cwd=root,
        text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=5, check=False,
    )
    return f"{value}-dirty" if dirty.returncode == 0 and dirty.stdout.strip() else value


def build_metadata(root: Path | None = None) -> dict[str, Any]:
    root = (root or runtime_root()).resolve()
    path = root / "BUILD.json"
    if path.is_file() and not path.is_symlink():
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value, dict) and isinstance(value.get("commit"), str):
                return value
        except (OSError, json.JSONDecodeError):
            pass
    return {"commit": git_build(root) or "unknown", "source": "development"}


def source_build(cwd: Path | None = None) -> str | None:
    root = (cwd or Path.cwd()).resolve()
    if not (root / ".git").exists() or not (root / "pyproject.toml").is_file() or not (root / "cw").is_dir():
        return None
    return git_build(root)


def version_diagnostics(cwd: Path | None = None) -> dict[str, Any]:
    runtime = runtime_root()
    metadata = build_metadata(runtime)
    source = source_build(cwd)
    executable = shutil.which("cw") or sys.argv[0]
    installed = str(metadata.get("commit", "unknown"))
    return {
        "executable": str(Path(executable).expanduser().resolve(strict=False)),
        "runtime": str(runtime),
        "build": installed,
        "build_source": str(metadata.get("source", "unknown")),
        "source_build": source,
        "source_match": None if source is None or installed == "unknown" else source == installed,
    }
