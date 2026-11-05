"""Telegram-модуль: жизнь одного юзер-аккаунта через tdlib.

Отвечает за: авторизацию (первый запуск — интерактивный вход),
приём updateNewMessage, приоритеты чатов (закреплённые важнее,
заминченные тише), отправку текста/фото/войсов, статусы человека.

Модуль ничего не знает про LLM: входящее сообщение уходит наружу
колбэком on_message, ответ возвращает вызывающий.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from .tdjson import TdError, TdJson, find_tdjson
from .human import HumanBehavior

log = logging.getLogger(__name__)

#: Служебные сообщения Telegram: это не реплики людей, в LLM их нельзя.
#: Раньше они попадали в общий else и уходили как «[PinMessage]».
SERVICE_IGNORED = frozenset({
    "messagePinMessage",
    "messageSupergroupChatCreate",
    "messageChatChangeTitle",
    "messageChatChangePhoto",
    "messageChatDeletePhoto",
    "messageChatAddMembers",
    "messageChatJoinByLink",
    "messageChatJoinByRequest",
    "messageChatUpgradeTo",
    "messageChatUpgradeFrom",
    "messageBasicGroupChatCreate",
    "messageChatSetTheme",
    "messageChatSetMessageAutoDeleteTime",
    "messageScreenshotTaken",
    "messageCall",
    "messageVideoChatStarted",
    "messageVideoChatEnded",
    "messageVideoChatScheduled",
    "messageProximityAlertTriggered",
    "messageCustomServiceAction",
    "messageUnsupported",
})


def _caption_text(content: dict[str, Any]) -> str:
    """Подпись медиа как текст. В tdlib это объект formattedText или None."""
    caption = content.get("caption")
    if isinstance(caption, dict):
        return caption.get("text") or ""
    return ""


@dataclass
class IncomingMessage:
    chat_id: int
    message_id: int
    sender_user_id: int
    text: str = ""
    is_voice: bool = False
    voice_file_id: int = 0
    is_photo: bool = False
    photo_file_id: int = 0
    is_sticker: bool = False
    sticker_file_id: int = 0
    is_reply: bool = False          # сообщение является ответом на другое
    reply_to_self: bool = False     # ответ адресован именно ей
    chat_title: str = ""
    chat_type: str = ""
    is_pinned: bool = False       # чат закреплён у НЕЁ
    is_muted: bool = False        # чат заминчен у НЕЁ
    is_channel: bool = False      # это канал (писать нельзя)
    is_mention: bool = False      # её упомянули через @
    #: человеческое имя отправителя (27.10): без него память не может
    #: отличить «сказал собеседник» от «сказал УзбекГПТ». Пустое — если
    #: TDLib не отдал (удалённый аккаунт, служебное сообщение).
    sender_name: str = ""
    ts: float = field(default_factory=time.time)
    #: 28.10: время СООБЩЕНИЯ из Telegram, а не момента обработки.
    #: Без него после рестарта она отвечает на бэклог часовой давности:
    #: 74 записи с одинаковым ts в интервале 0.14 с — это не разговор,
    #: это пачка из очереди TDLib.
    message_ts: float = 0.0


MessageHandler = Callable[[IncomingMessage], Awaitable[None]]
AuthPrompt = Callable[[str], Awaitable[str]]   # вопрос -> ответ (код/пароль)

#: (chat_id, message_id, sender_user_id, data) — нажатие inline-кнопки.
CallbackHandler = Callable[[int, int, int, str], Awaitable[None]]


class TelegramModule:
    def __init__(self, config_provider: Any, bus: Any,
                 data_dir: Path, interactive: bool = False) -> None:
        self.cfg_provider = config_provider
        self.bus = bus
        self.data_dir = data_dir
        self.interactive = interactive   # True при `kuni login` — можно спрашивать код
        self.td: TdJson | None = None
        self.human: HumanBehavior | None = None
        self.on_message: MessageHandler | None = None
        self.on_callback: CallbackHandler | None = None
        #: социальные события про неё: kicked / muted / restricted
        self.on_social: Callable[[dict], Awaitable[None]] | None = None
        self.me_user_id: int = 0
        self.phone_hint: str = ""      # `kuni login --phone ...` — не спрашивать номер
        self.chats: dict[int, dict[str, Any]] = {}      # кэш chat_id -> инфо
        #: кэш user_id -> имя. getUser бьёт в локальную БД TDLib, но
        #: лишний запрос на каждое сообщение в группе всё равно дорог.
        self._user_names: dict[int, str] = {}
        self.authorized = False
        self._auth_prompt: AuthPrompt | None = None
        self._auth_lock = asyncio.Lock()   # state-machine авторизации — строго по одному
        self._started_at = 0.0
        #: 28.10: чаты, куда писать нельзя из-за блокировки. TDLib отвечает
        #: YOU_BLOCKED_USER, а раньше это молча копилось в очередь отправки.
        self._blocked_chats: set[int] = set()

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> bool:
        cfg = self.cfg_provider.current
        if not cfg.telegram.enabled:
            log.info("telegram отключён в конфиге (standalone-режим)")
            return False
        lib = find_tdjson(cfg.telegram.tdjson_path)
        if lib is None:
            log.error(
                "tdjson не найден. Установи tdlib и укажи путь в config.toml "
                "[telegram].tdjson_path или переменной KUNI_TDJSON_PATH"
            )
            self.bus.emit("module.status", module="telegram", status="no_tdjson")
            return False
        self.td = TdJson(lib, on_update=self._handle_update)
        self.td.bind_loop(asyncio.get_running_loop())
        self.human = HumanBehavior(self.request, self.cfg_provider, self.bus)
        self._started_at = time.time()
        self.bus.emit("module.status", module="telegram", status="started")
        return True

    def stop(self) -> None:
        if self.td:
            self.td.close()
        self.bus.emit("module.status", module="telegram", status="stopped")

    async def request(self, data: dict, timeout: float = 60.0) -> dict:
        if self.td is None:
            raise TdError(-1, "telegram не запущен")
        return await self.td.request(data, timeout)

    # ------------------------------------------------------------ авторизация

    async def _ensure_authorized(self) -> None:
        """Прокручивает state-machine авторизации до готовности (однопоточно)."""
        assert self.td is not None
        async with self._auth_lock:
            await self._ensure_authorized_locked()

    async def _ensure_authorized_locked(self) -> None:
        for _ in range(1200):   # до 10 минут ожидания кода от пользователя
            state = await self.request({"@type": "getAuthorizationState"})
            st = state.get("@type")
            if st == "authorizationStateWaitTdlibParameters":
                cfg = self.cfg_provider.current
                self.data_dir.mkdir(parents=True, exist_ok=True)
                await self.request({
                    "@type": "setTdlibParameters",
                    "database_directory": str(self.data_dir / "tdlib"),
                    "files_directory": str(self.data_dir / "tdlib_files"),
                    "use_message_database": True,
                    "use_secret_chats": False,
                    "api_id": cfg.general.telegram_api_id,
                    "api_hash": cfg.general.telegram_api_hash,
                    "system_language_code": cfg.general.language,
                    "device_model": "Desktop",
                    "application_version": "persona-runtime",
                })
            elif st == "authorizationStateWaitDatabaseEncryptionKey":
                key = secrets.token_hex(16)
                await self.request({"@type": "checkDatabaseEncryptionKey", "encryption_key": key})
            elif st == "authorizationStateWaitPhoneNumber":
                phone = self.phone_hint or await self._ask("Номер телефона (в формате +7999...): ")
                self.phone_hint = ""
                await self.request({"@type": "setAuthenticationPhoneNumber", "phone_number": phone})
            elif st == "authorizationStateWaitCode":
                code = await self._ask("Код из Telegram: ")
                await self.request({"@type": "checkAuthenticationCode", "code": code})
            elif st == "authorizationStateWaitPassword":
                password = await self._ask("Облачный пароль (2FA): ")
                await self.request({"@type": "checkAuthenticationPassword", "password": password})
            elif st == "authorizationStateReady":
                self.authorized = True
                me = await self.request({"@type": "getMe"})
                self.me_user_id = me.get("id", 0)
                log.info("telegram: аккаунт %s (id=%s)", me.get("first_name"), self.me_user_id)
                self.bus.emit("module.status", module="telegram", status="authorized",
                              user_id=self.me_user_id)
                await self._load_chats()
                return
            elif st in ("authorizationStateClosed", "authorizationStateLoggingOut"):
                log.error("telegram: сессия закрыта")
                self.bus.emit("module.status", module="telegram", status="closed")
                return
            else:
                await asyncio.sleep(0.5)
        log.error("telegram: не дождались авторизации")

    async def _ask(self, question: str) -> str:
        if self._auth_prompt is not None:
            return await self._auth_prompt(question)
        if self.interactive:
            return input(question)
        # неинтерактивный запуск (фон/сервис): вопрос в файл, ответ ждём в файле
        auth_dir = self.data_dir.parent / "auth"
        auth_dir.mkdir(parents=True, exist_ok=True)
        question_file = auth_dir / "question.txt"
        answer_file = auth_dir / "answer.txt"
        question_file.write_text(question, encoding="utf-8")
        log.warning("ЖДУ ОТВЕТ: %s (запиши ответ в %s)", question.strip(), answer_file)
        print(f"\n[AUTH] {question.strip()}\n       запиши ответ в {answer_file}", flush=True)
        for _ in range(1200):   # до 10 минут
            if answer_file.exists():
                try:
                    answer = answer_file.read_text(encoding="utf-8").strip()
                finally:
                    answer_file.unlink(missing_ok=True)
                question_file.unlink(missing_ok=True)
                if answer:
                    return answer
            await asyncio.sleep(0.5)
        raise RuntimeError(f"ответ на «{question.strip()}» не пришёл (10 минут)")

    # -------------------------------------------------------------- чаты

    async def _load_chats(self) -> None:
        try:
            result = await self.request({"@type": "getChats", "limit": 100})
            for chat_id in result.get("chat_ids", []):
                await self._cache_chat(chat_id)
        except Exception:
            log.exception("не удалось загрузить список чатов")

    async def open_dm(self, user_id: int) -> int:
        """Открыть личный чат с человеком по user_id. 0 — не вышло.

        28.10: у неё НЕ БЫЛО этой возможности. Кэш чатов наполнялся только
        из входящих сообщений (`self.chats[chat_id] = info` в обработчике),
        поэтому она знала лишь тех, кто ей уже написал. Написать первой
        в новую личку было физически нельзя — нечем открыть чат.

        createPrivateChat идемпотентен: если переписка есть, вернёт её же.
        """
        if not user_id:
            return 0
        try:
            chat = await self.request({
                "@type": "createPrivateChat",
                "user_id": int(user_id),
                "force": False,
            })
            chat_id = int(chat.get("id") or 0)
            if chat_id:
                await self._cache_chat(chat_id)
                log.info("личка с %s открыта (chat_id=%s)", user_id, chat_id)
            return chat_id
        except TdError as e:
            log.warning("createPrivateChat(%s): %s", user_id, str(e)[:140])
            return 0

    async def list_dialogues(self, limit: int = 30) -> list[dict]:
        """Список чатов, которые она знает: id, название, тип, личка/группа.

        28.10: чтобы она могла выбрать, КОМУ написать, а не только отвечать
        тем, кто написал первым.
        """
        try:
            known = getattr(self, "chats", {}) or {}
            out: list[dict] = []
            for chat_id, info in known.items():
                if info.get("blocked"):
                    continue
                out.append({
                    "chat_id": int(chat_id),
                    "title": info.get("title") or str(chat_id),
                    "type": info.get("type") or "unknown",
                    "is_private": (info.get("type") in ("private", "secret")),
                })
            out.sort(key=lambda x: x["title"].lower())
            return out[:limit]
        except Exception:
            log.debug("список чатов недоступен", exc_info=True)
            return []

    async def get_user_name(self, user_id: int) -> str:
        """Имя пользователя по id. Кэш в памяти, пустая строка при неудаче.

        27.10: нужно для атрибуции памяти — эпизод должен знать, КТО
        сказал, иначе «люблю кофе» от собеседника и от бота сливаются в
        один факт без автора.
        """
        if not user_id:
            return ""
        # тесты собирают модуль через __new__ без __init__ — кэш может
        # отсутствовать, поэтому берём лениво, а не в конструкторе
        cache = getattr(self, "_user_names", None)
        if cache is None:
            cache = self._user_names = {}
        cached = cache.get(user_id)
        if cached is not None:
            return cached
        try:
            u = await self.request({"@type": "getUser", "user_id": user_id})
        except (TdError, Exception) as e:
            log.debug("getUser(%s): %s", user_id, str(e)[:80])
            cache[user_id] = ""
            return ""
        first = (u.get("first_name") or "").strip()
        last = (u.get("last_name") or "").strip()
        name = " ".join(x for x in (first, last) if x).strip()
        if not name:
            # без имени — хотя бы юзернейм, иначе непонятно, кто это
            names = u.get("usernames") or {}
            name = (names.get("active_username") or "").strip()
            if name:
                name = "@" + name.lstrip("@")
        cache[user_id] = name
        return name

    async def _cache_chat(self, chat_id: int) -> dict:
        try:
            chat = await self.request({"@type": "getChat", "chat_id": chat_id})
        except TdError as e:
            log.debug("getChat(%s): %s", chat_id, e)
            return {}
        pinned = any(
            (p.get("source", {}).get("chat_list", {}).get("@type") == "chatListMain" and p.get("is_pinned"))
            for p in chat.get("positions", [])
        )
        muted = (chat.get("notification_settings") or {}).get("mute_for", 0) > 0
        # ВАЖНО: chat["@type"] всегда "chat". Реальный тип чата лежит в
        # chat["type"]["@type"] и бывает chatTypePrivate / chatTypeBasicGroup /
        # chatTypeSupergroup. Из-за чтения не того поля все чаты получали
        # тип "chat", и гейтинг групп не мог работать.
        type_obj = chat.get("type") or {}
        raw_type = str(type_obj.get("@type") or "")
        chat_type = {
            "chatTypePrivate": "private",
            "chatTypeSecret": "private",
            "chatTypeBasicGroup": "group",
            "chatTypeSupergroup": "supergroup",
        }.get(raw_type, raw_type.replace("chatType", "").lower() or "unknown")
        info = {
            "title": chat.get("title") or "",
            "type": chat_type,
            "is_channel": bool(type_obj.get("is_channel")),
            "pinned": pinned,
            "muted": muted,
        }
        self.chats[chat_id] = info
        return info

    # ------------------------------------------------------------- апдейты

    def _handle_update(self, update: dict) -> None:
        """Вызывается в loop-потоке; переносим тяжёлую работу в задачи."""
        utype = update.get("@type", "")
        if utype == "updateAuthorizationState":
            asyncio.ensure_future(self._ensure_authorized())
        elif utype == "updateNewMessage":
            asyncio.ensure_future(self._process_new_message(update["message"]))
        elif utype == "updateNewCallbackQuery":
            asyncio.ensure_future(self._process_callback_query(update))
        elif utype in ("updateChatPosition", "updateChatNotificationSettings"):
            chat_id = update.get("chat_id")
            if chat_id:
                asyncio.ensure_future(self._cache_chat(chat_id))

    async def _process_new_message(self, message: dict) -> None:
        chat_id = message.get("chat_id", 0)
        sender_id = ((message.get("sender_id") or {}).get("user_id")
                     if (message.get("sender_id") or {}).get("@type") == "messageSenderUser" else 0)
        if sender_id == self.me_user_id:
            return  # свои сообщения не обрабатываем
        content = message.get("content", {})
        ctype = content.get("@type", "")
        text = ""
        is_voice, voice_id = False, 0
        is_photo, photo_id = False, 0
        is_sticker, sticker_id = False, 0
        if ctype == "messageText":
            text = (content.get("text") or {}).get("text", "")
        elif ctype == "messageVoiceNote":
            is_voice = True
            voice_id = (content.get("voice") or {}).get("id", 0)
            text = _caption_text(content)
        elif ctype == "messagePhoto":
            is_photo = True
            photo_id = (content.get("photo") or {}).get("id", 0)
            text = _caption_text(content)
        elif ctype == "messageSticker":
            is_sticker = True
            sticker_id = (content.get("sticker") or {}).get("sticker", {}).get("id", 0)
            text = "[стикер]"
        elif ctype == "messageAnimation":
            is_sticker = True          # гифки разбираем тем же путём, что стикеры
            sticker_id = (content.get("animation") or {}).get("animation", {}).get("id", 0)
            text = "[гифка]"
        elif ctype == "messageAnimatedEmoji":
            # анимированный эмодзи — это не текст, в LLM его как реплику не шлём
            is_sticker = True
            sticker_id = (content.get("animated_emoji") or {}).get("sticker", {}).get("id", 0)
            emoji = (content.get("animated_emoji") or {}).get("emoji", "")
            text = emoji or "[анимированный эмодзи]"
        elif ctype in ("messageDice", "messageGame"):
            text = "[игра]"
        elif ctype == "messagePoll":
            poll = content.get("poll") or {}
            question = ((poll.get("question") or {}).get("text") or "").strip()
            text = f"[опрос] {question}".strip()
        elif ctype == "messageContact":
            contact = content.get("contact") or {}
            text = f"[контакт] {contact.get('first_name', '')} {contact.get('last_name', '')}".strip()
        elif ctype == "messageLocation":
            text = "[геолокация]"
        elif ctype == "messageChatDeleteMember":
            # Её исключили из группы — или вышел кто-то другой.
            # Раньше это лежало в SERVICE_IGNORED и отбрасывалось: она
            # физически не могла заметить, что её выгнали, и потому мут
            # в двух группах прошёл без единой эмоции.
            removed = (content.get("user_id") or 0)
            if removed and removed == self.me_user_id:
                if self.on_social is not None:
                    asyncio.ensure_future(self.on_social({
                        "kind": "kicked",
                        "chat_id": chat_id,
                        "chat_title": (self.chats.get(chat_id) or {}).get("title", ""),
                        "actor_id": sender_id,
                        "message_id": message.get("id", 0),
                    }))
                log.info("её исключили из чата %s", chat_id)
                return
            log.debug("из чата %s вышел участник %s — не про неё",
                      chat_id, removed)
            return
        elif ctype in SERVICE_IGNORED:
            # Служебные события Telegram — не реплики людей.
            # Раньше они уходили в LLM как текст «[PinMessage]» и модель
            # пыталась на это отвечать. Такие апдейты просто пропускаем.
            log.debug("служебное сообщение %s в чате %s — пропускаю",
                      ctype, chat_id)
            return
        else:
            text = f"[{ctype.replace('message', '')}]"

        chat_info = self.chats.get(chat_id) or await self._cache_chat(chat_id)
        # разбираем reply_to: важно понять, отвечает ли человек ИМЕННО ей
        reply_to = message.get("reply_to") or {}
        reply_to_self = False
        if reply_to:
            replied = reply_to.get("message_id")
            if replied:
                try:
                    original = await self.request({
                        "@type": "getMessage", "chat_id": chat_id,
                        "message_id": replied,
                    })
                    sender = (original.get("sender_id") or {})
                    reply_to_self = (sender.get("@type") == "messageSenderUser"
                                     and sender.get("user_id") == self.me_user_id)
                except TdError:
                    reply_to_self = False
        sender_name = await self.get_user_name(sender_id)
        incoming = IncomingMessage(
            chat_id=chat_id, message_id=message.get("id", 0),
            sender_user_id=sender_id, sender_name=sender_name, text=text,
            is_voice=is_voice, voice_file_id=voice_id,
            is_photo=is_photo, photo_file_id=photo_id,
            is_sticker=is_sticker, sticker_file_id=sticker_id,
            is_reply=bool(reply_to),
            reply_to_self=reply_to_self,
            chat_title=chat_info.get("title", ""), chat_type=chat_info.get("type", ""),
            is_pinned=chat_info.get("pinned", False), is_muted=chat_info.get("muted", False),
            is_channel=chat_info.get("is_channel", False),
            message_ts=float(message.get("date") or 0),
        )
        self.bus.emit("telegram.message", chat_id=chat_id, sender=sender_id,
                      text=text[:120], chat_title=incoming.chat_title)
        if self.on_message is not None:
            try:
                await self.on_message(incoming)
            except Exception:
                log.exception("обработчик сообщения упал (чат %s)", chat_id)

    # ------------------------------------------------------------ отправка

    async def send_text(self, chat_id: int, text: str, reply_to: int = 0,
                        quote: str = "",
                        buttons: list[list[tuple[str, str]]] | None = None) -> int:
        """Отправить текст, опционально ответом на сообщение или с кнопками.

        TDLib 1.8+ не принимает `reply_to_message_id` — нужен объект
        `reply_to`. Со старым полем reply молча не работал, поэтому она
        никогда не отвечала цитатой.

        `buttons` — ряды пар (подпись, data). Data уходит в
        updateNewCallbackQuery как есть; TDLib ждёт base64.
        """
        msg: dict[str, Any] = {
            "@type": "sendMessage",
            "chat_id": chat_id,
            "input_message_content": {
                "@type": "inputMessageText",
                "text": {"@type": "formattedText", "text": text},
            },
        }
        if buttons:
            # 28.10: TDLib ждёт массив МАССИВОВ кнопок, без обёртки Row.
            # Проверено на живом 1.8.69 через td_execute:
            #   rows=[{"@type":"inlineKeyboardButtonRow",...}] ->
            #        "Expected Array, but receive Object"  (ПАДАЛО)
            #   rows=[[button]] -> парсится
            # Из-за этого падал каждый запрос подтверждения: она не могла
            # ни удалить, ни переслать, ни отправить что-то «как своё» —
            # вместо кнопок уходила ошибка парсинга, и гейт отвечал отказом.
            rows: list[list[dict[str, Any]]] = []
            for row in buttons:
                cells: list[dict[str, Any]] = []
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
            if rows:
                msg["reply_markup"] = {"@type": "replyMarkupInlineKeyboard", "rows": rows}
        if reply_to:
            reply: dict[str, Any] = {
                "@type": "inputMessageReplyToMessage",
                "message_id": reply_to,
            }
            if quote:
                reply["quote"] = {"@type": "formattedText", "text": quote[:200]}
            msg["reply_to"] = reply
        try:
            result = await self.request(msg)
        except TdError as e:
            # Нет прав писать: её замутили или ограничили в правах.
            #
            # ВАЖНО: TDLib отвечает кодом 400 с текстом «Not enough rights
            # to send text messages to the chat», а не 403. Проверка только
            # по коду не срабатывала, и ошибка улетала наверх — она не
            # знала, что её лишили голоса. Поэтому смотрим и код, и текст.
            message = (e.message or "").lower()
            no_rights = (
                e.code in (403, 400) and (
                    "not enough rights" in message
                    or "chat_write_forbidden" in message
                    or "have no rights" in message
                    or "not a member" in message
                    or "chat member" in message
                )
            )
            if no_rights and self.on_social is not None:
                asyncio.ensure_future(self.on_social({
                    "kind": "muted",
                    "chat_id": chat_id,
                    "chat_title": (self.chats.get(chat_id) or {}).get("title", ""),
                    "message_id": 0,
                    "error": str(e)[:200],
                }))
            # 28.10: она САМА заблокировала собеседника, но код продолжал
            # туда писать. TDLib копил «yet unsent message» и ретраил их
            # (54951.1, .9, .17 в чате 100000003). Помечаем чат, чтобы
            # больше не пытаться: блокировка — её решение, уважаем его.
            if ("you_blocked_user" in message or "user_is_blocked" in message):
                if chat_id in self.chats:
                    self.chats[chat_id]["blocked"] = True
                self._blocked_chats.add(chat_id)
                log.info("чат %s: собеседник заблокирован — больше не пишу",
                         chat_id)
            raise
        message_id = (result.get("messages") or [{}])[0].get("id", 0) if "messages" in result \
            else result.get("id", 0)
        self.bus.emit("telegram.sent", chat_id=chat_id, text=text[:120], message_id=message_id)
        return message_id

    async def _process_callback_query(self, update: dict) -> None:
        """Нажатие inline-кнопки: подтверждаем Telegram и отдаём наверх."""
        query_id = update.get("id")
        chat_id = update.get("chat_id", 0)
        message_id = update.get("message_id", 0)
        sender_id = update.get("sender_user_id", 0)
        payload = update.get("payload") or {}
        data = ""
        if payload.get("@type") == "callbackQueryPayloadData":
            raw = payload.get("data", "")
            try:
                data = base64.b64decode(raw).decode("utf-8", errors="replace")
            except Exception:
                data = ""
        # без ответа у клиента крутится спиннер на кнопке
        if query_id:
            try:
                await self.request({
                    "@type": "answerCallbackQuery",
                    "callback_query_id": query_id,
                }, timeout=10.0)
            except TdError:
                pass
        self.bus.emit("telegram.callback", chat_id=chat_id, data=data[:80])
        if self.on_callback is not None:
            try:
                await self.on_callback(chat_id, message_id, sender_id, data)
            except Exception:
                log.exception("обработчик кнопки упал (чат %s)", chat_id)

    async def edit_message_text(self, chat_id: int, message_id: int, text: str) -> bool:
        """Переписать текст уже отправленного сообщения (убрать кнопки после решения)."""
        try:
            await self.request({
                "@type": "editMessageText",
                "chat_id": chat_id,
                "message_id": message_id,
                "input_message_content": {
                    "@type": "inputMessageText",
                    "text": {"@type": "formattedText", "text": text},
                },
            })
            return True
        except TdError:
            return False

    async def send_photo_file(self, chat_id: int, path: Path, caption: str = "") -> int:
        result = await self.request({"@type": "sendMessage", "chat_id": chat_id, "input_message_content": {
            "@type": "inputMessagePhoto",
            "photo": {"@type": "inputFileLocal", "path": str(path)},
            "caption": {"@type": "formattedText", "text": caption},
        }})
        return result.get("id", 0)

    async def send_voice_file(self, chat_id: int, path: Path, duration_sec: int) -> int:
        result = await self.request({"@type": "sendMessage", "chat_id": chat_id, "input_message_content": {
            "@type": "inputMessageVoiceNote",
            "voice": {"@type": "inputFileLocal", "path": str(path)},
            "duration": max(1, duration_sec),
            "waveform": base64.b64encode(secrets.token_bytes(32)).decode(),
        }})
        return result.get("id", 0)

    async def download_file(self, file_id: int, chat_id: int = 0, message_id: int = 0) -> Path | None:
        """Скачивает file_id tdlib во временный файл и отдаёт путь."""
        try:
            result = await self.request({"@type": "downloadFile", "file_id": file_id,
                                         "priority": 32, "synchronous": True})
            local = (result.get("local") or {}).get("path", "")
            return Path(local) if local else None
        except TdError as e:
            log.warning("downloadFile: %s", e)
            return None

    async def get_chat_history(self, chat_id: int, limit: int = 20,
                               from_message_id: int = 0,
                               offset: int = 0) -> list[dict]:
        """История чата. TDLib отдаёт её от указанного сообщения.

        27.10: добавлены from_message_id и offset — без них нельзя
        посмотреть контекст разговора ни до, ни после.

        Семантика TDLib:
          offset < 0  — столько сообщений ДО from_message_id
          offset = 0  — начиная с from_message_id и назад
          offset > 0  — столько сообщений ПОСЛЕ from_message_id

        Порядок в ответе — от новых к старым (как отдаёт TDLib).
        """
        body: dict = {"@type": "getChatHistory", "chat_id": chat_id,
                      "limit": limit, "only_local": False}
        if from_message_id:
            body["from_message_id"] = from_message_id
            body["offset"] = offset
        result = await self.request(body)
        return result.get("messages", [])

    async def add_reaction(self, chat_id: int, message_id: int, emoji: str) -> bool:
        """Ставит эмодзи-реакцию на сообщение (двойная функция живого ответа)."""
        try:
            await self.request({
                "@type": "addMessageReaction",
                "chat_id": chat_id,
                "message_id": message_id,
                "reaction_type": {"@type": "reactionTypeEmoji", "emoji": emoji},
                "is_big": False,
            })
            self.bus.emit("telegram.reacted", chat_id=chat_id, emoji=emoji,
                          message_id=message_id)
            return True
        except TdError as e:
            log.warning("addMessageReaction не встала (%s/%s): %s", chat_id,
                        message_id, str(e)[:160])
            return False

    async def send_document(self, chat_id: int, path: Path, caption: str = "") -> int:
        """Отправить файл документом (не сжатым фото)."""
        result = await self.request({
            "@type": "sendMessage", "chat_id": chat_id,
            "input_message_content": {
                "@type": "inputMessageDocument",
                "document": {"@type": "inputFileLocal", "path": str(path)},
                "caption": {"@type": "formattedText", "text": caption[:1000]},
            },
        })
        message_id = (result.get("messages") or [{}])[0].get("id", 0) \
            if "messages" in result else result.get("id", 0)
        self.bus.emit("telegram.sent", chat_id=chat_id,
                      text=f"[файл] {path.name}", message_id=message_id)
        return message_id

    async def delete_messages(self, chat_id: int, message_ids: list[int],
                              revoke: bool = True) -> bool:
        """Удалить сообщения. revoke=True — у всех, а не только у себя."""
        if not message_ids:
            return False
        try:
            await self.request({
                "@type": "deleteMessages", "chat_id": chat_id,
                "message_ids": list(message_ids), "revoke": bool(revoke),
            })
            self.bus.emit("telegram.deleted", chat_id=chat_id,
                          message_ids=list(message_ids), revoke=revoke)
            return True
        except TdError as e:
            log.warning("deleteMessages не сработал (%s): %s", chat_id, str(e)[:160])
            return False

    async def get_message(self, chat_id: int, message_id: int) -> dict:
        try:
            return await self.request({
                "@type": "getMessage", "chat_id": chat_id,
                "message_id": message_id})
        except TdError as e:
            log.warning("getMessage (%s/%s): %s", chat_id, message_id, str(e)[:160])
            return {}

    async def forward_messages(self, chat_id: int, from_chat_id: int,
                               message_ids: list[int]) -> list[int]:
        """Обычный форвард — с пометкой, кто автор оригинала."""
        if not message_ids:
            return []
        result = await self.request({
            "@type": "forwardMessages", "chat_id": chat_id,
            "from_chat_id": from_chat_id, "message_ids": list(message_ids),
        })
        ids = [m.get("id", 0) for m in (result.get("messages") or [])]
        self.bus.emit("telegram.forwarded", chat_id=chat_id,
                      count=len(ids), hidden=False)
        return ids

    async def repost_message(self, chat_id: int, from_chat_id: int,
                             message_id: int) -> int:
        """Переслать содержимое как своё: без «переслано от…».

        Telegram не даёт подделать origin форварда: скрытый автор — это
        флаг самого автора, а не отправителя. Единственный честный способ
        убрать подпись — заново отправить то же содержимое от своего имени.
        Поэтому для текста это новый текст, для медиа — новое вложение.
        """
        message = await self.get_message(from_chat_id, message_id)
        if not message:
            return 0
        content = message.get("content") or {}
        ctype = content.get("@type", "")
        if ctype == "messageText":
            text = (content.get("text") or {}).get("text", "")
            if not text.strip():
                return 0
            new_id = await self.send_text(chat_id, text)
            self.bus.emit("telegram.forwarded", chat_id=chat_id, count=1, hidden=True)
            return new_id
        if ctype in ("messagePhoto", "messageDocument", "messageVideo",
                     "messageAnimation", "messageAudio"):
            caption = _caption_text(content)
            kind = ctype.replace("message", "").lower()
            file_id = ((content.get(kind) or {}).get("id")
                       or (content.get(kind) or {}).get(kind, {}).get("id", 0))
            if file_id:
                local = await self.download_file(file_id)
                if local and local.exists():
                    if ctype == "messagePhoto":
                        return await self.send_photo_file(chat_id, local, caption)
                    if ctype in ("messageVideo", "messageAnimation", "messageAudio"):
                        # видео/аудио отправляем документом: тот же файл, без подписи
                        return await self.send_document(chat_id, local, caption)
                    return await self.send_document(chat_id, local, caption)
            if caption.strip():
                return await self.send_text(chat_id, caption)
        if ctype == "messageSticker":
            return 0          # стикер без автора переслать нельзя, пропускаем
        if ctype == "messageVoiceNote":
            voice_id = (content.get("voice") or {}).get("id", 0)
            local = await self.download_file(voice_id) if voice_id else None
            if local and local.exists():
                return await self.send_document(chat_id, local, "")
        return 0

    async def search_public_chat(self, username: str) -> int | None:
        try:
            result = await self.request({"@type": "searchPublicChat", "username": username.lstrip("@")})
            return result.get("id")
        except TdError:
            return None

    def is_blocked(self, chat_id: int) -> bool:
        """Она заблокировала этот чат — писать туда бессмысленно."""
        return chat_id in self._blocked_chats

    def uptime(self) -> float:
        return time.time() - self._started_at if self._started_at else 0.0
