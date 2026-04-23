from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any


EventHandler = Callable[[Any], Awaitable[None]]


class AsyncEventBus:
    def __init__(self) -> None:
        self._subscribers: list[tuple[tuple[type[Any], ...], EventHandler]] = []

    def subscribe(self, event_types: type[Any] | tuple[type[Any], ...], handler: EventHandler) -> None:
        if not isinstance(event_types, tuple):
            event_types = (event_types,)
        self._subscribers.append((event_types, handler))

    async def publish(self, event: Any) -> None:
        for event_types, handler in self._subscribers:
            if isinstance(event, event_types):
                await handler(event)
