#!/usr/bin/env python3
"""Validate public-facing version surfaces against repository VERSION."""
from __future__ import annotations

import json
import re
import sys
from datetime import date
from itertools import pairwise
from pathlib import Path
from typing import Any

try:
    from scripts.hero_demo import recording_is_patch_compatible
except ModuleNotFoundError:  # Direct `python scripts/check_public_version.py` execution.
    from hero_demo import recording_is_patch_compatible

ROOT = Path(__file__).resolve().parents[1]
SEMVER_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
CHANGELOG_HEADER_RE = re.compile(
    r"(?m)^## ((?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)) — ([0-9]{4}-[0-9]{2}-[0-9]{2})$"
)
CORE_ASSET_TEMPLATES = {
    "dist/codex_workflow-${release_version}-py3-none-any.whl",
    "dist/codex_workflow-${release_version}.tar.gz",
    "dist/cw-${release_version}-linux-x86_64.tar.gz",
    "dist/cw-release-manifest.json",
}


def _semver(value: object) -> tuple[int, int, int] | None:
    if not isinstance(value, str):
        return None
    match = SEMVER_RE.fullmatch(value)
    if match is None:
        return None
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def _release_metadata_errors(root: Path) -> list[str]:
    errors: list[str] = []
    version = (root / "VERSION").read_text(encoding="utf-8").strip()
    if _semver(version) is None:
        errors.append(f"VERSION `{version}` is not valid SemVer.")

    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    matches = list(CHANGELOG_HEADER_RE.finditer(changelog))
    current = [match for match in matches if match.group(1) == version]
    if len(current) != 1:
        errors.append(
            f"CHANGELOG.md must contain exactly one dated `## {version}` heading."
        )
    else:
        try:
            date.fromisoformat(current[0].group(2))
        except ValueError:
            errors.append(f"CHANGELOG.md has an invalid date for `{version}`.")
    unreleased = re.findall(r"(?m)^## Unreleased\s*$", changelog)
    if len(unreleased) != 1:
        errors.append("CHANGELOG.md must contain exactly one `## Unreleased` heading.")
    else:
        after_unreleased = changelog.split("## Unreleased", 1)[1]
        first_content = next(
            (line for line in after_unreleased.splitlines() if line.strip()), ""
        )
        expected = f"## {version} — "
        if not first_content.startswith(expected):
            errors.append(
                "The current dated release heading must immediately follow empty Unreleased."
            )

    history_path = root / "cw" / "release_history.json"
    try:
        history: Any = json.loads(history_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        errors.append("cw/release_history.json is not valid UTF-8 JSON.")
        history = None
    releases: object = None
    if not isinstance(history, dict) or set(history) != {"schema_version", "releases"}:
        if history is not None:
            errors.append("Release history must be a closed object with schema and releases.")
    else:
        if history.get("schema_version") != 1:
            errors.append("Release history schema_version must be 1.")
        releases = history.get("releases")
    if not isinstance(releases, list) or not releases:
        errors.append("Release history releases must be a non-empty list.")
    else:
        parsed_versions: list[tuple[int, int, int]] = []
        seen: set[str] = set()
        for entry in releases:
            if not isinstance(entry, dict) or set(entry) != {"version", "changes"}:
                errors.append("Every release must contain only version and changes.")
                continue
            entry_version = entry.get("version")
            parsed = _semver(entry_version)
            if parsed is None:
                errors.append(f"Release version `{entry_version}` is not valid SemVer.")
            else:
                parsed_versions.append(parsed)
            if isinstance(entry_version, str):
                if entry_version in seen:
                    errors.append(f"Release version `{entry_version}` is duplicated.")
                seen.add(entry_version)
            changes = entry.get("changes")
            if (
                not isinstance(changes, list)
                or not changes
                or any(not isinstance(change, str) or not change.strip() for change in changes)
            ):
                errors.append("Every release changes list must contain non-empty strings.")
        if releases[0].get("version") != version:
            errors.append("The first release history entry must match VERSION.")
        if any(left <= right for left, right in pairwise(parsed_versions)):
            errors.append("Release history versions must be strictly descending.")

    release_process = (root / "docs" / "release-process.md").read_text(encoding="utf-8")
    for required in (
        'release_version="$(cat VERSION)"',
        'git tag -a "v${release_version}" -m "CW CLI v${release_version}"',
        'git push origin "v${release_version}"',
    ):
        if required not in release_process:
            errors.append("Release documentation must derive tag identity from VERSION.")
            break
    if re.search(r"git (?:tag|push origin) [^\n]*v[0-9]+\.[0-9]+\.[0-9]+", release_process):
        errors.append("Release documentation must not hardcode a historical release tag.")
    if "Core releases do not build or\nattach the public Plugin" not in release_process:
        errors.append("Release documentation must preserve the version-neutral Core-only policy.")

    workflow = (root / ".github" / "workflows" / "release-check.yml").read_text(
        encoding="utf-8"
    )
    asset_block = re.search(r"(?ms)^\s*assets=\(\n(?P<body>.*?)^\s*\)", workflow)
    assets = (
        set(re.findall(r'^\s*"([^"]+)"\s*$', asset_block.group("body"), re.MULTILINE))
        if asset_block is not None
        else set()
    )
    if assets != CORE_ASSET_TEMPLATES:
        errors.append("Release workflow must publish exactly the four Core assets.")
    if (
        "python scripts/build_release.py --output dist --channel stable --component core"
        not in workflow
        or "python scripts/validate_release_assets.py --directory dist --component core"
        not in workflow
        or "build_plugin_candidate.py" in workflow
        or "cw-plugin-" in workflow
    ):
        errors.append("Release workflow must retain the exact Core-only profile.")
    if "python -m pip install . tiktoken==0.14.0" not in workflow:
        errors.append("Release workflow must install the pinned benchmark tokenizer.")
    return errors


def _errors(root: Path = ROOT) -> list[str]:
    errors = _release_metadata_errors(root)
    version = (root / "VERSION").read_text(encoding="utf-8").strip()

    readme = (root / "README.md").read_text(encoding="utf-8")
    if "img.shields.io/github/v/release/Queopius/cw?display_name=tag&sort=semver" not in readme:
        errors.append("README missing dynamic GitHub latest-release badge URL.")
    docs_index = (root / "docs" / "index.md").read_text(encoding="utf-8")
    if "img.shields.io/github/v/release/Queopius/cw?display_name=tag&sort=semver" not in docs_index:
        errors.append("docs/index.md missing dynamic GitHub latest-release badge URL.")

    release_workflow = (root / ".github" / "workflows" / "release-check.yml").read_text(
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

    versioning = (root / "docs" / "versioning.md").read_text(encoding="utf-8")
    core_match = re.search(r"(?m)^- \*\*CW Core / CLI\*\*: `([^`]+)`\s*$", versioning)
    if not core_match:
        errors.append("docs/versioning.md missing `CW Core / CLI` current-version line.")
    elif core_match.group(1) != version:
        errors.append(
            f"docs/versioning.md has CW Core / CLI `{core_match.group(1)}`, expected `{version}`."
        )

    hero = root / "demo" / "hero" / "hero-demo.json"
    artifact = json.loads(hero.read_text(encoding="utf-8"))
    hero_version = artifact.get("cw_version")
    if not isinstance(hero_version, str):
        errors.append("demo/hero/hero-demo.json missing string `cw_version`.")
    elif not recording_is_patch_compatible(hero_version, version):
        errors.append(
            "demo/hero/hero-demo.json is not a validated recording from the current "
            f"or immediately preceding minor line (`{hero_version}` vs `{version}`)."
        )

    return errors


def main() -> int:
    issues = _errors()
    if issues:
        for issue in issues:
            print(f"ERROR: {issue}", file=sys.stderr)
        return 1
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    print(f"Public version surfaces match VERSION={version}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
