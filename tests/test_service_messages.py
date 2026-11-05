"""Регрессии на служебные сообщения Telegram.

Из живого прогона 04:18: в переписке появились «[PinMessage]»,
«[SupergroupChatCreate]» — они уходили в LLM как реплики людей,
и модель пыталась на них отвечать.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from kuni.telegram.client import SERVICE_IGNORED, TelegramModule


class _Bus:
    history: list = []

    def emit(self, *a, **k):
        pass


def _module() -> TelegramModule:
    module = TelegramModule.__new__(TelegramModule)
    module.cfg_provider = None
    module.data_dir = Path(".")
    module.interactive = False
    module.td = None
    module.human = None
    module.on_message = None
    module.me_user_id = 1
    module.phone_hint = ""
    module.chats = {10: {"title": "чат", "type": "group",
                         "pinned": False, "muted": False,
                         "is_channel": False}}
    module.authorized = True
    module._started_at = 0.0
    module._auth_prompt = None
    module._auth_lock = None
    module.bus = _Bus()
    return module


@pytest.mark.asyncio
async def test_pin_message_is_ignored():
    """Закрепление сообщения — не реплика, в LLM идти не должно."""
    module = _module()
    called = []

    async def on_message(incoming):
        called.append(incoming)

    module.on_message = on_message
    message = {
        "chat_id": 10,
        "id": 99,
        "sender_id": {"@type": "messageSenderUser", "user_id": 2},
        "content": {"@type": "messagePinMessage", "message_id": 5},
    }
    await TelegramModule._process_new_message(module, message)
    assert called == [], "служебное сообщение дошло до обработчика"


@pytest.mark.asyncio
async def test_supergroup_create_is_ignored():
    module = _module()
    called = []

    async def on_message(incoming):
        called.append(incoming)

    module.on_message = on_message
    message = {
        "chat_id": 10,
        "id": 100,
        "sender_id": {"@type": "messageSenderUser", "user_id": 2},
        "content": {"@type": "messageSupergroupChatCreate", "title": "x"},
    }
    await TelegramModule._process_new_message(module, message)
    assert called == []


@pytest.mark.asyncio
async def test_normal_text_still_passes():
    """Обычный текст не должен отфильтроваться вместе со служебными."""
    module = _module()
    called = []

    async def on_message(incoming):
        called.append(incoming)

    module.on_message = on_message
    message = {
        "chat_id": 10,
        "id": 101,
        "sender_id": {"@type": "messageSenderUser", "user_id": 2},
        "content": {"@type": "messageText", "text": {"text": "привет"}},
    }
    await TelegramModule._process_new_message(module, message)
    assert len(called) == 1
    assert called[0].text == "привет"


@pytest.mark.asyncio
async def test_sticker_still_passes():
    """Стикер — не служебное, он должен доходить (для vision-разбора)."""
    module = _module()
    called = []

    async def on_message(incoming):
        called.append(incoming)

    module.on_message = on_message
    message = {
        "chat_id": 10,
        "id": 102,
        "sender_id": {"@type": "messageSenderUser", "user_id": 2},
        "content": {"@type": "messageSticker",
                    "sticker": {"sticker": {"id": 777}}},
    }
    await TelegramModule._process_new_message(module, message)
    assert len(called) == 1
    assert called[0].is_sticker is True


def test_service_set_covers_known_types():
    for ctype in ("messagePinMessage", "messageSupergroupChatCreate",
                  "messageChatAddMembers", "messageCall",
                  "messageScreenshotTaken"):
        assert ctype in SERVICE_IGNORED


def test_service_set_excludes_real_content():
    for ctype in ("messageText", "messagePhoto", "messageSticker",
                  "messageVoiceNote", "messagePoll"):
        assert ctype not in SERVICE_IGNORED
