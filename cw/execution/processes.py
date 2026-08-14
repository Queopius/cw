from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable

from cw.core.platform import process_is_alive


@dataclass(frozen=True, slots=True)
class ProcessStatus:
    process_id: int | None
    alive: bool
    platform: str


class ProcessInspector:
    """Portable managed-process liveness without `/proc` or process-table scans."""

    def __init__(self, *, platform: str | None = None, signaler: Callable[[int, int], None] | None = None) -> None:
        self.platform = platform or os.name
        self.signaler = signaler

    def inspect(self, process_id: int | None) -> ProcessStatus:
        if not isinstance(process_id, int) or isinstance(process_id, bool) or process_id <= 0:
            return ProcessStatus(process_id, False, self.platform)
        if self.signaler is None:
            alive = process_is_alive(process_id)
        else:
            try:
                self.signaler(process_id, 0)
            except ProcessLookupError:
                alive = False
            except PermissionError:
                alive = True
            except OSError:
                alive = False
            else:
                alive = True
        return ProcessStatus(process_id, alive, self.platform)
