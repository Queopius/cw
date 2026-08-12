#!/usr/bin/env python3
"""Thin project hook. All mutable behavior remains in the installed CW package."""
from __future__ import annotations

import json
import os
import subprocess
import sys


def main() -> int:
    if os.environ.get("CW_REVIEWER_ACTIVE") == "1" or os.environ.get("CW_IMPLEMENTER_ACTIVE") != "1":
        print("{}")
        return 0
    payload = sys.stdin.read().strip()
    if payload:
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            print(json.dumps({"continue": False, "stopReason": "CW received invalid Stop hook input."}))
            return 0
        if not isinstance(event, dict) or event.get("hook_event_name") != "Stop":
            print(json.dumps({"continue": False, "stopReason": "CW phase gate accepts only Stop events."}))
            return 0
        if event.get("stop_hook_active") is True:
            reason = "CW phase gate will not recurse. Run: cw status"
            print(json.dumps({"continue": False, "stopReason": reason, "systemMessage": reason}))
            return 0
    completed = subprocess.run(["cw", "review", "--hook"], text=True, capture_output=True, check=False)
    output = completed.stdout.strip()
    if output:
        print(output)
    else:
        reason = completed.stderr.strip() or "CW phase review failed closed. Run: cw error"
        print(json.dumps({"continue": False, "stopReason": reason, "systemMessage": reason}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
