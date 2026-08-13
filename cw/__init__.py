"""Codex Workflow."""

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


def _version() -> str:
    source = Path(__file__).resolve().parent.parent / "VERSION"
    if source.is_file():
        return source.read_text(encoding="utf-8").strip()
    try:
        return version("codex-workflow")
    except PackageNotFoundError:
        return "0.0.0+unknown"


__version__ = _version()
