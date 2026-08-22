#!/usr/bin/env python3
"""Validate the immutable public Plugin against the candidate Core runtime."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
import sys
import zipfile
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cw import __version__
from cw.adapters.mcp.compatibility import ensure_plugin_compatible
from cw.adapters.mcp.runtime import TOOLS


def validate(archive: Path, expected_sha256: str, expected_size: int) -> dict[str, object]:
    before = archive.read_bytes()
    digest = hashlib.sha256(before).hexdigest()
    if digest != expected_sha256 or len(before) != expected_size:
        raise RuntimeError("public Plugin asset identity does not match the approved release")
    names: list[str] = []
    folded: set[str] = set()
    with zipfile.ZipFile(archive) as bundle:
        for item in bundle.infolist():
            pure = PurePosixPath(item.filename)
            if (
                pure.is_absolute()
                or ".." in pure.parts
                or not pure.parts
                or item.filename in names
                or item.filename.casefold() in folded
                or stat.S_ISLNK(item.external_attr >> 16)
            ):
                raise RuntimeError("public Plugin asset contains an unsafe entry")
            names.append(item.filename)
            folded.add(item.filename.casefold())
        required = {
            "cw/VERSION", "cw/.codex-plugin/plugin.json", "cw/.mcp.json",
            "cw/capabilities.json", "cw/skills/cw-workflow/SKILL.md",
        }
        if not required.issubset(names):
            raise RuntimeError("public Plugin asset is incomplete")
        plugin_version = bundle.read("cw/VERSION").decode("utf-8").strip()
        manifest = json.loads(bundle.read("cw/.codex-plugin/plugin.json"))
        mcp = json.loads(bundle.read("cw/.mcp.json"))
    if plugin_version != "0.1.0" or manifest.get("version") != "0.1.0":
        raise RuntimeError("public Plugin version identity changed")
    if not isinstance(mcp, dict) or not mcp:
        raise RuntimeError("public Plugin MCP configuration is invalid")
    policy = ensure_plugin_compatible(core_version=__version__, plugin_version=plugin_version)
    if len(TOOLS) != 12:
        raise RuntimeError("local MCP tool count changed")
    for tool in TOOLS:
        if tool.input_schema().get("additionalProperties") is not False:
            raise RuntimeError(f"{tool.name} input schema is not closed")
        output = tool.output_schema()
        if output.get("additionalProperties") is not False:
            raise RuntimeError(f"{tool.name} output schema is not closed")
        if output.get("properties", {}).get("schema_version", {}).get("const") != 1:
            raise RuntimeError(f"{tool.name} output schema is not versioned")
    if any(re.search(r"plan[_ -]?amend", tool.name, re.IGNORECASE) for tool in TOOLS):
        raise RuntimeError("cw plan amend must not be exposed through MCP")
    if archive.read_bytes() != before:
        raise RuntimeError("public Plugin asset changed during validation")
    return {
        "core_version": __version__,
        "plugin_version": plugin_version,
        "plugin_sha256": digest,
        "plugin_size": len(before),
        "tool_count": len(TOOLS),
        "minimum_core": policy["core"]["minimum"],
        "maximum_core_exclusive": policy["core"]["maximum_exclusive"],
        "modified": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--size", type=int, required=True)
    args = parser.parse_args()
    print(json.dumps(validate(args.archive, args.sha256, args.size), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
