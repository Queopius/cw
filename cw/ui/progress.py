from __future__ import annotations

from dataclasses import dataclass


def progress_percentage(approved: int, total: int) -> int:
    if total <= 0:
        return 0
    safe = min(max(approved, 0), total)
    return round((safe / total) * 100)


@dataclass(frozen=True, slots=True)
class ProgressBar:
    complete: str
    remaining: str
    percentage: int


def progress_bar(approved: int, total: int, width: int, *, unicode: bool) -> ProgressBar:
    percentage = progress_percentage(approved, total)
    bar_width = max(4, width)
    filled = round(bar_width * percentage / 100)
    completed_symbol, pending_symbol = ("█", "░") if unicode else ("#", "-")
    return ProgressBar(
        complete=completed_symbol * filled,
        remaining=pending_symbol * (bar_width - filled),
        percentage=percentage,
    )
