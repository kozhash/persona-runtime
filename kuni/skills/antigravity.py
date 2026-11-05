"""Скилл antigravity: поручить техническое дело агенту Antigravity CLI (agy).

Она сама не пишет код руками — строго делегирует agy и возвращает выжимку.
"""
from __future__ import annotations

import asyncio
import logging
import shlex
from typing import Any

from . import SkillResult

log = logging.getLogger(__name__)


class AntigravitySkill:
    def __init__(self, root: Any, agy_command: str, timeout_minutes: float = 40.0,
                 bus: Any = None) -> None:
        self.root = root
        self.agy_command = agy_command or "agy --print={task}"
        self.timeout = timeout_minutes * 60
        self.bus = bus

    async def __call__(self, args: dict[str, Any]) -> SkillResult:
        task = str(args.get("task") or "").strip()
        if not task:
            return SkillResult(ok=False, output="пустая задача")
        template = self.agy_command
        argv = shlex.split(template, posix=False)
        argv = [str(a).replace("{task}", task) if "{task}" in a else a for a in argv]
        if "{task}" not in template:
            argv.append(task)
        if self.bus:
            self.bus.emit("terminal.line", text=f"$ agy task: {task[:80]}", kind="cmd")
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=str(self.root),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=self.timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            return SkillResult(ok=False, output="антигравити не уложился в лимит")
        except FileNotFoundError:
            return SkillResult(ok=False, output="agy не найден — установи Antigravity CLI")
        except Exception as e:
            return SkillResult(ok=False, output=f"запуск не вышел: {str(e)[:120]}")
        text = out.decode("utf-8", errors="replace")
        if self.bus:
            for line in text.strip().splitlines()[-8:]:
                self.bus.emit("terminal.line", text=line[:300], kind="out")
            self.bus.emit("terminal.line",
                          text="OK: agy закончил" if proc.returncode == 0 else f"FAIL: rc={proc.returncode}",
                          kind="ok" if proc.returncode == 0 else "err")
        if proc.returncode != 0:
            return SkillResult(ok=False, output=text[-1500:])
        # выжимка: последние содержательные строки
        tail = [l for l in text.strip().splitlines() if l.strip()][-25:]
        return SkillResult(ok=True, output="\n".join(tail)[-3000:])

    def skill(self) -> Any:
        from . import Skill
        return Skill(
            name="antigravity",
            description="Поручить техническую задачу (код, файлы, скрипты, деплой) "
                        "агенту Antigravity CLI — он делает, ты получаешь результат. "
                        "Свои руки для кода не используй, только этот инструмент.",
            parameters={
                "type": "object",
                "properties": {
                    "task": {"type": "string",
                             "description": "чёткое ТЗ одной строкой: что сделать и где"},
                },
                "required": ["task"],
            },
            execute=self.__call__,
        )
