"""Скиллы — LLM-инструменты (function calling).

Каждый скилл: имя, описание, JSON-схема аргументов и async-выполнение.
Скиллы регистрируются в реестре и собираются в OpenAI tools-спеку.
Добавление нового скилла = один файл-класс, ядро не трогается.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)


@dataclass
class SkillResult:
    ok: bool
    output: str
    artifact_path: str = ""   # например, путь к сгенерированной картинке


Executor = Callable[[dict[str, Any]], Awaitable[SkillResult]]


@dataclass
class Skill:
    name: str
    description: str
    parameters: dict[str, Any]
    execute: Executor

    def to_openai_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class SkillRegistry:
    def __init__(self, bus: Any | None = None) -> None:
        self._skills: dict[str, Skill] = {}
        self.bus = bus

    def register(self, skill: Skill) -> None:
        self._skills[skill.name] = skill
        log.debug("скилл зарегистрирован: %s", skill.name)

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def names(self) -> list[str]:
        return list(self._skills)

    def openai_tools(self, only: list[str] | None = None) -> list[dict[str, Any]]:
        names = only if only else list(self._skills)
        return [self._skills[n].to_openai_tool() for n in names if n in self._skills]

    async def dispatch(self, name: str, arguments: dict[str, Any]) -> SkillResult:
        skill = self.get(name)
        if skill is None:
            return SkillResult(ok=False, output=f"неизвестный инструмент: {name}")
        if self.bus:
            self.bus.emit("tool.called", name=name, arguments=arguments)
        try:
            result = await skill.execute(arguments or {})
            log.info("скилл %s -> ok=%s: %s", name, result.ok, result.output[:120])
            if self.bus:
                self.bus.emit("tool.result", name=name, ok=result.ok,
                              output=result.output[:500],
                              artifact=result.artifact_path or "")
            return result
        except Exception as e:
            log.exception("скилл %s упал", name)
            if self.bus:
                self.bus.emit("tool.result", name=name, ok=False, output=str(e)[:500])
            return SkillResult(ok=False, output=f"ошибка инструмента {name}: {e}")


def make_skill(name: str, description: str, parameters: dict[str, Any]) -> Callable[[Executor], Executor]:
    """Декоратор-сахар: @make_skill('x', '...', schema) над async-функцией."""
    def decorator(fn: Executor) -> Executor:
        REGISTRY_PLACEHOLDER.append(Skill(name, description, parameters, fn))
        return fn
    return decorator


REGISTRY_PLACEHOLDER: list[Skill] = []   # используется тестами; основной путь — SkillRegistry
