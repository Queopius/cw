#!/usr/bin/env python3
"""Build a deterministic standalone archive of the CW plugin candidate."""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
import zipfile
from pathlib import Path

try:
    from .validate_plugin_candidate import ROOT, validation_errors
except ImportError:  # Direct script execution keeps scripts/ on sys.path.
    from validate_plugin_candidate import ROOT, validation_errors


PLUGIN = ROOT / "plugins" / "cw"
FIXED_TIME = (2026, 8, 15, 0, 0, 0)


def build(output: Path) -> dict[str, object]:
    errors = validation_errors()
    if errors:
        raise ValueError("; ".join(errors))
    output.parent.mkdir(parents=True, exist_ok=True)
    members = sorted(path for path in PLUGIN.rglob("*") if path.is_file())
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in members:
            relative = Path("cw") / path.relative_to(PLUGIN)
            info = zipfile.ZipInfo(relative.as_posix(), FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes(), compresslevel=9)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    return {
        "schema_version": 1,
        "archive": output.name,
        "sha256": digest,
        "files": len(members),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check and args.output:
        parser.error("--check and --output cannot be combined")
    if args.check:
        with tempfile.TemporaryDirectory(prefix="cw-plugin-build-") as temporary:
            output = Path(temporary) / "cw-plugin.zip"
            first = build(output)
            first_bytes = output.read_bytes()
            second = build(output)
            if first_bytes != output.read_bytes() or first["sha256"] != second["sha256"]:
                raise RuntimeError("plugin candidate archive is not deterministic")
            print(json.dumps(first, sort_keys=True))
        return 0
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    output = args.output or ROOT / "artifacts" / f"cw-plugin-{version}.zip"
    print(json.dumps(build(output), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
