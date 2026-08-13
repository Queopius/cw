"""A deliberately tiny HTTP-like router for the CW hero demo."""
from __future__ import annotations


def route(method: str, path: str) -> tuple[int, dict[str, str]]:
    """Return a status code and JSON-compatible response body."""

    return 404, {"error": "not found"}
