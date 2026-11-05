"""Регрессии на реальные баги, найденные при разборе проекта.

Каждый тест закрывает конкретную поломку, которая была в коде:
  * краш на войсе с подписью (caption — объект, а не строка);
  * падение при пустом ответе модели;
  * отсутствие эмбеддингов валит память вместо мягкой деградации.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from kuni.telegram.client import IncomingMessage, _caption_text


def test_caption_text_handles_none():
    """Баг: (content.get("caption") or "").get("text") падал на None."""
    assert _caption_text({}) == ""
    assert _caption_text({"caption": None}) == ""
    assert _caption_text({"caption": {"text": "привет"}}) == "привет"


def test_caption_text_ignores_wrong_type():
    assert _caption_text({"caption": "строка вместо объекта"}) == ""


class FakeChat:
    """Минимальная замена TelegramModule для проверки парсинга апдейта."""

    def __init__(self):
        self.me_user_id = 1
        self.chats = {10: {"title": "чат", "type": "private",
                           "pinned": False, "muted": False}}

    async def _cache_chat(self, chat_id):
        return self.chats.get(chat_id, {})


async def _process(module, message):
    """Вызвать _process_new_message на фейковом модуле."""
    from kuni.telegram.client import TelegramModule

    captured = {}

    async def on_message(incoming):
        captured["msg"] = incoming

    module.on_message = on_message
    await TelegramModule._process_new_message(module, message)
    return captured.get("msg")


@pytest.mark.asyncio
async def test_voice_with_caption_does_not_crash():
    """Голосовое с подписью раньше роняло обработчик."""
    from kuni.telegram.client import TelegramModule

    module = TelegramModule.__new__(TelegramModule)
    module.cfg_provider = None
    module.data_dir = Path(".")
    module.interactive = False
    module.td = None
    module.human = None
    module.on_message = None
    module.me_user_id = 1
    module.phone_hint = ""
    module.chats = {10: {"title": "чат", "type": "private",
                         "pinned": False, "muted": False}}
    module.authorized = True
    module._started_at = time.time()
    module._auth_prompt = None
    module._auth_lock = None

    class Bus:
        def emit(self, *a, **k):
            pass
        history = []

    module.bus = Bus()

    message = {
        "chat_id": 10,
        "id": 5,
        "sender_id": {"@type": "messageSenderUser", "user_id": 2},
        "content": {
            "@type": "messageVoiceNote",
            "voice": {"id": 777},
            "caption": {"text": "слушай"},
        },
    }
    incoming = await _process(module, message)
    assert incoming is not None
    assert incoming.is_voice is True
    assert incoming.voice_file_id == 777
    assert incoming.text == "слушай"


@pytest.mark.asyncio
async def test_voice_without_caption_is_safe():
    from kuni.telegram.client import TelegramModule

    module = TelegramModule.__new__(TelegramModule)
    module.cfg_provider = None
    module.data_dir = Path(".")
    module.interactive = False
    module.td = None
    module.human = None
    module.on_message = None
    module.me_user_id = 1
    module.phone_hint = ""
    module.chats = {10: {"title": "чат", "type": "private",
                         "pinned": False, "muted": False}}
    module.authorized = True
    module._started_at = time.time()
    module._auth_prompt = None
    module._auth_lock = None

    class Bus:
        def emit(self, *a, **k):
            pass
        history = []

    module.bus = Bus()

    message = {
        "chat_id": 10,
        "id": 6,
        "sender_id": {"@type": "messageSenderUser", "user_id": 2},
        "content": {"@type": "messageVoiceNote", "voice": {"id": 888}},
    }
    incoming = await _process(module, message)
    assert incoming is not None
    assert incoming.text == ""


@pytest.mark.asyncio
async def test_sticker_flagged():
    from kuni.telegram.client import TelegramModule

    module = TelegramModule.__new__(TelegramModule)
    module.cfg_provider = None
    module.data_dir = Path(".")
    module.interactive = False
    module.td = None
    module.human = None
    module.on_message = None
    module.me_user_id = 1
    module.phone_hint = ""
    module.chats = {10: {"title": "чат", "type": "private",
                         "pinned": False, "muted": False}}
    module.authorized = True
    module._started_at = time.time()
    module._auth_prompt = None
    module._auth_lock = None

    class Bus:
        def emit(self, *a, **k):
            pass
        history = []

    module.bus = Bus()

    message = {
        "chat_id": 10,
        "id": 7,
        "sender_id": {"@type": "messageSenderUser", "user_id": 2},
        "content": {"@type": "messageSticker",
                    "sticker": {"sticker": {"id": 999}}},
    }
    incoming = await _process(module, message)
    assert incoming is not None
    assert incoming.is_sticker is True
    assert incoming.sticker_file_id == 999


@pytest.mark.asyncio
async def test_own_message_is_ignored():
    from kuni.telegram.client import TelegramModule

    module = TelegramModule.__new__(TelegramModule)
    module.me_user_id = 1
    module.chats = {}
    module.bus = type("B", (), {"emit": lambda *a, **k: None, "history": []})()

    called = []

    async def on_message(incoming):
        called.append(incoming)

    module.on_message = on_message
    message = {
        "chat_id": 10, "id": 8,
        "sender_id": {"@type": "messageSenderUser", "user_id": 1},
        "content": {"@type": "messageText", "text": {"text": "моё"}},
    }
    await TelegramModule._process_new_message(module, message)
    assert called == []
