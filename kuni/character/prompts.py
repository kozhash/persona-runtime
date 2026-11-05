"""Динамическая библиотека промптов.

Все промпты — обычные .md файлы в prompts/. При каждом обращении
проверяется mtime файла: правишь файл в блокноте — следующий же
вызов LLM берёт новую версию. Никаких перезапусков.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from string import Formatter

log = logging.getLogger(__name__)

_MISSING = object()


@dataclass
class _Entry:
    mtime: float
    text: str


class PromptLibrary:
    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self._cache: dict[str, _Entry] = {}
        self.misses: list[str] = []  # какие имена запрашивали, но не нашли

    def get(self, prompt_name: str, **variables: str) -> str:
        """Промпт по имени (файл prompts/<prompt_name>.md) с подстановкой {переменных}.

        Несуществующие переменные остаются как есть — промпт не ломается.
        Если файла нет — пустая строка и запись в лог (система деградирует мягко).
        """
        text = self.raw(prompt_name)
        if not text:
            return ""
        return render(text, **variables)

    def raw(self, prompt_name: str) -> str:
        path = self.directory / f"{prompt_name}.md"
        try:
            mtime = path.stat().st_mtime
        except OSError:
            if prompt_name not in self.misses:
                self.misses.append(prompt_name)
                log.warning("промпт не найден: %s", path)
            return ""
        cached = self._cache.get(prompt_name)
        if cached is not None and cached.mtime == mtime:
            return cached.text
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            log.exception("не смог прочитать промпт %s", path)
            return ""
        self._cache[prompt_name] = _Entry(mtime, text)
        log.debug("промпт загружен: %s (%d симв.)", prompt_name, len(text))
        return text

    def reload_all(self) -> int:
        """Сбросить кэш (используется тестами и при смене prompts_dir)."""
        n = len(self._cache)
        self._cache.clear()
        return n


class _SafeFormatter(Formatter):
    """str.format, который не падает на отсутствующих ключах."""

    def get_value(self, key, args, kwargs):
        if isinstance(key, str):
            return kwargs.get(key, "{" + key + "}")
        return super().get_value(key, args, kwargs)


def render(template: str, **variables: str) -> str:
    try:
        return _SafeFormatter().vformat(template, (), variables)
    except (ValueError, KeyError):
        # например одиночная "{" в тексте промпта — отдаём как есть
        return template
