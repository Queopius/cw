#!/usr/bin/env python3
"""Publish a tagged documentation version as Read the Docs stable."""
from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request


API_BASE = "https://app.readthedocs.org/api/v3"
RELEASE_TAG = re.compile(r"^v\d+\.\d+\.\d+$")


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
        return exc.code, exc.read().decode("utf-8", errors="replace")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, help="Read the Docs project slug")
    parser.add_argument("--version", required=True, help="Stable release tag, for example v0.14.0")
    parser.add_argument("--alias", default="stable", help="Managed RTD stable alias slug")
    parser.add_argument(
        "--token",
        default=os.environ.get("READTHEDOCS_TOKEN", ""),
        help="Read the Docs API token (or set READTHEDOCS_TOKEN)",
    )
    parser.add_argument(
        "--trigger-build",
        action="store_true",
        help="Trigger a build when the version is already active.",
    )
    parser.add_argument("--wait-seconds", type=int, default=600)
    parser.add_argument("--poll-interval", type=float, default=5)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report the planned RTD synchronization without making requests.",
    )
    return parser.parse_args()


def _get_json(url: str, token: str) -> tuple[int, dict]:
    status, body = _request("GET", url, token)
    if status == 404:
        return status, {}
    if status != 200:
        raise RuntimeError(f"GET {url} failed: HTTP {status}: {body}")
    return status, json.loads(body)


def _wait_for_version(version_url: str, token: str, deadline: float, interval: float) -> dict:
    while time.monotonic() < deadline:
        status, version = _get_json(version_url, token)
        if status == 200:
            return version
        time.sleep(interval)
    raise RuntimeError("Timed out waiting for Read the Docs to discover the release tag.")


def _wait_for_stable(
    version_url: str,
    alias_url: str,
    version: str,
    token: str,
    deadline: float,
    interval: float,
) -> None:
    last_ref = None
    while time.monotonic() < deadline:
        _, target = _get_json(version_url, token)
        _, alias = _get_json(alias_url, token)
        last_ref = alias.get("ref")
        if target.get("built") is True and last_ref == version:
            return
        time.sleep(interval)
    raise RuntimeError(
        f"Timed out waiting for {version} to build and stable to update "
        f"(stable.ref={last_ref!r})."
    )


def main() -> int:
    args = _parse_args()
    if not RELEASE_TAG.fullmatch(args.version):
        print("--version must be a stable release tag in vMAJOR.MINOR.PATCH form.")
        return 2
    if args.dry_run:
        print(f"Would sync {args.version}, build it, and verify {args.alias} -> {args.version}.")
        return 0
    if not args.token:
        print("READTHEDOCS_TOKEN missing.")
        return 1

    project_url = f"{API_BASE}/projects/{urllib.parse.quote(args.project, safe='')}"
    version_slug = urllib.parse.quote(args.version, safe="")
    alias_slug = urllib.parse.quote(args.alias, safe="")
    version_url = f"{project_url}/versions/{version_slug}/"
    alias_url = f"{project_url}/versions/{alias_slug}/"
    deadline = time.monotonic() + args.wait_seconds

    sync_status, sync_body = _request("POST", f"{project_url}/sync-versions/", args.token)
    if sync_status != 202:
        raise RuntimeError(f"Unable to sync RTD versions: HTTP {sync_status}: {sync_body}")
    print(f"Version synchronization requested for {args.version}.")

    target = _wait_for_version(version_url, args.token, deadline, args.poll_interval)
    was_active = target.get("active") is True
    if not was_active or target.get("hidden") is True:
        patch_status, patch_body = _request(
            "PATCH",
            version_url,
            args.token,
            payload={"active": True, "hidden": False},
        )
        if patch_status != 204:
            raise RuntimeError(
                f"Unable to activate {args.version}: HTTP {patch_status}: {patch_body}"
            )
        print(f"Activated {args.version}; Read the Docs started its build.")
    elif args.trigger_build:
        build_status, build_body = _request("POST", f"{version_url}builds/", args.token)
        if build_status != 202:
            raise RuntimeError(
                f"Unable to build {args.version}: HTTP {build_status}: {build_body}"
            )
        print(f"Rebuild triggered for {args.version}.")

    _wait_for_stable(
        version_url,
        alias_url,
        args.version,
        args.token,
        deadline,
        args.poll_interval,
    )
    print(f"Verified: {args.alias} points to built version {args.version}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
