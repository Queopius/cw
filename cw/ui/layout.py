from __future__ import annotations

import textwrap


MIN_WIDTH = 36
DEFAULT_WIDTH = 80
MAX_WIDTH = 88


def bounded_width(terminal_width: int) -> int:
    """Keep the canvas readable without overflowing or stretching indefinitely."""
    return max(20, min(MAX_WIDTH, terminal_width))


def visible_ljust(left: str, right: str, width: int) -> str:
    gap = max(1, width - len(left) - len(right))
    return f"{left}{' ' * gap}{right}"[:width]


def wrap(value: str, width: int) -> list[str]:
    return textwrap.wrap(
        value,
        width=max(8, width),
        break_long_words=False,
        break_on_hyphens=False,
    ) or [""]


def display_state(value: str) -> str:
    return value.replace("_", " ")
