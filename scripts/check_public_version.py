#!/usr/bin/env python3
"""Validate public-facing version surfaces against repository VERSION."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

try:
    from scripts.hero_demo import recording_is_patch_compatible
except ModuleNotFoundError:  # Direct `python scripts/check_public_version.py` execution.
    from hero_demo import recording_is_patch_compatible

ROOT = Path(__file__).resolve().parents[1]
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()


def _errors() -> list[str]:
    errors: list[str] = []

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    if "img.shields.io/github/v/release/Queopius/cw?display_name=tag&sort=semver" not in readme:
        errors.append("README missing dynamic GitHub latest-release badge URL.")
    docs_index = (ROOT / "docs" / "index.md").read_text(encoding="utf-8")
    if "img.shields.io/github/v/release/Queopius/cw?display_name=tag&sort=semver" not in docs_index:
        errors.append("docs/index.md missing dynamic GitHub latest-release badge URL.")

    release_workflow = (ROOT / ".github" / "workflows" / "release-check.yml").read_text(
        encoding="utf-8"
    )
    if (
        "gh release create" not in release_workflow
        or "--existing-release-json" not in release_workflow
    ):
        errors.append("Release workflow does not publish an idempotent GitHub Release.")
    if "python scripts/build_release.py --output dist --channel stable --component core" not in release_workflow:
        errors.append("Release workflow does not use the explicit Core-only release profile.")
    if "python scripts/validate_release_assets.py --directory dist --component core" not in release_workflow:
        errors.append("Release workflow does not validate the exact Core-only asset allowlist.")
    if "build_plugin_candidate.py" in release_workflow or "cw-plugin-" in release_workflow:
        errors.append("Core release workflow must not build or publish a Plugin asset.")
    if "origin/prod" not in release_workflow or "origin/release" in release_workflow:
        errors.append("Public release tags must be verified against origin/prod.")
    if "dist/*" in release_workflow or "--clobber" in release_workflow:
        errors.append("Release publication must use the verified exact asset allowlist without replacement.")

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
    elif not recording_is_patch_compatible(hero_version, VERSION):
        errors.append(
            "demo/hero/hero-demo.json is not a validated recording from the current "
            f"or immediately preceding minor line (`{hero_version}` vs `{VERSION}`)."
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
