from __future__ import annotations

import re

from cw.core.errors import CwError, ErrorCode


_DURATION = re.compile(r"^(?:(?P<hours>[1-9]\d*)h)?(?:(?P<minutes>[1-9]\d*)m)?$")


def parse_duration(value: str) -> int:
    match = _DURATION.fullmatch(value.strip().lower())
    if not match or not (match.group("hours") or match.group("minutes")):
        raise CwError(
            f"Invalid duration: {value}", ErrorCode.USAGE_ERROR,
            "Use a duration such as 30m, 2h, or 1h30m", exit_code=2,
        )
    hours = int(match.group("hours") or 0)
    minutes = int(match.group("minutes") or 0)
    if minutes >= 60 and hours:
        raise CwError(
            f"Invalid duration: {value}", ErrorCode.USAGE_ERROR,
            "Use normalized syntax such as 2h30m or 90m", exit_code=2,
        )
    seconds = hours * 3600 + minutes * 60
    if seconds <= 0:
        raise CwError("Duration must be positive", ErrorCode.USAGE_ERROR, exit_code=2)
    return seconds


def format_duration(seconds: float) -> str:
    value = max(0, int(seconds))
    hours, remainder = divmod(value, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m" if minutes else f"{hours}h"
    if minutes:
        return f"{minutes}m{secs:02d}s" if secs else f"{minutes}m"
    return f"{secs}s"
