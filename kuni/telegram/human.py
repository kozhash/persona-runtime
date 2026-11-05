"""Человекоподобное поведение: не отвечает мгновенно, «набирает»
сообщение с реалистичной скоростью, ставит прочтения, ходит онлайн.

Использует обычный random — предсказуемость здесь враг правдоподобия.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any
from ..core import clock

log = logging.getLogger(__name__)


def _is_night(night_from: int, night_to: int, now: time.struct_time | None = None) -> bool:
    now = now or clock.localtime()
    hour = now.tm_hour
    if night_from <= night_to:
        return night_from <= hour < night_to
    return hour >= night_from or hour < night_to  # через полночь, напр. 22..8


class HumanBehavior:
    def __init__(self, request: Any, cfg_provider: Any, bus: Any | None = None) -> None:
        """request — async fn(dict) -> dict (см. TelegramModule.request)."""
        self.request = request
        self.cfg_provider = cfg_provider   # .current -> Config
        self.bus = bus
        self._last_activity = 0.0
        self._offline_task: asyncio.Task | None = None
        self._currently_typing: dict[int, float] = {}   # chat_id -> since
        self._online = False

    # ------------------------------------------------------------- статусы

    async def go_online(self) -> None:
        if self._online:
            return
        await self.request({"@type": "setOption", "name": "online",
                            "value": {"@type": "optionValueBoolean", "value": True}})
        self._online = True
        self._last_activity = time.time()
        self._emit("online", online=True)

    async def maybe_go_offline(self, idle_seconds: float = 240.0) -> None:
        if self._online and time.time() - self._last_activity > idle_seconds:
            await self.request({"@type": "setOption", "name": "online",
                                "value": {"@type": "optionValueBoolean", "value": False}})
            self._online = False
            self._emit("online", online=False)

    def is_night_now(self) -> bool:
        cfg = self.cfg_provider.current.telegram
        if not getattr(cfg, "night_enabled", True):
            return False          # ночной режим выключен явно
        return _is_night(cfg.night_from, cfg.night_to)

    # ------------------------------------------------------------ задержки

    async def before_reply(self, chat_id: int, text_len: int) -> None:
        """Пауза «прочитала и подумала» + демонстрация набора."""
        cfg = self.cfg_provider.current.telegram
        base = random.uniform(cfg.reply_delay_min_ms, cfg.reply_delay_max_ms) / 1000.0
        # длинный ответ — дольше «думает»
        base += min(6.0, text_len / 80.0)
        await asyncio.sleep(base)
        self._last_activity = time.time()

    async def typing_session(self, chat_id: int, text: str) -> None:
        """Набирает сообщение столько, сколько занял бы набор человеком."""
        cfg = self.cfg_provider.current.telegram
        seconds = max(0.5, len(text) / max(1.0, cfg.typing_speed_cps))
        await self.set_typing(chat_id, "chatActionTyping", active=True)
        self._currently_typing[chat_id] = time.time()
        self._emit("typing", chat_id=chat_id, typing=True, approx_seconds=round(seconds, 1),
                   text_preview=text[:400])
        try:
            # tdlib сам сбрасывает статус через ~5 сек — периодически продлеваем
            deadline = time.time() + seconds
            while time.time() < deadline:
                await asyncio.sleep(4.0)
                if time.time() < deadline:
                    await self.set_typing(chat_id, "chatActionTyping", active=True)
            await asyncio.sleep(max(0.0, deadline - time.time()))
        finally:
            self._currently_typing.pop(chat_id, None)
            await self.set_typing(chat_id, "chatActionCancel", active=False)
            self._emit("typing", chat_id=chat_id, typing=False)
            self._last_activity = time.time()

    async def set_typing(self, chat_id: int, action_type: str, *, active: bool) -> None:
        try:
            await self.request({
                "@type": "sendChatAction",
                "chat_id": chat_id,
                "action": {"@type": action_type},
            })
        except Exception as e:
            log.debug("sendChatAction не прошёл: %s", e)

    # ------------------------------------------------------------ прочтения

    async def mark_read(self, chat_id: int, message_ids: list[int], *, delay: bool = True) -> None:
        """Двойные галочки — но не мгновенно."""
        cfg = self.cfg_provider.current.telegram
        if delay and message_ids:
            await asyncio.sleep(random.uniform(cfg.read_delay_min_ms, cfg.read_delay_max_ms) / 1000.0)
        try:
            await self.request({
                "@type": "viewMessages",
                "chat_id": chat_id,
                "message_ids": message_ids,
                "force_read": True,
            })
            self._emit("read", chat_id=chat_id, message_ids=message_ids)
        except Exception as e:
            log.debug("viewMessages не прошёл: %s", e)
        self._last_activity = time.time()

    def _emit(self, kind: str, **payload: Any) -> None:
        if self.bus:
            self.bus.emit(f"telegram.human.{kind}", **payload)
