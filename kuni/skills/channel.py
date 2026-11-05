"""Скилл «вести свой канал»: создать, постить текст/картинки, посмотреть инфо.

Канал создаётся её аккаунтом при первом использовании и запоминается
в data/channel.json. Картинки — через её скилл draw (Stable Diffusion).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from . import SkillResult

log = logging.getLogger(__name__)


class ChannelSkill:
    def __init__(self, telegram: Any, data_dir: Path, draw_skill: Any = None,
                 bus: Any = None) -> None:
        self.telegram = telegram
        self.draw = draw_skill
        self.bus = bus
        self.state_file = Path(data_dir) / "channel.json"

    def _load(self) -> dict:
        if self.state_file.exists():
            try:
                return json.loads(self.state_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
        return {}

    def _save(self, state: dict) -> None:
        self.state_file.write_text(json.dumps(state, ensure_ascii=False, indent=1),
                                   encoding="utf-8")

    async def _ensure_channel(self, title: str = "") -> int:
        state = self._load()
        if state.get("chat_id"):
            return int(state["chat_id"])
        result = await self.telegram.request({
            "@type": "createNewSupergroupChat",
            "title": title or "жизнь на своих условиях",
            "is_channel": True,
            "description": "личный канал",
        })
        chat_id = result.get("id") or result.get("chat_id")
        if not chat_id:
            raise RuntimeError(f"канал не создан: {str(result)[:120]}")
        self._save({"chat_id": chat_id, "created": True})
        log.info("её канал создан: chat_id=%s", chat_id)
        return int(chat_id)

    async def __call__(self, args: dict[str, Any]) -> SkillResult:
        action = str(args.get("action") or "post").lower()
        try:
            if action == "info":
                state = self._load()
                if not state.get("chat_id"):
                    return SkillResult(ok=True, output="канала ещё нет — создастся при первом посту")
                return SkillResult(ok=True, output=json.dumps(state, ensure_ascii=False))
            if action == "post":
                text = str(args.get("text") or "").strip()
                if not text:
                    return SkillResult(ok=False, output="пустой пост")
                chat_id = await self._ensure_channel(str(args.get("title") or ""))
                await self.telegram.send_text(chat_id, text)
                if self.bus:
                    self.bus.emit("channel.posted", chat_id=chat_id, text=text[:120])
                return SkillResult(ok=True, output="запостила в канал")
            if action == "photo":
                prompt = str(args.get("prompt") or "").strip()
                caption = str(args.get("caption") or "").strip()
                if not prompt or self.draw is None:
                    return SkillResult(ok=False, output="нужен prompt (и включённый draw)")
                chat_id = await self._ensure_channel()
                outcome = await self.draw.execute({"prompt": prompt})
                if not outcome.ok or not outcome.artifact_path:
                    return SkillResult(ok=False, output=f"картинка не вышла: {outcome.output[:120]}")
                await self.telegram.send_photo_file(chat_id, Path(outcome.artifact_path), caption)
                if self.bus:
                    self.bus.emit("channel.posted", chat_id=chat_id, text=f"[фото] {caption}"[:120])
                return SkillResult(ok=True, output="пост с картинкой в канале")
            return SkillResult(ok=False, output=f"неизвестное действие: {action}")
        except Exception as e:
            log.exception("канал: ошибка")
            return SkillResult(ok=False, output=f"не вышло: {str(e)[:140]}")

    def skill(self) -> Any:
        from . import Skill
        return Skill(
            name="channel",
            description="Вести свой телеграм-канал: post {text} — запостить, "
                        "photo {prompt, caption} — пост с нарисованной картинкой, info — состояние.",
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["post", "photo", "info"]},
                    "text": {"type": "string"},
                    "prompt": {"type": "string", "description": "что нарисовать для photo-поста"},
                    "caption": {"type": "string"},
                    "title": {"type": "string", "description": "название канала при создании"},
                },
                "required": ["action"],
            },
            execute=self.__call__,
        )
