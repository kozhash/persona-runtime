"""Совместимость: ночная консолидация через новый Consolidator.

Раньше здесь был `SleepCycle` с одним проходом «слей/перепиши/выброси».
Теперь сон трёхфазный (NREM / REM / FORGET) и живёт в
`kuni.memory.consolidation`. Этот модуль оставлен как обёртка, чтобы
`app.py` и внешний код продолжали работать.
"""
from __future__ import annotations

from typing import Any

from .consolidation import (ConsolidationStats, Consolidator,
                            estimate_importance)

#: Прежнее имя статистики сна
SleepStats = ConsolidationStats


class SleepCycle(Consolidator):
    """Совместимая обёртка: тот же вызов run(), новая механика внутри."""

    def __init__(self, diary: Any, store: Any, working: Any = None,
                 llm: Any = None, prompts: Any = None,
                 bus: Any | None = None) -> None:
        distill = build_distill(llm, prompts) if llm is not None else None
        super().__init__(
            store=store,
            working=working,
            embedder=getattr(diary, "embedder", None),
            distill=distill,
            bus=bus,
        )
        self.diary = diary
        self.llm = llm
        self.prompts = prompts


def build_distill(llm: Any, prompts: Any = None):
    """Собрать функцию дистилляции кластера для LLM.

    Промпт берётся из prompts/sleep_consolidator.md (он же используется
    как «инструкция по сну»), а формат ответа — строгий JSON.
    """

    async def distill(cluster: list) -> dict[str, Any]:
        instruction = ""
        if prompts is not None:
            instruction = prompts.get("sleep_consolidator") or ""
        if not instruction:
            instruction = (
                "Ты — процесс консолидации памяти во сне. Тебе дают записи дневника. "
                'Ответь СТРОГО JSON: {"action": "merge|keep|discard", "kind": '
                '"EVENT|FACT|ENTITY_DESCRIPTION|THOUGHT|OTHER", "text": "...", '
                '"reason": "..."}. Сливай записи об одном событии в одну, '
                "сохраняй факты, выбрасывай мусор. Пиши от первого лица, кратко."
            )
        listing = "\n".join(
            f"[{i + 1}] {e.text} (confidence={getattr(e, 'confidence', 0):.2f})"
            for i, e in enumerate(cluster)
        )
        return await llm.chat_json([
            {"role": "system", "content": instruction},
            {"role": "user", "content": f"Записи дневника:\n{listing}"},
        ], function_label="sleep")

    return distill


DEFAULT_CONSOLIDATION_PROMPT = (
    "Ты — процесс консолидации памяти во сне. Тебе дают записи дневника персонажа. "
    'Ответь СТРОГО JSON: {"action": "merge|keep|discard", "kind": "EVENT|FACT|'
    'ENTITY_DESCRIPTION|THOUGHT|OTHER", "text": "...", "reason": "..."}.'
)
