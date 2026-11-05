"""Рабочая память — data/working_memory.md.

Живёт 1-3 дня: мелочи, которые надо держать в голове прямо сейчас
(«обещала скинуть ссылку», «у Влада завтра собес»). Подмешивается
в каждый промпт, переживает рестарты (это просто файл), записи
старше max_age_days вычищаются при консолидации.
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path

log = logging.getLogger(__name__)

# строка с датой в начале: "2026-10-24 - купить молоко"
_DATE_LINE = re.compile(r"^\s*(\d{4}-\d{2}-\d{2})\s*[-–—:]*\s*(.+)$", re.M)


class WorkingMemory:
    def __init__(self, path: Path, bus: Any | None = None) -> None:
        self.path = Path(path)
        self.bus = bus
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text("", encoding="utf-8")

    def content(self) -> str:
        try:
            return self.path.read_text(encoding="utf-8")
        except OSError:
            return ""

    def replace(self, new_content: str, *, source: str = "llm") -> None:
        new_content = new_content.strip()
        self.path.write_text(new_content + ("\n" if new_content else ""), encoding="utf-8")
        if self.bus:
            self.bus.emit("working_memory.updated", source=source, chars=len(new_content))
        log.info("рабочая память обновлена (%s): %d симв.", source, len(new_content))

    def add_item(self, item: str) -> None:
        item = item.strip()
        if not item:
            return
        stamp = time.strftime("%Y-%m-%d")
        current = self.content().rstrip("\n")
        line = f"{stamp} - {item}"
        if line in current:
            return
        new = (current + "\n" + line).strip("\n") if current else line
        self.replace(new, source="add_item")

    def cleanup(self, max_age_days: int = 3) -> int:
        """Убирает строки старше N дней. Возвращает число удалённых."""
        threshold = time.time() - max_age_days * 86400
        kept: list[str] = []
        removed = 0
        for match in _DATE_LINE.finditer(self.content()):
            date_str, rest = match.group(1), match.group(2)
            try:
                ts = time.mktime(time.strptime(date_str, "%Y-%m-%d"))
            except ValueError:
                kept.append(f"{date_str} - {rest}")
                continue
            if ts >= threshold:
                kept.append(f"{date_str} - {rest}")
            else:
                removed += 1
        # строки без даты считаем свежими
        undated = [
            line for line in self.content().splitlines()
            if line.strip() and not _DATE_LINE.match(line)
        ]
        new_content = "\n".join(undated + kept)
        if new_content != self.content():
            self.replace(new_content, source="cleanup")
        return removed
