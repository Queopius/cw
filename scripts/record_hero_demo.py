#!/usr/bin/env python3
"""Record the official hero artifact from a real disposable CW workflow."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from hero_demo import (
    GOAL, HeroDemoError, atomic_write_artifact, record_real_workflow,
    resolve_installed_cw, source_root,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yes", action="store_true", help="Confirm real Codex usage non-interactively")
    parser.add_argument("--dry-run", action="store_true", help="Describe the recording without running CW or Codex")
    parser.add_argument("--keep-temp", action="store_true", help="Preserve the disposable repository for debugging")
    parser.add_argument("--cw-executable", help="Installed CW executable (defaults to PATH lookup)")
    parser.add_argument("--output", type=Path, help="Artifact destination")
    args = parser.parse_args(argv)

    root = source_root()
    version = (root / "VERSION").read_text(encoding="utf-8").strip()
    output = (args.output or root / "demo/hero/hero-demo.json").resolve()
    template = root / "demo/hero/project"
    print("HERO DEMO RECORDING\n")
    print("This operation will run a real Codex workflow.")
    print("Network access and Codex usage may occur.\n")
    print("Project: temporary demo repository")
    print(f"Goal: {GOAL}")
    print("Existing hero recording: preserved unless the new run completes successfully.")
    if args.dry_run:
        print("\nDRY RUN\nNo repository, CW workflow, Codex session, or artifact was changed.")
        return 0
    if not args.yes:
        if not sys.stdin.isatty() or input("\nStart real recording? [y/N] ").strip().lower() not in {"y", "yes"}:
            print("Recording cancelled. Existing artifact preserved.")
            return 2
    try:
        executable = resolve_installed_cw(args.cw_executable)
        print(f"\n✓ Installed CW resolved · {executable}")
        artifact, retained = record_real_workflow(
            executable=executable,
            template=template,
            expected_version=version,
            keep_temp=args.keep_temp,
        )
        print("✓ Real workflow completed")
        print(f"✓ {artifact['final_result']['valid_gates']} approval gate verified")
        atomic_write_artifact(output, artifact)
        print(f"✓ Public artifact committed atomically · {output}")
        if retained is not None:
            print(f"· Temporary repository preserved · {retained}")
        return 0
    except (HeroDemoError, OSError, TimeoutError) as exc:
        print(f"\n✕ Recording failed\n\n{exc}", file=sys.stderr)
        print("\nExisting hero-demo.json preserved.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
