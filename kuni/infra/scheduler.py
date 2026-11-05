"""Планировщик фоновых ритмов: сон-консолидация раз в сутки,
чистка рабочей памяти, ночной офлайн-статус.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable
from ..core import clock

log = logging.getLogger(__name__)

Job = Callable[[], Awaitable[None]]


class Scheduler:
    def __init__(self, bus: Any) -> None:
        self.bus = bus
        self._tasks: list[asyncio.Task] = []

    def start(self) -> None:
        self._tasks = [asyncio.ensure_future(t, loop=asyncio.get_running_loop()) for t in ()]

    def add_interval(self, name: str, interval_sec: float, job: Job) -> asyncio.Task:
        async def runner() -> None:
            while True:
                try:
                    await asyncio.sleep(interval_sec)
                    log.debug("запуск периодической задачи %s", name)
                    await job()
                except asyncio.CancelledError:
                    return
                except Exception:
                    log.exception("периодическая задача %s упала (продолжаю)", name)

        task = asyncio.ensure_future(runner())
        self._tasks.append(task)
        return task

    def add_daily(self, name: str, hour: int, job: Job) -> asyncio.Task:
        """Каждый день в указанный час локального времени."""
        async def runner() -> None:
            while True:
                # 28.10: время суток считается в фиксированном UTC+5,
                # а не в поясе ОС — иначе переезд на другую машину
                # сдвинул бы час консолидации и все суточные задачи.
                target = clock.wall_seconds(hour)
                if target <= time.time():
                    target += 86400
                await asyncio.sleep(max(1.0, target - time.time()))
                log.info("суточная задача %s запущена", name)
                self.bus.emit("scheduler.job", name=name)
                try:
                    await job()
                except Exception:
                    log.exception("суточная задача %s упала", name)

        task = asyncio.ensure_future(runner())
        self._tasks.append(task)
        return task

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()
