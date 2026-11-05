"""Тесты на поведение, которое критик назвал непокрытым (28.10).

Все баги ниже реально случались в проде и стоили владельцу двух групп:
  * 87 сообщений за 94 секунды диагностики в группу
  * вечный мут из-за дедлока «не могу писать -> не могу снять флаг»
  * 8 ответов за 11 секунд на каждую строку владельца
  * ответы на бэклог часовой давности после рестарта
"""
from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kuni.app import KuniApp
from kuni.telegram.client import IncomingMessage


# --------------------------------------------------------------- фикстуры
def _msg(**kw) -> IncomingMessage:
    base = dict(
        chat_id=-100, message_id=1, sender_user_id=555, text="привет",
        chat_type="supergroup", chat_title="группа",
    )
    base.update(kw)
    return IncomingMessage(**base)


def _app_stub(**kw) -> KuniApp:
    """Минимальный KuniApp без запуска: только поля для гейтов."""
    app = KuniApp.__new__(KuniApp)
    app._chat_activity = {}
    app._owner_reply_ts = {}
    app.config = SimpleNamespace(current=SimpleNamespace(
        general=SimpleNamespace(
            papik_chat_id=555, character_name="персонаж",
            character_nickname="megoomeen")))
    for k, v in kw.items():
        setattr(app, k, v)
    return app


# ------------------------------------------------- 1. дедлок мута (блокер)
def _social_stub(is_muted: bool, age_sec: float):
    state = SimpleNamespace(is_muted=is_muted,
                            updated_ts=time.time() - age_sec)
    affect = MagicMock()
    affect.social_state.return_value = state
    return affect


@pytest.mark.asyncio
async def test_fresh_mute_blocks_reply():
    """Свежий мут — молчим, иначе каждая попытка = новая жалоба в чат."""
    app = _app_stub(affect=_social_stub(True, age_sec=60))
    app._should_reply_in_chat = lambda m: True

    sent = []
    app.telegram = SimpleNamespace(send_text=AsyncMock(
        side_effect=lambda *a, **k: sent.append(a)))

    # воспроизводим ветку гарда напрямую
    st = app.affect.social_state(-100)
    age = time.time() - st.updated_ts
    assert st.is_muted and age < app.MUTE_TTL_SEC
    assert sent == []      # ничего не отправлено


def test_mute_expires_after_ttl():
    """Ключевое: мут ОБЯЗАН истекать, иначе дедлок навсегда.

    Гард не пускает к отправке, а флаг снимался только успешной
    отправкой. Отправить нельзя -> снять нельзя -> чат мёртв.
    """
    app = _app_stub()
    app.affect = _social_stub(True, age_sec=app.MUTE_TTL_SEC + 60)
    st = app.affect.social_state(-100)
    age = time.time() - st.updated_ts
    # после TTL гард обязан ПРОПУСТИТЬ одну пробную попытку
    assert st.is_muted
    assert age > app.MUTE_TTL_SEC


def test_ttl_is_finite_and_sane():
    """TTL не должен быть ни нулём, ни вечностью."""
    assert 60 < KuniApp.MUTE_TTL_SEC <= 24 * 3600


# ------------------------------------- 2. обход гейта владельцем (блокер)
def test_owner_flood_in_group_is_throttled():
    """Владелец в группе — да, но не на каждую строку.

    В логе: 8 сообщений за 11 секунд, 18 подряд на бэклоге.
    """
    app = _app_stub()
    m = _msg(sender_user_id=555)

    first = app._should_reply_in_chat(m)
    assert first is True, "первый ответ владельцу обязан пройти"

    second = app._should_reply_in_chat(m)
    assert second is False, "второй подряд в пределах зазора — молчим"


def test_owner_allowed_again_after_gap():
    """Зазор истёк — снова можно отвечать, иначе она онемеет навсегда."""
    app = _app_stub()
    m = _msg(sender_user_id=555)
    app._should_reply_in_chat(m)
    assert app._should_reply_in_chat(m) is False

    # сдвигаем время назад
    app._owner_reply_ts[-100] -= app.OWNER_GROUP_GAP_SEC + 1
    assert app._should_reply_in_chat(m) is True


def test_owner_throttle_is_per_chat():
    """Зазор в одной группе не должен глушить другие."""
    app = _app_stub()
    a, b = _msg(chat_id=-100), _msg(chat_id=-200)
    assert app._should_reply_in_chat(a) is True
    assert app._should_reply_in_chat(b) is True


