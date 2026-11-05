"""Скилл «менять себя снаружи»: имя, био, юзернейм, аватар (рисует), сторисы."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from . import SkillResult

log = logging.getLogger(__name__)


class ProfileSkill:
    def __init__(self, telegram: Any, draw_skill: Any = None, bus: Any = None) -> None:
        self.telegram = telegram
        self.draw = draw_skill
        self.bus = bus

    async def _draw_file(self, prompt: str) -> Path | None:
        if self.draw is None or not prompt.strip():
            return None
        outcome = await self.draw.execute({"prompt": prompt})
        if outcome.ok and outcome.artifact_path:
            return Path(outcome.artifact_path)
        log.warning("profile: картинка не вышла: %s", outcome.output[:120])
        return None

    async def __call__(self, args: dict[str, Any]) -> SkillResult:
        action = str(args.get("action") or "").lower()
        try:
            if action == "set_name":
                await self.telegram.request({"@type": "setName",
                                             "first_name": str(args.get("first") or ""),
                                             "last_name": str(args.get("last") or "")})
                return SkillResult(ok=True, output="имя обновлено")
            if action == "set_bio":
                await self.telegram.request({"@type": "setBio",
                                             "bio": str(args.get("bio") or "")[:128]})
                return SkillResult(ok=True, output="био обновлено")
            if action == "set_username":
                await self.telegram.request({"@type": "setUsername",
                                             "username": str(args.get("username") or "")})
                return SkillResult(ok=True, output="юзернейм обновлён")
            if action == "set_avatar":
                path = await self._draw_file(str(args.get("prompt") or ""))
                if path is None:
                    return SkillResult(ok=False, output="нужен prompt и включённый draw")
                await self.telegram.request({
                    "@type": "setProfilePhoto",
                    "photo": {"@type": "inputChatPhotoStatic",
                              "photo": {"@type": "inputFileLocal", "path": str(path)}},
                })
                if self.bus:
                    self.bus.emit("profile.avatar", path=str(path))
                return SkillResult(ok=True, output="аватар обновлён")
            if action == "story":
                path = await self._draw_file(str(args.get("prompt") or ""))
                if path is None:
                    return SkillResult(ok=False, output="нужен prompt и включённый draw")
                await self.telegram.request({
                    "@type": "sendStory",
                    "content": {"@type": "inputStoryContentPhoto",
                                "photo": {"@type": "inputFileLocal", "path": str(path)}},
                    "areas": [],
                    "caption": {"@type": "formattedText",
                                "text": str(args.get("caption") or "")},
                    "privacy_settings": {"@type": "storyPrivacyAllowAll"},
                    "active_for": 86400,
                })
                if self.bus:
                    self.bus.emit("profile.story", path=str(path),
                                  caption=str(args.get("caption") or "")[:80])
                return SkillResult(ok=True, output="сторис опубликована (на 24ч)")
            return SkillResult(ok=False, output=f"неизвестное действие: {action}")
        except Exception as e:
            log.exception("profile: ошибка")
            return SkillResult(ok=False, output=f"не вышло: {str(e)[:140]}")

    def skill(self) -> Any:
        from . import Skill
        return Skill(
            name="profile",
            description="Менять свой профиль: set_name {first,last}, set_bio {bio}, "
                        "set_username {username}, set_avatar {prompt} (нарисует и поставит), "
                        "story {prompt, caption} — сторис на 24ч с нарисованной картинкой.",
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string",
                               "enum": ["set_name", "set_bio", "set_username",
                                        "set_avatar", "story"]},
                    "first": {"type": "string"}, "last": {"type": "string"},
                    "bio": {"type": "string"}, "username": {"type": "string"},
                    "prompt": {"type": "string", "description": "что нарисовать (аватар/сторис)"},
                    "caption": {"type": "string"},
                },
                "required": ["action"],
            },
            execute=self.__call__,
        )
