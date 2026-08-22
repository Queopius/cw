#!/usr/bin/env python3
"""Safely prepare a local CW evaluation marketplace from a canonical Plugin ZIP."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

try:
    from .build_plugin_candidate import validate_archive
    from .validate_plugin_candidate import ROOT
except ImportError:  # Direct script execution keeps scripts/ on sys.path.
    from build_plugin_candidate import validate_archive
    from validate_plugin_candidate import ROOT


MARKETPLACE_RELATIVE = Path(".agents/plugins/marketplace.json")
PLUGIN_RELATIVE = Path("plugins/cw")
EXPECTED_MARKETPLACE_NAME = "cw-development"
EXPECTED_PLUGIN_NAME = "cw"
REQUIRED_PLUGIN_FILES = {
    ".codex-plugin/plugin.json",
    ".mcp.json",
    "README.md",
    "VERSION",
    "capabilities.json",
    "skills/cw-workflow/SKILL.md",
    "skills/cw-workflow/agents/openai.yaml",
    "assets/cw-logo-dark.png",
    "assets/cw-mark-64.png",
    "assets/cw-mark.png",
}
REQUIRED_ARCHIVE_LEGAL_FILES = {"LICENSE", "NOTICE"}
FORBIDDEN_COMPONENTS = {".git", ".cw", "__pycache__", ".pytest_cache", "artifacts", "build", "dist"}
MAX_MEMBER_SIZE = 16 * 1024 * 1024
MAX_ARCHIVE_SIZE = 32 * 1024 * 1024


class DistributionError(ValueError):
    """A local Plugin distribution source is unsafe or inconsistent."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DistributionError(f"invalid JSON at {path.name}: {exc}") from exc


def _regular_file(path: Path, label: str) -> None:
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise DistributionError(f"{label} is missing: {exc}") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise DistributionError(f"{label} must be a regular non-symlink file")


def _relative_source_path(value: object) -> PurePosixPath:
    if not isinstance(value, str) or not value.startswith("./"):
        raise DistributionError("marketplace source.path must start with ./")
    if "\\" in value or "\x00" in value or re.match(r"^\./[A-Za-z]:", value):
        raise DistributionError("marketplace source.path is not a portable relative path")
    relative = PurePosixPath(value[2:])
    if not relative.parts or relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise DistributionError("marketplace source.path must remain inside the marketplace root")
    return relative


def _reject_symlink_components(root: Path, relative: PurePosixPath) -> Path:
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise DistributionError(f"marketplace source contains a symlink component: {relative}")
    resolved_root = root.resolve(strict=True)
    resolved = current.resolve(strict=True)
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise DistributionError("marketplace source.path escapes the marketplace root") from exc
    return resolved


def validate_plugin_tree(plugin: Path, *, require_legal: bool) -> None:
    if not plugin.is_dir() or plugin.is_symlink():
        raise DistributionError("marketplace Plugin source must be a real directory")
    required = set(REQUIRED_PLUGIN_FILES)
    if require_legal:
        required.update(REQUIRED_ARCHIVE_LEGAL_FILES)
    for relative in sorted(required):
        _regular_file(plugin / relative, f"Plugin file {relative}")
    manifest = _load_json(plugin / ".codex-plugin/plugin.json")
    mcp = _load_json(plugin / ".mcp.json")
    version = (plugin / "VERSION").read_text(encoding="utf-8").strip()
    if not isinstance(manifest, dict) or manifest.get("name") != EXPECTED_PLUGIN_NAME:
        raise DistributionError("Plugin manifest identity must be cw")
    if manifest.get("version") != version:
        raise DistributionError("Plugin manifest and VERSION must match")
    if manifest.get("skills") != "./skills/" or manifest.get("mcpServers") != "./.mcp.json":
        raise DistributionError("Plugin manifest component paths are invalid")
    if mcp != {
        "mcpServers": {
            "cw": {
                "command": "cw",
                "args": ["mcp", "serve", "--allowed-root", ".", "--project", "."],
            }
        }
    }:
        raise DistributionError("Plugin MCP definition is invalid or over-broad")
    for path in sorted(plugin.rglob("*")):
        relative = path.relative_to(plugin)
        if path.is_symlink():
            raise DistributionError(f"Plugin tree contains a symlink: {relative.as_posix()}")
        if any(part.casefold() in FORBIDDEN_COMPONENTS for part in relative.parts):
            raise DistributionError(f"Plugin tree contains a forbidden component: {relative.as_posix()}")
        if path.is_file() and path.stat().st_mode & 0o111:
            raise DistributionError(f"Plugin tree contains an unexpected executable: {relative.as_posix()}")


