#!/usr/bin/env python3
"""Require every public CW error code to appear in the error reference."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REFERENCE = ROOT / "docs" / "errors.md"


def missing_error_codes(reference: Path = REFERENCE) -> list[str]:
    sys.path.insert(0, str(ROOT))
    from cw.core.errors import ErrorCode

    text = reference.read_text(encoding="utf-8")
    return [code.value for code in ErrorCode if f"`{code.value}`" not in text]


def main() -> int:
    missing = missing_error_codes()
    if missing:
        print("ERROR: docs/errors.md is missing: " + ", ".join(missing), file=sys.stderr)
        return 1
    print("Error reference covers every public error code.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
