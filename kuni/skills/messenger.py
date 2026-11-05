"""Скилл `messenger`: удалять, править, пересылать сообщения.

Отдельно от `channel` (свой канал) и `reaction` (эмодзи): здесь — операции
над конкретными сообщениями в любом чате.

Про «переслать без отметки оригинала». Telegram не позволяет подделать
источник форварда: скрыть автора может только сам автор, поставив флаг
в своих настройках приватности. Отправитель не может это обойти — так
устроен протокол, и это не баг.

Поэтому `forward` с `hide_author=true` делает другое, честное: заново
отправляет то же содержимое от своего имени. Текст становится её текстом,
файл — её файлом, подписи «переслано от» нет. Разница видна только по
времени и отсутствию ссылки на оригинал.

Действия:
  delete   — удалить сообщения (у себя или у всех)
  edit     — изменить текст своего сообщения
  forward  — обычная пересылка (с пометкой автора)
  repost   — отправить содержимое как своё (без пометки)
  read     — прочитать сообщение/историю чата
  info     — что за сообщение (тип, автор, вложения)
"""
from __future__ import annotations

import logging
from typing import Any

from ..skills import Skill, SkillResult

log = logging.getLogger(__name__)


class MessengerSkill:
    def __init__(self, telegram: Any, approvals: Any = None,
                 bus: Any = None) -> None:
        self.telegram = telegram
        self.approvals = approvals
        self.bus = bus

    async def __call__(self, args: dict[str, Any]) -> SkillResult:
        action = str(args.get("action") or "").strip().lower()
        try:
            if action == "delete":
                return await self._delete(args)
            if action == "edit":
                return await self._edit(args)
            if action == "forward":
                return await self._forward(args)
            if action == "repost":
                return await self._repost(args)
            if action == "read":
                return await self._read(args)
            if action == "info":
                return await self._info(args)
            if action == "send":
                return await self._send(args)
            if action == "dm":
                return await self._dm(args)
            if action == "dialogues":
                return await self._dialogues(args)
            return SkillResult(ok=False,
                               output=f"неизвестное действие messenger: {action}")
        except Exception as e:
            log.exception("messenger упал на %s", action)
            return SkillResult(ok=False, output=f"ошибка messenger.{action}: {e}")

    # ------------------------------------------------------------- удаление

    async def _delete(self, args: dict[str, Any]) -> SkillResult:
        chat_id = int(args.get("chat_id") or 0)
        ids = self._ids(args)
        if not chat_id or not ids:
            return SkillResult(ok=False, output="нужны chat_id и message_ids")
        revoke = bool(args.get("revoke", True))
        if self.approvals is not None:
            verdict = await self.approvals.request(
                "messenger.delete",
                f"удалить {len(ids)} сообщ. в чате {chat_id}",
                f"чат: {chat_id}\nсообщения: {', '.join(str(i) for i in ids)}\n"
                f"{'у всех участников' if revoke else 'только у себя'}")
            if not verdict["allowed"]:
                return SkillResult(ok=False,
                                   output=f"удаление не разрешено: {verdict['reason']}")
        ok = await self.telegram.delete_messages(chat_id, ids, revoke)
        return SkillResult(ok=ok,
                           output=(f"удалила {len(ids)} сообщ."
                                   if ok else "телеграм отказал в удалении"))

    # ------------------------------------------------------------- правка

    async def _edit(self, args: dict[str, Any]) -> SkillResult:
        chat_id = int(args.get("chat_id") or 0)
        message_id = int(args.get("message_id") or 0)
        text = str(args.get("text") or "").strip()
        if not chat_id or not message_id or not text:
            return SkillResult(ok=False,
                               output="нужны chat_id, message_id и text")
        ok = await self.telegram.edit_message_text(chat_id, message_id, text)
        return SkillResult(ok=ok,
                           output=("поправила сообщение" if ok
                                   else "не вышло: чужое сообщение или уже удалено"))

    # ------------------------------------------------------------- пересылка

    async def _forward(self, args: dict[str, Any]) -> SkillResult:
        chat_id = int(args.get("chat_id") or 0)
        from_chat_id = int(args.get("from_chat_id") or 0)
        ids = self._ids(args)
        if not chat_id or not from_chat_id or not ids:
            return SkillResult(ok=False,
                               output="нужны chat_id, from_chat_id и message_ids")
        new_ids = await self.telegram.forward_messages(chat_id, from_chat_id, ids)
        return SkillResult(ok=bool(new_ids),
                           output=(f"переслала {len(new_ids)} сообщ. "
                                   f"(с пометкой автора)" if new_ids
                                   else "пересылка не удалась"))

    async def _repost(self, args: dict[str, Any]) -> SkillResult:
        """Содержимое как своё: без «переслано от»."""
        chat_id = int(args.get("chat_id") or 0)
        from_chat_id = int(args.get("from_chat_id") or 0)
        ids = self._ids(args)
        if not chat_id or not from_chat_id or not ids:
            return SkillResult(ok=False,
                               output="нужны chat_id, from_chat_id и message_ids")
        if self.approvals is not None:
            verdict = await self.approvals.request(
                "messenger.repost",
                f"отправить содержимое как своё в {chat_id}",
                f"из чата: {from_chat_id}\nсообщения: {', '.join(str(i) for i in ids)}\n"
                f"подписи «переслано от» не будет")
            if not verdict["allowed"]:
                return SkillResult(ok=False,
                                   output=f"не разрешено: {verdict['reason']}")
        sent = 0
        failed = 0
        for message_id in ids:
            new_id = await self.telegram.repost_message(chat_id, from_chat_id, message_id)
            if new_id:
                sent += 1
            else:
                failed += 1
        if not sent:
            return SkillResult(ok=False,
                               output=("не смогла переслать: стикеры и служебные "
                                       "сообщения так отправить нельзя"))
        detail = f"отправила как своё: {sent}"
        if failed:
            detail += f", не вышло: {failed}"
        return SkillResult(ok=True, output=detail)

    # ------------------------------------------------------- писать первой

    async def _send(self, args: dict[str, Any]) -> SkillResult:
        """Написать в известный чат от своего имени.

        28.10: раньше она умела ТОЛЬКО отвечать тем, кто написал первым.
        Написать самой было нечем — в messenger не было действия send,
        а в channel было (но там только свой канал).
        """
        chat_id = int(args.get("chat_id") or 0)
        text = str(args.get("text") or "").strip()
        if not chat_id or not text:
            return SkillResult(ok=False, output="нужны chat_id и text")
        if self.approvals is not None:
            verdict = await self.approvals.request(
                "messenger.send",
                f"написать в чат {chat_id}",
                f"чат: {chat_id}\nтекст: {text[:400]}")
            if not verdict["allowed"]:
                return SkillResult(ok=False,
                                   output=f"не разрешено: {verdict['reason']}")
        try:
            new_id = await self.telegram.send_text(chat_id, text)
            return SkillResult(ok=bool(new_id),
                               output=(f"отправила (id {new_id})" if new_id
                                       else "телеграм не принял сообщение"))
        except Exception as e:
            return SkillResult(ok=False, output=f"не отправилось: {e}")

    async def _dm(self, args: dict[str, Any]) -> SkillResult:
        """Открыть личку с человеком по user_id.

        28.10: раньше это было невозможно — кэш чатов заполнялся только
        входящими, и «написать человеку первому» упиралось в отсутствие
        chat_id. Здесь появляется сам chat_id через createPrivateChat.
        """
        user_id = int(args.get("user_id") or 0)
        if not user_id:
            return SkillResult(ok=False, output="нужен user_id")
        chat_id = await self.telegram.open_dm(user_id)
        if not chat_id:
            return SkillResult(ok=False,
                               output=f"не смогла открыть личку с {user_id}")
        text = str(args.get("text") or "").strip()
        if not text:
            return SkillResult(ok=True,
                               output=f"личка открыта, chat_id={chat_id}")
        if self.approvals is not None:
            verdict = await self.approvals.request(
                "messenger.dm",
                f"написать первым человеку {user_id}",
                f"user_id: {user_id}\nchat_id: {chat_id}\nтекст: {text[:400]}")
            if not verdict["allowed"]:
                return SkillResult(ok=False,
                                   output=f"не разрешено: {verdict['reason']}")
        try:
            await self.telegram.send_text(chat_id, text)
            return SkillResult(ok=True,
                               output=f"написала первой (chat_id={chat_id})")
        except Exception as e:
            return SkillResult(ok=False, output=f"не отправилось: {e}")

    async def _dialogues(self, args: dict[str, Any]) -> SkillResult:
        """Список чатов: кому она вообще может написать."""
        chats = await self.telegram.list_dialogues()
        if not chats:
            return SkillResult(ok=True, output="чатов не знаю")
        lines = [f"{'личка' if c['is_private'] else c['type']:<11} "
                 f"{c['chat_id']:>16}  {c['title'][:40]}" for c in chats]
        return SkillResult(ok=True, output="\n".join(lines))

    # ------------------------------------------------------------- чтение

    async def _read(self, args: dict[str, Any]) -> SkillResult:
        chat_id = int(args.get("chat_id") or 0)
        if not chat_id:
            return SkillResult(ok=False, output="нужен chat_id")
        message_id = int(args.get("message_id") or 0)
        if message_id:
            message = await self.telegram.get_message(chat_id, message_id)
            return self._describe(message, chat_id)
        limit = min(50, max(1, int(args.get("limit") or 20)))
        history = await self.telegram.get_chat_history(chat_id, limit)
        if not history:
            return SkillResult(ok=True, output="в чате пусто")
        lines: list[str] = []
        for message in reversed(history):
            content = message.get("content") or {}
            ctype = str(content.get("@type") or "")
            text = ""
            if ctype == "messageText":
                text = (content.get("text") or {}).get("text", "")
            elif ctype == "messageSticker":
                text = "[стикер]"
            elif ctype in ("messagePhoto", "messageDocument", "messageVideo"):
                text = f"[{ctype.replace('message', '').lower()}] " + \
                       ((content.get("caption") or {}).get("text") or "")
            else:
                text = f"[{ctype.replace('message', '')}]"
            sender = (message.get("sender_id") or {}).get("user_id", "?")
            lines.append(f"#{message.get('id')} [{sender}] {text[:300]}")
        return SkillResult(ok=True, output="\n".join(lines))

    async def _info(self, args: dict[str, Any]) -> SkillResult:
        chat_id = int(args.get("chat_id") or 0)
        message_id = int(args.get("message_id") or 0)
        if not chat_id or not message_id:
            return SkillResult(ok=False, output="нужны chat_id и message_id")
        message = await self.telegram.get_message(chat_id, message_id)
        return self._describe(message, chat_id)

    @staticmethod
    def _describe(message: dict[str, Any], chat_id: int) -> SkillResult:
        if not message:
            return SkillResult(ok=False, output="сообщение не найдено")
        content = message.get("content") or {}
        ctype = str(content.get("@type") or "")
        sender = message.get("sender_id") or {}
        author = sender.get("user_id") or sender.get("chat_id") or "?"
        parts = [
            f"чат: {chat_id}",
            f"id: {message.get('id')}",
            f"автор: {author}",
            f"тип: {ctype}",
            f"дата: {message.get('date')}",
        ]
        if ctype == "messageText":
            parts.append("текст: " + (content.get("text") or {}).get("text", "")[:500])
        return SkillResult(ok=True, output="\n".join(parts))

    @staticmethod
    def _ids(args: dict[str, Any]) -> list[int]:
        raw = args.get("message_ids")
        if isinstance(raw, list):
            ids = [int(x) for x in raw if str(x).strip().lstrip("-").isdigit()]
            if ids:
                return ids
        single = args.get("message_id")
        if single and str(single).strip().lstrip("-").isdigit():
            return [int(single)]
        return []

    # ------------------------------------------------------------- схема

    def skill(self) -> Skill:
        return Skill(
            name="messenger",
            description=(
                "Операции с сообщениями в телеграме: delete — удалить, "
                "edit — изменить своё, forward — переслать (с пометкой автора), "
                "repost — отправить содержимое как своё (без «переслано от»), "
                "read — прочитать сообщение или историю чата, info — детали сообщения, "
                "send — написать в известный чат, dm — открыть личку и написать "
                "первой человеку по user_id, dialogues — кому я могу писать. "
                "Удаление, repost, send и dm требуют подтверждения хозяина."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["delete", "edit", "forward", "repost",
                                 "read", "info", "send", "dm", "dialogues"],
                    },
                    "chat_id": {"type": "integer", "description": "куда (или где)"},
                    "from_chat_id": {"type": "integer",
                                     "description": "откуда брать при forward/repost"},
                    "message_id": {"type": "integer"},
                    "message_ids": {"type": "array", "items": {"type": "integer"}},
                    "text": {"type": "string",
                             "description": "новый текст для edit или текст для send/dm"},
                    "user_id": {"type": "integer",
                                "description": "кому открыть личку (dm)"},
                    "limit": {"type": "integer", "description": "сколько читать"},
                    "revoke": {"type": "boolean",
                               "description": "удалить у всех (по умолчанию да)"},
                },
                "required": ["action"],
            },
            execute=self,
        )
