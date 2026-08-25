#!/usr/bin/env python3
"""Validate the documented information architecture and its URL inventory."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "mkdocs.yml"
DOCS = ROOT / "docs"
SNAPSHOT = ROOT / "tests/fixtures/docs-navigation.snapshot.json"
TOP_LEVEL = (
    "Home",
    "Start Here",
    "Using CW",
    "Workflow & Governance",
    "Operations & Recovery",
    "Platform",
    "Engineering Reference",
    "Releases",
    "Project",
)
REQUIRED_FEATURES = {
    "navigation.indexes",
    "navigation.path",
    "navigation.prune",
    "navigation.top",
    "navigation.tracking",
}
MAX_DEPTH = 3
MAX_CHILDREN = 8


def _load_configuration(path: Path = CONFIG) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TypeError("mkdocs.yml must contain a mapping")
    return data


def _entry(item: object) -> tuple[str, str | list[object]]:
    if not isinstance(item, dict) or len(item) != 1:
        raise ValueError("each nav entry must be a single-key mapping")
    label, target = next(iter(item.items()))
    if not isinstance(label, str) or not isinstance(target, (str, list)):
        raise TypeError("nav labels must map to a path or child list")
    return label, target


def _walk(
    items: list[object], depth: int = 1, parent: str = "nav"
) -> Iterator[tuple[str, str | None, int, str]]:
    for item in items:
        label, target = _entry(item)
        if isinstance(target, str):
            yield label, target, depth, parent
        else:
            yield label, None, depth, parent
            yield from _walk(target, depth + 1, label)


def _is_section_index(label: str, children: list[object]) -> bool:
    if not children:
        return False
    child_label, child_target = _entry(children[0])
    return child_label == label and isinstance(child_target, str)


def _structure_errors(items: list[object], depth: int = 1) -> list[str]:
    errors: list[str] = []
    labels: list[str] = []
    for item in items:
        try:
            label, target = _entry(item)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        labels.append(label)
        if depth > MAX_DEPTH:
            errors.append(f"navigation depth exceeds {MAX_DEPTH}: {label}")
        if isinstance(target, list):
            effective_children = len(target) - int(_is_section_index(label, target))
            if effective_children > MAX_CHILDREN:
                errors.append(
                    f"{label} has {effective_children} visible children; maximum is {MAX_CHILDREN}"
                )
            errors.extend(_structure_errors(target, depth + 1))
    duplicates = sorted({label for label in labels if labels.count(label) > 1})
    if duplicates:
        errors.append(f"ambiguous sibling labels: {', '.join(duplicates)}")
    return errors


def snapshot_payload(path: Path = CONFIG) -> dict[str, object]:
    configuration = _load_configuration(path)
    nav = configuration.get("nav")
    if not isinstance(nav, list):
        raise TypeError("mkdocs.yml nav must be a list")
    documents = sorted(
        str(candidate.relative_to(DOCS))
        for candidate in DOCS.rglob("*.md")
        if candidate.is_file()
    )
    paths = sorted(
        target
        for _, target, _, _ in _walk(nav)
        if target is not None
    )
    return {
        "schema_version": 1,
        "top_level": [label for item in nav for label in [_entry(item)[0]]],
        "documents": documents,
        "navigation_paths": paths,
        "nav": nav,
    }


def navigation_errors(
    config_path: Path = CONFIG, snapshot_path: Path = SNAPSHOT
) -> list[str]:
    errors: list[str] = []
    try:
        configuration = _load_configuration(config_path)
        nav = configuration.get("nav")
        if not isinstance(nav, list):
            return ["mkdocs.yml nav must be a list"]
        walked = list(_walk(nav))
    except (OSError, TypeError, ValueError, yaml.YAMLError) as exc:
        return [f"navigation cannot be read: {exc}"]

    top_level = tuple(_entry(item)[0] for item in nav)
    if top_level != TOP_LEVEL:
        errors.append("top-level navigation order does not match the canonical architecture")
    if len(top_level) - 1 not in {7, 8}:
        errors.append("navigation must contain seven or eight main groups besides Home")

    errors.extend(_structure_errors(nav))
    paths = [target for _, target, _, _ in walked if target is not None]
    duplicate_paths = sorted({path for path in paths if paths.count(path) > 1})
    if duplicate_paths:
        errors.append(f"duplicate navigation paths: {', '.join(duplicate_paths)}")

    documents = sorted(
        str(candidate.relative_to(DOCS))
        for candidate in DOCS.rglob("*.md")
        if candidate.is_file()
    )
    missing = sorted(set(documents) - set(paths))
    unknown = sorted(set(paths) - set(documents))
    if missing:
        errors.append(f"orphan Markdown documents: {', '.join(missing)}")
    if unknown:
        errors.append(f"navigation targets missing documents: {', '.join(unknown)}")

    features = set(configuration.get("theme", {}).get("features", []))
    absent_features = sorted(REQUIRED_FEATURES - features)
    if absent_features:
        errors.append(f"required Material features missing: {', '.join(absent_features)}")
    if "navigation.expand" in features:
        errors.append("navigation.expand must remain disabled")
    if "navigation.sections" in features:
        errors.append("navigation.sections must remain disabled so inactive groups collapse")

    try:
        stored = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"navigation snapshot cannot be read: {exc}")
    else:
        current = snapshot_payload(config_path)
        if stored != current:
            errors.append(
                "documentation navigation changed; run "
                "`python3 scripts/check_docs_navigation.py --write` after review"
            )
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="refresh the reviewed snapshot")
    args = parser.parse_args()
    if args.write:
        SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT.write_text(
            json.dumps(snapshot_payload(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"Wrote {SNAPSHOT.relative_to(ROOT)}")
        return 0

    errors = navigation_errors()
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("Documentation navigation architecture and URL inventory are valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
