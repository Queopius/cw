#!/usr/bin/env python3
"""Validate local documentation links and anchors without network access."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
FENCE = re.compile(r"^```.*?^```\s*$", re.MULTILINE | re.DOTALL)
MARKDOWN_LINK = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)(?:\s+['\"].*?['\"])?\)")
HTML_LINK = re.compile(r"(?:href|src|srcset)=[\"']([^\"']+)[\"']")
HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$", re.MULTILINE)


def _slug(title: str) -> str:
    title = re.sub(r"<[^>]+>", "", title)
    title = title.replace("`", "").strip().lower()
    title = re.sub(r"[^\w\- ]", "", title, flags=re.UNICODE)
    return re.sub(r"[\s\-]+", "-", title).strip("-")


def _anchors(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8")
    anchors: set[str] = set()
    counts: dict[str, int] = {}
    for heading in HEADING.findall(text):
        base = _slug(heading)
        count = counts.get(base, 0)
        counts[base] = count + 1
        anchors.add(base if count == 0 else f"{base}_{count}")
    return anchors


def _resolve(source: Path, target: str) -> tuple[Path, str]:
    path_text, _, fragment = unquote(target).partition("#")
    if not path_text:
        return source, fragment
    candidate = (source.parent / path_text).resolve()
    if path_text.endswith("/"):
        candidate = (DOCS / f"{path_text.rstrip('/')}.md").resolve()
    return candidate, fragment


def broken_links(docs_dir: Path = DOCS) -> list[str]:
    problems: list[str] = []
    anchor_cache: dict[Path, set[str]] = {}
    for source in sorted(docs_dir.rglob("*.md")):
        text = FENCE.sub("", source.read_text(encoding="utf-8"))
        targets = MARKDOWN_LINK.findall(text) + HTML_LINK.findall(text)
        for target in targets:
            if target.startswith(("http://", "https://", "mailto:", "data:")):
                continue
            destination, fragment = _resolve(source, target)
            try:
                destination.relative_to(ROOT)
            except ValueError:
                problems.append(f"{source.relative_to(ROOT)}: link escapes repository: {target}")
                continue
            if not destination.exists():
                problems.append(f"{source.relative_to(ROOT)}: missing target: {target}")
                continue
            if fragment and destination.suffix.lower() == ".md":
                anchors = anchor_cache.setdefault(destination, _anchors(destination))
                if fragment not in anchors:
                    problems.append(f"{source.relative_to(ROOT)}: missing anchor: {target}")
    return problems


def main() -> int:
    problems = broken_links()
    if problems:
        for problem in problems:
            print(f"ERROR: {problem}", file=sys.stderr)
        return 1
    print("Documentation local links and anchors are valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
