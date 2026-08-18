#!/usr/bin/env python3
"""Synchronize the Read the Docs stable alias version."""
from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request


API_BASE = "https://app.readthedocs.org/api/v3"


def _request(method: str, url: str, token: str, payload: dict | None = None) -> tuple[int, str]:
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url=url, method=method, data=data)
    req.add_header("Authorization", f"Token {token}")
    req.add_header("Accept", "application/json")
    if payload is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{exc.code} {exc.reason}: {body}") from exc


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, help="Read the Docs project slug")
    parser.add_argument("--alias", default="stable", help="RTD alias/version slug to retarget")
    parser.add_argument("--ref", required=True, help="Target VCS ref for the alias (tag/branch)")
    parser.add_argument(
        "--token",
        default=os.environ.get("READTHEDOCS_TOKEN", ""),
        help="Read the Docs API token (or set READTHEDOCS_TOKEN)",
    )
    parser.add_argument(
        "--trigger-build",
        action="store_true",
        help="Trigger a rebuild after changing the alias mapping.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report planned RTD mutation without making requests.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if not args.token:
        print("READTHEDOCS_TOKEN missing.")
        return 1
    if args.dry_run:
        print(f"Would update {args.alias} in project {args.project} to ref {args.ref}.")
        return 0

    base = f"{API_BASE}/projects/{args.project}"
    version_url = f"{base}/versions/{args.alias}/"
    current_status, current_body = _request("GET", version_url, args.token)
    if current_status != 200:
        raise SystemExit(f"Unable to read alias {args.alias}: HTTP {current_status}")
    current = json.loads(current_body)

    current_ref = current.get("ref")
    if current_ref == args.ref:
        print(f"Alias {args.alias} already points to {args.ref}.")
        return 0

    payload = {"ref": args.ref}
    print(f"Updating {args.alias} -> {args.ref}")
    if args.dry_run:
        return 0

    patch_status, patch_body = _request("PATCH", version_url, args.token, payload=payload)
    if patch_status != 204:
        raise SystemExit(f"Unable to patch {args.alias}: HTTP {patch_status}: {patch_body}")

    if args.trigger_build:
        build_url = f"{version_url}builds/"
        build_status, build_body = _request("POST", build_url, args.token)
        if build_status != 202:
            raise SystemExit(f"Unable to trigger build for {args.alias}: HTTP {build_status}: {build_body}")
        print("Rebuild triggered.")

    print(f"Alias {args.alias} now points to {args.ref}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
