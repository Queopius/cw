#!/usr/bin/env python3
"""Validate public-facing version surfaces against repository VERSION."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()


def _errors() -> list[str]:
    errors: list[str] = []

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    if "img.shields.io/github/v/release/Queopius/cw?display_name=tag&sort=semver" not in readme:
        errors.append("README missing dynamic GitHub latest-release badge URL.")

    versioning = (ROOT / "docs" / "versioning.md").read_text(encoding="utf-8")
    core_match = re.search(r"(?m)^- \*\*CW Core / CLI\*\*: `([^`]+)`\s*$", versioning)
    if not core_match:
        errors.append("docs/versioning.md missing `CW Core / CLI` current-version line.")
    elif core_match.group(1) != VERSION:
        errors.append(
            f"docs/versioning.md has CW Core / CLI `{core_match.group(1)}`, expected `{VERSION}`."
        )

    hero = ROOT / "demo" / "hero" / "hero-demo.json"
    artifact = json.loads(hero.read_text(encoding="utf-8"))
    hero_version = artifact.get("cw_version")
    if not isinstance(hero_version, str):
        errors.append("demo/hero/hero-demo.json missing string `cw_version`.")
    elif hero_version != VERSION:
        errors.append(
            f"demo/hero/hero-demo.json has cw_version `{hero_version}`, expected `{VERSION}`."
        )

    return errors


def main() -> int:
    issues = _errors()
    if issues:
        for issue in issues:
            print(f"ERROR: {issue}", file=sys.stderr)
        return 1
    print(f"Public version surfaces match VERSION={VERSION}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
