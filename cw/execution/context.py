from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

from .events import EventSink


_EVENT_SINK: ContextVar[EventSink | None] = ContextVar("cw_execution_event_sink", default=None)


def current_event_sink() -> EventSink | None:
    return _EVENT_SINK.get()


@contextmanager
def execution_event_sink(sink: EventSink | None) -> Iterator[None]:
    token = _EVENT_SINK.set(sink)
    try:
        yield
    finally:
        _EVENT_SINK.reset(token)
