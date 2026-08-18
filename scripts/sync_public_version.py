#!/usr/bin/env python3
"""Synchronize public version surfaces with repository VERSION."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = ROOT / "VERSION"
DOCS_VERSIONING = ROOT / "docs" / "versioning.md"
HERO_ARTIFACT = ROOT / "demo" / "hero" / "hero-demo.json"


def _load_version() -> str:
    return VERSION_FILE.read_text(encoding="utf-8").strip()


def _sync_versioning(target_version: str) -> bool:
    text = DOCS_VERSIONING.read_text(encoding="utf-8")
    pattern = re.compile(r"(?m)^- \*\*CW Core / CLI\*\*: `[^`]+`\s*$")
    if not pattern.search(text):
        raise RuntimeError("docs/versioning.md does not contain a CW Core / CLI version line.")
    replacement = f"- **CW Core / CLI**: `{target_version}`"
    next_text = pattern.sub(replacement, text, count=1)
    if next_text == text:
        return False
    DOCS_VERSIONING.write_text(next_text, encoding="utf-8")
    return True


def _sync_hero(target_version: str) -> bool:
    artifact = json.loads(HERO_ARTIFACT.read_text(encoding="utf-8"))
    current = artifact.get("cw_version")
    if current == target_version:
        return False
    if not isinstance(current, str):
        raise RuntimeError("demo/hero/hero-demo.json missing string cw_version.")
    artifact["cw_version"] = target_version
    HERO_ARTIFACT.write_text(
        json.dumps(artifact, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="return error if any public surface is out of sync")
    parser.add_argument("--no-hero", action="store_true", help="skip demo hero artifact update")
    args = parser.parse_args()

    version = _load_version()
    changed = False
    changed_docs = _sync_versioning(version)
    changed |= changed_docs

    changed_hero = False
    if not args.no_hero:
        changed_hero = _sync_hero(version)
        changed |= changed_hero

    if args.check:
        if changed_docs or changed_hero:
            print(f"ERROR: public version surfaces out of sync with VERSION={version}.")
            return 1
        print(f"Public version surfaces already synchronized for VERSION={version}.")
        return 0

    print(f"Public version targets synchronized to {version}:")
    print(f"- docs/versioning.md: {'updated' if changed_docs else 'already synced'}")
    if not args.no_hero:
        print(f"- demo/hero/hero-demo.json: {'updated' if changed_hero else 'already synced'}")
    else:
        print("- demo/hero/hero-demo.json: skipped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
