"""Базовые скиллы: память (записать/вспомнить), картинки, поиск.

Каждый скилл получает зависимости через конструктор — тестируется
на моках, ядро не знает деталей.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from string import Formatter
from typing import Any

import httpx

from . import Skill, SkillResult

log = logging.getLogger(__name__)


# ------------------------------------------------------------------ память

def memory_skills(diary: Any, working: Any) -> list[Skill]:
    """remember — записать в дневник; recall — вспомнить похожее;
    remember_now — положить в рабочую память (на пару дней)."""

    async def remember(args: dict[str, Any]) -> SkillResult:
        text = str(args.get("text") or "").strip()
        if not text:
            return SkillResult(ok=False, output="пустой текст записи")
        entry = await diary.add(text, tags=list(args.get("tags") or []), source="chat")
        return SkillResult(ok=True, output=f"запомнила (id={entry.entry_id})")

    async def recall(args: dict[str, Any]) -> SkillResult:
        query = str(args.get("query") or "").strip()
        if not query:
            return SkillResult(ok=False, output="пустой запрос")
        found = await diary.recall(query, k=int(args.get("limit") or 5))
        if not found:
            found = diary.recall_sync_fallback(query)
        if not found:
            return SkillResult(ok=True, output="ничего похожего в памяти нет")
        lines = [f"- ({f['when']}, похожесть {f.get('similarity', '-')}) {f['text']}" for f in found]
        return SkillResult(ok=True, output="Вот что вспоминаю:\n" + "\n".join(lines))

    async def remember_now(args: dict[str, Any]) -> SkillResult:
        item = str(args.get("item") or "").strip()
        if not item:
            return SkillResult(ok=False, output="пустая запись")
        working.add_item(item)
        return SkillResult(ok=True, output="держу это в голове ближайшие пару дней")

    return [
        Skill(
            name="remember",
            description="Записать в долгосрочную память (дневник) важный факт, событие или чувство. Пиши от первого лица, кратко.",
            parameters={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "что запомнить, от первого лица"},
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "тематические метки"},
                },
                "required": ["text"],
            },
            execute=remember,
        ),
        Skill(
            name="recall",
            description="Вспомнить из долгосрочной памяти всё похожее на запрос по смыслу.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "о чём вспомнить"},
                    "limit": {"type": "integer", "description": "сколько воспоминаний (по умолчанию 5)"},
                },
                "required": ["query"],
            },
            execute=recall,
        ),
        Skill(
            name="remember_now",
            description="Положить в краткосрочную память (на 1-3 дня): обещания, планы, мелочи «прямо сейчас».",
            parameters={
                "type": "object",
                "properties": {"item": {"type": "string", "description": "одно короткое напоминание себе"}},
                "required": ["item"],
            },
            execute=remember_now,
        ),
    ]


# ---------------------------------------------------------------- картинки

class ImageSkill:
    """Stable Diffusion с инженерией промпта и качественным гейтом.

    Два промпта, которые раньше лежали мёртвыми, теперь работают:
      * image_engineer_system / _instructions — превращают свободное
        описание в валидный SD-промпт (positive + negative);
      * image_assess_system — строгий критик: принимает картинку только
        если анатомия и композиция не врут.

    Без LLM скилл деградирует до простой генерации по исходному промпту.
    """

    MAX_ATTEMPTS = 2

    def __init__(self, url: str, steps: int = 25, timeout: float = 180.0,
                 llm: Any = None, prompts: Any = None,
                 character_name: str = "", appearance: str = "",
                 quality_gate: bool = True) -> None:
        self.url = url.rstrip("/")
        self.steps = steps
        self.timeout = timeout
        self.llm = llm
        self.prompts = prompts
        self.character_name = character_name
        self.appearance = appearance
        self.quality_gate = quality_gate

    async def _engineer(self, description: str) -> tuple[str, str]:
        """Свободное описание -> (positive, negative) для SD."""
        if self.llm is None or self.prompts is None:
            return description, ""
        system = self.prompts.get(
            "image_engineer_system",
            CHARACTER_NAME=self.character_name or "character",
            CHARACTER_APPEARANCE_PROMPT=self.appearance or description,
            SANITIZED_PHOTO_DESCRIPTION=description,
        )
        if not system:
            return description, ""
        try:
            data = await self.llm.chat_json(
                [{"role": "system", "content": system},
                 {"role": "user", "content": description}],
                function_label="image_engineer",
            )
        except Exception as log_exc:      # noqa: BLE001 - деградируем молча
            log.warning("инженерия промпта не удалась: %s", str(log_exc)[:120])
            return description, ""
        positive = str(data.get("positivePrompt") or "").strip() or description
        negative = str(data.get("negativePrompt") or "").strip()
        return positive, negative

    async def _assess(self, image_b64: str, description: str) -> tuple[bool, str]:
        """Строгий гейт: (принято, замечания). Без vision — всегда принято."""
        if not self.quality_gate or self.llm is None or self.prompts is None:
            return True, ""
        instruction = self.prompts.get("image_assess_system") or ""
        if not instruction:
            return True, ""
        try:
            result = await self.llm.chat(
                [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": instruction},
                        {"type": "image_url",
                         "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
                    ],
                }],
                function_label="image_assess",
                temperature=0.1,
            )
            from ..llm.client import extract_json
            data = extract_json(result.content)
        except Exception as e:
            log.warning("критик картинки не ответил: %s", str(e)[:120])
            return True, ""
        satisfied = bool(data.get("satisfied"))
        return satisfied, str(data.get("feedback") or "")

    async def generate(self, prompt: str) -> SkillResult:
        """Полный цикл: инженерия -> генерация -> критика -> (ретрай)."""
        import base64
        from pathlib import Path
        prompt = (prompt or "").strip()
        if not prompt:
            return SkillResult(ok=False, output="пустой prompt")

        positive, negative = await self._engineer(prompt)
        feedback = ""
        for attempt in range(self.MAX_ATTEMPTS):
            body: dict[str, Any] = {
                "prompt": positive, "steps": self.steps,
                "width": 768, "height": 768, "cfg_scale": 6.5,
            }
            if negative:
                body["negative_prompt"] = negative
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    resp = await client.post(f"{self.url}/sdapi/v1/txt2img", json=body)
                    resp.raise_for_status()
                    data = resp.json()
            except httpx.HTTPError as e:
                return SkillResult(ok=False, output=f"Stable Diffusion недоступен: {e}")
            images = data.get("images") or []
            if not images:
                return SkillResult(ok=False, output="SD вернул пусто")

            accepted, feedback = await self._assess(images[0], prompt)
            if accepted or attempt == self.MAX_ATTEMPTS - 1:
                out_dir = Path("data/images")
                out_dir.mkdir(parents=True, exist_ok=True)
                safe = re.sub(r"[^\w\-]+", "_", prompt[:40]).strip("_") or "img"
                path = out_dir / f"{safe}.png"
                path.write_bytes(base64.b64decode(images[0]))
                note = "" if accepted else f" (критик не принял: {feedback[:80]})"
                return SkillResult(ok=True, output="картинка готова" + note,
                                   artifact_path=str(path))
            # не приняли — правим промпт по замечаниям
            instructions = self.prompts.get("image_engineer_instructions") or ""
            if not instructions or self.llm is None:
                break
            try:
                data_fix = await self.llm.chat_json(
                    [{"role": "system", "content": instructions},
                     {"role": "user", "content":
                      f"positive: {positive}\nnegative: {negative}\n"
                      f"Проблемы: {feedback}"}],
                    function_label="image_engineer_fix",
                )
                positive = str(data_fix.get("positivePrompt") or positive)
                negative = str(data_fix.get("negativePrompt") or negative)
            except Exception:
                break
        return SkillResult(ok=False, output=f"не вышло после {self.MAX_ATTEMPTS} попыток: {feedback[:120]}")

    async def __call__(self, args: dict[str, Any]) -> SkillResult:
        return await self.generate(str(args.get("prompt") or ""))

    def skill(self) -> Skill:
        return Skill(
            name="draw",
            description="Нарисовать картинку по описанию (Stable Diffusion) и отправить её в чат.",
            parameters={
                "type": "object",
                "properties": {"prompt": {"type": "string", "description": "что нарисовать, можно по-английски"}},
                "required": ["prompt"],
            },
            execute=self.__call__,
        )


# ------------------------------------------------------------------ поиск

class WebSearchSkill:
    def __init__(self, timeout: float = 30.0) -> None:
        self.timeout = timeout

    async def __call__(self, args: dict[str, Any]) -> SkillResult:
        query = str(args.get("query") or "").strip()
        if not query:
            return SkillResult(ok=False, output="пустой запрос")
        try:
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
                resp = await client.get(
                    "https://html.duckduckgo.com/html/",
                    params={"q": query},
                    headers={"User-Agent": "Mozilla/5.0"},
                )
                resp.raise_for_status()
        except httpx.HTTPError as e:
            return SkillResult(ok=False, output=f"поиск недоступен: {e}")
        results = self._parse(resp.text)
        if not results:
            return SkillResult(ok=True, output="поиск ничего не дал")
        lines = [f"- {t}: {u}\n  {s}" for t, u, s in results[:6]]
        return SkillResult(ok=True, output="\n".join(lines))

    @staticmethod
    def _parse(html: str) -> list[tuple[str, str, str]]:
        results = []
        for m in re.finditer(
            r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?class="result__snippet"[^>]*>(.*?)</a>',
            html, re.S,
        ):
            url, title, snippet = (re.sub(r"<[^>]+>", "", x).strip() for x in m.groups())
            results.append((title[:120], url[:200], snippet[:200]))
        return results

    def skill(self) -> Skill:
        return Skill(
            name="web_search",
            description="Поискать в интернете свежую информацию (новости, факты, ссылки).",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string", "description": "поисковый запрос"}},
                "required": ["query"],
            },
            execute=self.__call__,
        )
