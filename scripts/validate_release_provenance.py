#!/usr/bin/env python3
"""Fail closed unless an immutable annotated Core tag comes from prod."""
from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path


ZERO_SHA = "0" * 40


def git(root: Path, *arguments: str, check: bool = True) -> str:
    completed = subprocess.run(
        ["git", *arguments], cwd=root, text=True, encoding="utf-8", errors="replace",
        capture_output=True, check=False,
    )
    if check and completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or f"git {' '.join(arguments)} failed")
    return completed.stdout.strip()


def validate(root: Path, tag: str, expected_commit: str, branch_ref: str, push_before: str) -> None:
    version = (root / "VERSION").read_text(encoding="utf-8").strip()
    if tag != f"v{version}" or re.fullmatch(r"v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", tag) is None:
        raise RuntimeError("release tag does not match VERSION")
    if push_before != ZERO_SHA:
        raise RuntimeError("moved or recreated release tags are forbidden")
    if git(root, "cat-file", "-t", f"refs/tags/{tag}") != "tag":
        raise RuntimeError("public release tags must be annotated")
    peeled = git(root, "rev-parse", f"refs/tags/{tag}^{{commit}}")
    if peeled != expected_commit:
        raise RuntimeError("tag commit does not match the workflow commit")
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", expected_commit, branch_ref], cwd=root, check=False,
    )
    if ancestor.returncode != 0:
        raise RuntimeError("public release tag commit is not contained in origin/prod")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument("--tag", required=True)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--branch-ref", default="origin/prod")
    parser.add_argument("--push-before", required=True)
    args = parser.parse_args()
    validate(args.repository.resolve(), args.tag, args.expected_commit, args.branch_ref, args.push_before)
    print(f"{args.tag} is an immutable annotated tag contained in {args.branch_ref}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