def validate_marketplace_root(root: Path, *, require_legal: bool = False) -> Path:
    root = root.resolve(strict=True)
    marketplace_path = root / MARKETPLACE_RELATIVE
    _regular_file(marketplace_path, "repository marketplace")
    marketplace = _load_json(marketplace_path)
    if not isinstance(marketplace, dict) or set(marketplace) != {"name", "interface", "plugins"}:
        raise DistributionError("repository marketplace root schema is invalid")
    if marketplace.get("name") != EXPECTED_MARKETPLACE_NAME:
        raise DistributionError("repository marketplace name must be cw-development")
    if marketplace.get("interface") != {"displayName": "CW Development"}:
        raise DistributionError("repository marketplace displayName is invalid")
    plugins = marketplace.get("plugins")
    if not isinstance(plugins, list) or len(plugins) != 1 or not isinstance(plugins[0], dict):
        raise DistributionError("repository marketplace must contain exactly one Plugin")
    entry = plugins[0]
    if set(entry) != {"name", "source", "policy", "category"} or entry.get("name") != EXPECTED_PLUGIN_NAME:
        raise DistributionError("repository marketplace Plugin entry schema is invalid")
    source = entry.get("source")
    if not isinstance(source, dict) or set(source) != {"source", "path"} or source.get("source") != "local":
        raise DistributionError("repository marketplace source must be local")
    relative = _relative_source_path(source.get("path"))
    if relative != PurePosixPath("plugins/cw"):
        raise DistributionError("repository marketplace source must resolve to ./plugins/cw")
    if entry.get("policy") != {"installation": "AVAILABLE", "authentication": "ON_INSTALL"}:
        raise DistributionError("repository marketplace policy must be AVAILABLE/ON_INSTALL")
    if entry.get("category") != "Coding":
        raise DistributionError("repository marketplace category must be Coding")
    plugin = _reject_symlink_components(root, relative)
    validate_plugin_tree(plugin, require_legal=require_legal)
    serialized = json.dumps(marketplace, sort_keys=True).casefold()
    if any(term in serialized for term in ("token", "secret", "/home/", "c:\\users\\")):
        raise DistributionError("repository marketplace contains a secret or private path marker")
    return plugin


def _validate_zip_entries(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    entries = archive.infolist()
    names = [entry.filename for entry in entries]
    if not entries or len(names) != len(set(names)):
        raise DistributionError("Plugin ZIP is empty or contains duplicate entries")
    if len({name.casefold() for name in names}) != len(names):
        raise DistributionError("Plugin ZIP contains case-colliding entries")
    total = 0
    for entry in entries:
        name = entry.filename
        if "\x00" in name or "\\" in name or re.match(r"^[A-Za-z]:", name):
            raise DistributionError(f"Plugin ZIP contains a non-portable path: {name}")
        relative = PurePosixPath(name)
        if relative.is_absolute() or not relative.parts or relative.parts[0] != "cw" or ".." in relative.parts:
            raise DistributionError(f"Plugin ZIP entry escapes the Plugin root: {name}")
        if entry.is_dir():
            raise DistributionError(f"Plugin ZIP contains an unexpected directory entry: {name}")
        mode = entry.external_attr >> 16
        if not stat.S_ISREG(mode) or mode != 0o100644:
            raise DistributionError(f"Plugin ZIP entry is not a normalized regular file: {name}")
        if entry.flag_bits & 0x1:
            raise DistributionError(f"Plugin ZIP entry is encrypted: {name}")
        if entry.file_size > MAX_MEMBER_SIZE:
            raise DistributionError(f"Plugin ZIP member is oversized: {name}")
        total += entry.file_size
        if total > MAX_ARCHIVE_SIZE:
            raise DistributionError("Plugin ZIP expands beyond the allowed size")
        if entry.file_size and entry.compress_size == 0:
            raise DistributionError(f"Plugin ZIP member has an invalid compression ratio: {name}")
        if entry.compress_size and entry.file_size / entry.compress_size > 1000:
            raise DistributionError(f"Plugin ZIP member has an excessive compression ratio: {name}")
    return entries


def prepare_marketplace(
    archive_path: Path,
    destination: Path,
    *,
    expected_sha256: str,
    root: Path = ROOT,
) -> dict[str, object]:
    _regular_file(archive_path, "Plugin ZIP")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise DistributionError("expected SHA-256 must be 64 lowercase hexadecimal characters")
    observed = sha256_file(archive_path)
    if observed != expected_sha256:
        raise DistributionError("Plugin ZIP SHA-256 does not match the expected digest")
    archive_errors = validate_archive(archive_path, root)
    if archive_errors:
        raise DistributionError("; ".join(archive_errors))
    if destination.exists() or destination.is_symlink():
        raise DistributionError("destination already exists; refusing a partial or in-place update")
    destination.parent.mkdir(parents=True, exist_ok=True)
    canonical_marketplace = root / MARKETPLACE_RELATIVE
    _regular_file(canonical_marketplace, "canonical repository marketplace")
    with tempfile.TemporaryDirectory(prefix=".cw-plugin-marketplace-", dir=destination.parent) as temporary:
        stage = Path(temporary) / "marketplace"
        plugin = stage / PLUGIN_RELATIVE
        plugin.mkdir(parents=True)
        with zipfile.ZipFile(archive_path) as archive:
            entries = _validate_zip_entries(archive)
            for entry in entries:
                relative = PurePosixPath(entry.filename).relative_to("cw")
                target = plugin.joinpath(*relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(entry, "r") as source, target.open("xb") as output:
                    while chunk := source.read(1024 * 1024):
                        output.write(chunk)
                target.chmod(0o644)
        marketplace = stage / MARKETPLACE_RELATIVE
        marketplace.parent.mkdir(parents=True, exist_ok=True)
        marketplace.write_bytes(canonical_marketplace.read_bytes())
        marketplace.chmod(0o644)
        validate_marketplace_root(stage, require_legal=True)
        os.replace(stage, destination)
    return {
        "schema_version": 1,
        "marketplace": EXPECTED_MARKETPLACE_NAME,
        "plugin": EXPECTED_PLUGIN_NAME,
        "plugin_version": (destination / PLUGIN_RELATIVE / "VERSION").read_text(encoding="utf-8").strip(),
        "sha256": observed,
        "destination": str(destination.resolve()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--sha256", required=True, dest="expected_sha256")
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    try:
        result = prepare_marketplace(
            args.archive.resolve(), args.destination.resolve(),
            expected_sha256=args.expected_sha256, root=args.root.resolve(),
        )
    except (DistributionError, OSError, zipfile.BadZipFile) as exc:
        parser.exit(1, f"ERROR: {exc}\n")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
