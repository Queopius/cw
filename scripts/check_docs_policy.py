#!/usr/bin/env python3
"""Enforce CW's public documentation source-link policy without YAML dependencies."""

from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "mkdocs.yml"
EXPECTED = {
    "edit_uri": '""',
    "repo_url": "https://github.com/Queopius/cw",
    "repo_name": "Queopius/cw",
}


def _configuration_lines(path: Path = CONFIG) -> list[str]:
    return [line.split("#", 1)[0].rstrip() for line in path.read_text(encoding="utf-8").splitlines()]


def documentation_policy_errors(path: Path = CONFIG) -> list[str]:
    lines = _configuration_lines(path)
    errors: list[str] = []
    if any(re.fullmatch(r"\s*-\s*content\.action\.edit\s*", line) for line in lines):
        errors.append("content.action.edit must remain disabled")
    for key, expected in EXPECTED.items():
        matches = [
            match.group(1).strip()
            for line in lines
            if (match := re.fullmatch(rf"{re.escape(key)}:\s*(.*?)\s*", line))
        ]
        if matches != [expected]:
            errors.append(f"{key} must be configured exactly as {expected}")
    return errors


def main() -> int:
    errors = documentation_policy_errors()
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("Documentation edit actions are disabled and the GitHub source link is preserved.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
