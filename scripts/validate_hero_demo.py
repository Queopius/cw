#!/usr/bin/env python3
"""Validate CW's committed hero artifact entirely offline."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from hero_demo import (
    HeroDemoError,
    load_and_validate,
    recording_is_patch_compatible,
    source_root,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", type=Path)
    args = parser.parse_args(argv)
    root = source_root()
    path = (args.path or root / "demo/hero/hero-demo.json").resolve()
    version = (root / "VERSION").read_text(encoding="utf-8").strip()
    try:
        artifact = load_and_validate(path)
        recorded_version = str(artifact["cw_version"])
        if recorded_version != version and not recording_is_patch_compatible(recorded_version, version):
            raise HeroDemoError(
                f"Hero recording version {recorded_version} is not compatible with VERSION {version}"
            )
    except HeroDemoError as exc:
        print(f"HERO DEMO INVALID\n{exc}", file=sys.stderr)
        return 1
    final = artifact["final_result"]
    if artifact["cw_version"] != version:
        print(
            f"HERO DEMO NOTICE\n"
            f"Real recording CW {artifact['cw_version']} remains valid for the compatible public UI contract in {version}.",
            file=sys.stderr,
        )
    print(
        "HERO DEMO VALID\n"
        f"CW {artifact['cw_version']} · {len(artifact['events'])} events · "
        f"{final['valid_gates']} verified gate(s) · {final['workflow_status']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
