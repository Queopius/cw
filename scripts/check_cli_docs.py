#!/usr/bin/env python3
"""Validate the public CLI documentation against CW's argparse surface."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "docs" / "cli-reference.snapshot.json"
REFERENCE = ROOT / "docs" / "cli-reference.md"
COMMON_OPTIONS = {"--json", "--verbose", "--quiet", "--no-color"}


def _source_parser() -> argparse.ArgumentParser:
    sys.path.insert(0, str(ROOT))
    from cw.cli.parser import build_parser

    return build_parser()


def public_surface() -> dict[str, Any]:
    parser = _source_parser()
    subparsers = next(
        action for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    commands: list[dict[str, Any]] = []
    for name, command_parser in subparsers.choices.items():
        options: list[str] = []
        positionals: list[dict[str, Any]] = []
        for action in command_parser._actions:
            if action.help is argparse.SUPPRESS or action.dest == "help":
                continue
            long_options = sorted(value for value in action.option_strings if value.startswith("--"))
            if long_options:
                options.extend(long_options)
                continue
            if action.option_strings:
                continue
            positionals.append({
                "name": action.dest,
                "choices": list(action.choices or ()),
                "required": action.nargs not in ("?", "*"),
            })
        commands.append({
            "name": name,
            "options": sorted(options),
            "positionals": positionals,
        })
    return {"schema_version": 1, "commands": commands}


def _reference_sections(text: str) -> dict[str, str]:
    matches = list(re.finditer(r"^## (cw(?: [a-z][a-z-]*)?)\s*$", text, re.MULTILINE))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections[match.group(1)] = text[match.start():end]
    return sections


def validate(snapshot_path: Path = SNAPSHOT, reference_path: Path = REFERENCE) -> list[str]:
    errors: list[str] = []
    expected = public_surface()
    try:
        stored = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"CLI snapshot cannot be read: {exc}"]
    if stored != expected:
        errors.append(
            "CLI parser surface changed; run `python scripts/check_cli_docs.py --write` "
            "and update docs/cli-reference.md."
        )

    try:
        reference = reference_path.read_text(encoding="utf-8")
    except OSError as exc:
        return [*errors, f"CLI reference cannot be read: {exc}"]
    sections = _reference_sections(reference)
    if "cw" not in sections:
        errors.append("CLI reference is missing the `cw` section.")
    for command in expected["commands"]:
        heading = f"cw {command['name']}"
        section = sections.get(heading)
        if section is None:
            errors.append(f"CLI reference is missing the `{heading}` section.")
            continue
        for option in set(command["options"]) - COMMON_OPTIONS:
            if f"`{option}" not in section:
                errors.append(f"CLI reference does not mention `{heading} {option}`.")
    for option in COMMON_OPTIONS:
        if f"`{option}" not in reference:
            errors.append(f"CLI reference does not document common option `{option}`.")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="refresh the parser snapshot")
    args = parser.parse_args()
    if args.write:
        SNAPSHOT.write_text(
            json.dumps(public_surface(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"Updated {SNAPSHOT.relative_to(ROOT)}")
        return 0
    errors = validate()
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("CLI documentation matches the public parser surface.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
