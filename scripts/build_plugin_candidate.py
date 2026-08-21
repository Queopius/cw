#!/usr/bin/env python3
"""Build a deterministic standalone archive of the CW plugin candidate."""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

try:
    from .validate_plugin_candidate import ROOT, validation_errors
except ImportError:  # Direct script execution keeps scripts/ on sys.path.
    from validate_plugin_candidate import ROOT, validation_errors


PLUGIN = ROOT / "plugins" / "cw"
FIXED_TIME = (2026, 8, 15, 0, 0, 0)


def plugin_version(root: Path = ROOT) -> str:
    return (root / "plugins" / "cw" / "VERSION").read_text(encoding="utf-8").strip()


def expected_members(root: Path = ROOT) -> dict[str, Path]:
    plugin = root / "plugins" / "cw"
    members = {
        (Path("cw") / path.relative_to(plugin)).as_posix(): path
        for path in sorted(plugin.rglob("*")) if path.is_file() and not path.is_symlink()
    }
    members.update({"cw/LICENSE": root / "LICENSE", "cw/NOTICE": root / "NOTICE"})
    return members


def validate_archive(archive_path: Path, root: Path = ROOT) -> list[str]:
    errors: list[str] = []
    expected = expected_members(root)
    try:
        with zipfile.ZipFile(archive_path) as archive:
            entries = archive.infolist()
            names = [entry.filename for entry in entries]
            if len(names) != len(set(names)):
                errors.append("archive contains duplicate entries")
            folded = [name.casefold() for name in names]
            if len(folded) != len(set(folded)):
                errors.append("archive contains case-colliding entries")
            for entry in entries:
                path = PurePosixPath(entry.filename)
                mode = entry.external_attr >> 16
                if path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] != "cw":
                    errors.append(f"archive entry has an unsafe path: {entry.filename}")
                if (mode & 0o170000) == 0o120000:
                    errors.append(f"archive entry is a symlink: {entry.filename}")
                if entry.date_time != FIXED_TIME:
                    errors.append(f"archive timestamp is not normalized: {entry.filename}")
                if mode != 0o100644:
                    errors.append(f"archive mode is not normalized: {entry.filename}")
            if names != sorted(expected):
                errors.append("archive inventory or ordering does not match the canonical Plugin bundle")
            for name, source in expected.items():
                if name in names and archive.read(name) != source.read_bytes():
                    errors.append(f"archive member differs from its canonical source: {name}")
    except (OSError, zipfile.BadZipFile) as exc:
        errors.append(f"archive is not a readable ZIP: {exc}")
    return errors


def build(output: Path) -> dict[str, object]:
    errors = validation_errors()
    if errors:
        raise ValueError("; ".join(errors))
    output.parent.mkdir(parents=True, exist_ok=True)
    archive_members = expected_members()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for relative, path in sorted(archive_members.items()):
            info = zipfile.ZipInfo(relative, FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes(), compresslevel=9)
    archive_errors = validate_archive(output)
    if archive_errors:
        raise ValueError("; ".join(archive_errors))
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    return {
        "schema_version": 1,
        "archive": output.name,
        "plugin_version": plugin_version(),
        "sha256": digest,
        "files": len(archive_members),
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
    output = args.output or ROOT / "artifacts" / f"cw-plugin-{plugin_version()}.zip"
    print(json.dumps(build(output), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
