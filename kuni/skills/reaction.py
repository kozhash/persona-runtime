"""Скилл реакций: поставить эмодзи-реакцию вместо ответа.

Почему инструмент, а не текстовая команда `#react`:
текстовую команду модель почти не использует — она «забывает» про неё,
когда есть обычный ответ. Инструмент в списке function-calling виден
в каждом вызове и вызывается естественно.

Экономика: реакция дешевле реплики и уместнее в трёх случаях —
подтверждение без слов, шутка, на которую нечего добавить, и
«видела, но отвечать не буду».
"""
from __future__ import annotations

import logging
from typing import Any

from . import Skill, SkillResult

log = logging.getLogger(__name__)

#: Разрешённые реакции. Telegram принимает не любые эмодзи.
ALLOWED_REACTIONS = (
    "👍", "👎", "❤️", "🔥", "🥰", "👏", "😁", "🤔", "🤯", "😱", "🤬",
    "😢", "🎉", "🤩", "🤮", "💩", "🙏", "👌", "🤡", "🥱", "🥴", "😍",
    "🌚", "💯", "🤣", "⚡", "💔", "🤨", "😐", "💋", "😈", "😴", "😭",
    "👻", "👀", "🗿", "😘", "😎", "👾", "🤷", "😡",
)


class ReactionSkill:
    """Ставит реакцию на сообщение собеседника."""

    def __init__(self, telegram: Any, bus: Any = None) -> None:
        self.telegram = telegram
        self.bus = bus
        self._last_message_id: int = 0
        self._last_chat_id: int = 0

    def remember_message(self, chat_id: int, message_id: int) -> None:
        """Запомнить сообщение, на которое можно реагировать.

        Вызывается конвейером перед tool-циклом: модель не знает
        message_id, но инструмент должен знать, куда ставить реакцию.
        """
        self._last_chat_id = chat_id
        self._last_message_id = message_id

    async def __call__(self, args: dict[str, Any]) -> SkillResult:
        emoji = str(args.get("emoji") or "").strip()
        if not emoji:
            return SkillResult(ok=False, output="пустой эмодзи")
        # нормализация: убираем вариационные селекторы, оставляем базу
        emoji = emoji.replace("\ufe0f", "") or "👍"
        if not self._last_message_id:
            return SkillResult(ok=False, output="не на что реагировать")
        try:
            ok = await self.telegram.add_reaction(
                self._last_chat_id, self._last_message_id, emoji)
        except Exception as e:
            log.warning("реакция не встала: %s", str(e)[:120])
            return SkillResult(ok=False, output=f"реакция не встала: {str(e)[:100]}")
        if not ok:
            return SkillResult(ok=False, output="telegram отклонил реакцию")
        if self.bus is not None:
            self.bus.emit("reaction.sent", chat_id=self._last_chat_id,
                          emoji=emoji, message_id=self._last_message_id)
        return SkillResult(ok=True, output=f"поставила реакцию {emoji}")

    def skill(self) -> Skill:
        return Skill(
            name="react",
            description=(
                "Поставить эмодзи-реакцию на сообщение собеседника вместо ответа. "
                "Используй, когда слов не нужно: подтверждение, смешная шутка, "
                "«видела», пренебрежение. Это дешевле и человечнее короткой реплики."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "emoji": {
                        "type": "string",
                        "description": "один эмодзи из разрешённых: "
                                       + " ".join(ALLOWED_REACTIONS[:20]),
                    },
                },
                "required": ["emoji"],
            },
            execute=self.__call__,
        )
