"""Внутренняя шина событий — клей, который держит модули раздельными.

Модули (telegram, memory, llm, dashboard, proxy...) не знают друг о друге:
они только публикуют события и подписываются на нужные. Упадёт один —
остальные продолжают работать.
"""
from __future__ import annotations

import asyncio
import fnmatch
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

log = logging.getLogger(__name__)

Handler = Callable[[dict[str, Any]], None]


@dataclass
class Event:
    topic: str
    payload: dict[str, Any]
    ts: float = field(default_factory=time.time)


class Bus:
    """Простой pub/sub на asyncio + журнал последних событий для дашборда."""

    def __init__(self, history_size: int = 500) -> None:
        self._subs: list[tuple[str, Handler]] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self.history: deque[Event] = deque(maxlen=history_size)

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self, pattern: str, handler: Handler) -> Callable[[], None]:
        """Подписка по glob-шаблону топика ('telegram.*', '*' и т.п.)."""
        self._subs.append((pattern, handler))

        def unsubscribe() -> None:
            try:
                self._subs.remove((pattern, handler))
            except ValueError:
                pass

        return unsubscribe

    def emit(self, topic: str, **payload: Any) -> None:
        """Опубликовать событие. Безопасно вызывать из любого потока."""
        event = Event(topic=topic, payload=payload)
        self.history.append(event)
        loop = self._loop
        if loop is None:
            self._dispatch(event)
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._dispatch(event)
        else:
            loop.call_soon_threadsafe(self._dispatch, event)

    def _dispatch(self, event: Event) -> None:
        for pattern, handler in list(self._subs):
            if not fnmatch.fnmatch(event.topic, pattern):
                continue
            try:
                result = handler(event.payload)
                if asyncio.iscoroutine(result):
                    asyncio.ensure_future(result)
            except Exception:
                log.exception("обработчик события %s упал", event.topic)

    def recent(self, n: int = 100, topics: Iterable[str] = ()) -> list[Event]:
        items = list(self.history)
        patterns = list(topics)
        if patterns:
            items = [e for e in items if any(fnmatch.fnmatch(e.topic, p) for p in patterns)]
        return items[-n:]