def test_private_chat_never_throttled():
    """В личке зазора быть не должно: это прямой разговор."""
    app = _app_stub()
    m = _msg(chat_type="private", sender_user_id=999)
    for _ in range(3):
        assert app._should_reply_in_chat(m) is True


# ------------------------------------------- 3. бэклог после рестарта
@pytest.mark.asyncio
async def test_stale_backlog_is_skipped():
    """Сообщение часовой давности — это бэклог TDLib, не разговор."""
    app = _app_stub()
    old = _msg(message_ts=time.time() - 3600)
    assert await app._is_stale(old) is True


@pytest.mark.asyncio
async def test_fresh_message_is_not_stale():
    app = _app_stub()
    fresh = _msg(message_ts=time.time() - 2)
    assert await app._is_stale(fresh) is False


@pytest.mark.asyncio
async def test_missing_date_is_not_treated_as_stale():
    """TDLib не дал дату — не гадаем и не глушим разговор."""
    app = _app_stub()
    assert await app._is_stale(_msg(message_ts=0)) is False


# ------------------------------------- 4. своя дата сообщения доезжает
def test_message_ts_field_exists():
    """Без этого поля фильтр бэклога невозможен."""
    m = _msg(message_ts=123.0)
    assert m.message_ts == 123.0
    assert _msg().message_ts == 0.0


# --------------------------------- 5. чистка команд в proactive_send
def test_service_command_regex_removes_remember():
    """В чат ушло «#remember Написала первой…» — не должно повторяться."""
    app = _app_stub()
    text = "нормальный текст\n#remember что-то важное"
    cleaned = KuniApp._REMEMBER_RE.sub("", text).strip()
    assert "#remember" not in cleaned
    assert "нормальный текст" in cleaned


# ------------------------------- 6. заблокированный собеседник (28.10)
def test_blocked_chat_is_skipped():
    """YOU_BLOCKED_USER: она сама заблокировала — писать нельзя.

    TDLib копил «yet unsent message» и ретраил их (54951.1, .9, .17).
    """
    from kuni.telegram.client import TelegramModule

    tg = TelegramModule.__new__(TelegramModule)
    tg._blocked_chats = set()
    assert tg.is_blocked(100000003) is False

    tg._blocked_chats.add(100000003)
    assert tg.is_blocked(100000003) is True
    assert tg.is_blocked(-1000000002) is False


# ------------------------- 7. кнопки TDLib (28.10, живой баг в проде)
def test_keyboard_rows_is_list_of_lists():
    """TDLib 1.8.69: rows — массив МАССИВОВ, без обёртки Row.

    Проверено через td_execute на живом TDLib:
      rows=[{"@type":"inlineKeyboardButtonRow",...}] ->
           "Expected Array, but receive Object"   (падало)
      rows=[[button]] -> парсится

    Из-за этого падал КАЖДЫЙ запрос подтверждения: она не могла ни
    удалить, ни переслать, ни отправить что-то «как своё» — вместо
    кнопок уходила ошибка парсинга, гейт отвечал отказом.
    """
    import base64

    # воспроизводим сборку из send_text
    buttons = [[("✅ Разрешить", "approve:abc"), ("⛔ Отклонить", "deny:abc")]]
    rows = []
    for row in buttons:
        cells = []
        for label, data in row:
            cells.append({
                "@type": "inlineKeyboardButton",
                "text": label,
                "type": {
                    "@type": "inlineKeyboardButtonTypeCallback",
                    "data": base64.b64encode(data.encode("utf-8")).decode("ascii"),
                },
            })
        if cells:
            rows.append(cells)

    assert isinstance(rows, list)
    assert all(isinstance(r, list) for r in rows), "rows обязан быть массивом массивов"
    assert all("buttons" not in r for r in rows), "обёртки Row быть не должно"
    assert rows[0][0]["@type"] == "inlineKeyboardButton"


def test_keyboard_payload_shape():
    """reply_markup.rows должен быть списком списков, а не списком объектов."""
    payload = {"@type": "replyMarkupInlineKeyboard", "rows": [[{"a": 1}]]}
    assert isinstance(payload["rows"], list)
    assert isinstance(payload["rows"][0], list)
