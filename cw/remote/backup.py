#!/usr/bin/env python3
"""Operator-only CW production SQLite backup and verification utility."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


EXPECTED_SCHEMA = 1


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inspect(path: Path) -> tuple[int, str]:
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        integrity_rows = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
        integrity = "ok" if integrity_rows == ["ok"] else "; ".join(integrity_rows)
        row = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        schema = int(row[0] or 0)
    finally:
        connection.close()
    return schema, integrity


def create_backup(source: Path, output: Path, deployed_sha: str) -> dict[str, object]:
    if not source.is_file():
        raise ValueError("source database does not exist")
    if output.exists() or output.with_suffix(output.suffix + ".manifest.json").exists():
        raise ValueError("backup output or manifest already exists")
    if source.resolve() == output.resolve():
        raise ValueError("backup output must differ from the source database")
    if re.fullmatch(r"[0-9a-f]{40}", deployed_sha) is None:
        raise ValueError("deployed SHA must be a full lowercase Git SHA")
    if not output.parent.is_dir():
        raise ValueError("backup destination directory does not exist")

    source_connection = sqlite3.connect(f"file:{source.resolve().as_posix()}?mode=ro", uri=True)
    destination = sqlite3.connect(output)
    if os.name != "nt":
        os.chmod(output, 0o600)
    try:
        source_connection.backup(destination)
    finally:
        destination.close()
        source_connection.close()
    if os.name != "nt":
        os.chmod(output, 0o600)

    schema, integrity = _inspect(output)
    if schema != EXPECTED_SCHEMA or integrity != "ok":
        raise ValueError(f"backup verification failed: schema={schema}, integrity={integrity}")
    payload: dict[str, object] = {
        "schema_version": schema,
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "deployed_sha": deployed_sha,
        "backup_sha256": _sha256(output),
        "integrity_result": integrity,
        "size_bytes": output.stat().st_size,
    }
    manifest = output.with_suffix(output.suffix + ".manifest.json")
    manifest.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if os.name != "nt":
        os.chmod(manifest, 0o600)
    return payload


def verify_backup(backup: Path, expected_sha256: str) -> dict[str, object]:
    if not backup.is_file():
        raise ValueError("backup does not exist")
    if re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None:
        raise ValueError("expected backup SHA-256 is invalid")
    actual = _sha256(backup)
    if actual != expected_sha256:
        raise ValueError("backup SHA-256 does not match")
    schema, integrity = _inspect(backup)
    if schema != EXPECTED_SCHEMA or integrity != "ok":
        raise ValueError(f"backup verification failed: schema={schema}, integrity={integrity}")
    return {"schema_version": schema, "backup_sha256": actual, "integrity_result": integrity}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    backup = subparsers.add_parser("backup")
    backup.add_argument("--source", type=Path, required=True)
    backup.add_argument("--output", type=Path, required=True)
    backup.add_argument("--deployed-sha", required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--backup", type=Path, required=True)
    verify.add_argument("--sha256", required=True)
    args = parser.parse_args()
    try:
        if args.action == "backup":
            result = create_backup(args.source, args.output, args.deployed_sha)
        else:
            result = verify_backup(args.backup, args.sha256)
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
