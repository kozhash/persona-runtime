"""Скилл `shell`: выполнить команду на машине хозяина.

Это самый опасный инструмент из трёх, поэтому правил больше, чем кода:

1. Только allowlist. Не «запрещено всё, кроме», а «разрешено ровно это».
   Команда, которой нет в списке, не выполняется даже с одобрением —
   иначе кнопка «разрешить» превращается в безусловный shell.
2. Одобрение обязательно. Модель не может запустить ничего сама:
   владелец видит точную команду и жмёт кнопку.
3. Таймаут и убийство дерева. `pytest` может висеть, `npm` — оставить
   детей; `taskkill /T` убирает всех.
4. Аудит. Каждая команда, согласие и результат — в jsonl. Если что-то
   пойдёт не так, будет видно, что именно и когда.
5. Никаких конструкций с побочными эффектами: `;`, `&&`, `|`, `>`,
   обратные кавычки, `$(`. Они позволяют спрятать что угодно за
   безобидным префиксом.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import time
from pathlib import Path
from typing import Any

from ..skills import Skill, SkillResult
from .paths import PathDenied, PathPolicy

log = logging.getLogger(__name__)

#: Разрешённые исполняемые файлы. Ключ — имя, значение — как проверять
#: аргументы (None = любые в пределах рабочего каталога).
ALLOWED_COMMANDS: dict[str, str | None] = {
    "git": None,          # status/diff/log/show/add/commit — но не push
    "python": None,
    "python3": None,
    "pytest": None,
    "pip": "install-list",   # только list/show/freeze, установка — нет
    "node": None,
    "npm": "test-only",
    "npx": "test-only",
    "dir": None,
    "ls": None,
    "type": None,
    "cat": None,
    "rg": None,
    "findstr": None,
    "where": None,
    "echo": None,
}

#: Подстроки, при которых команда отклоняется до всякого одобрения.
FORBIDDEN_FRAGMENTS = (
    "rm -rf", "rmdir /s", "del /f", "del /q", "format ", "diskpart",
    "shutdown", "restart-computer", "bcdedit", "reg delete", "reg add",
    "net user", "net localgroup", "cipher /w", "vssadmin",
    "git push", "git reset --hard", "git clean",
    "curl ", "wget ", "invoke-webrequest", "iwr ", "start-process",
    "powershell -enc", "-encodedcommand", "cmd /c", "schtasks",
    ">", ">>", "|", "&&", "||", ";", "`", "$(",
)

#: Опасные аргументы git, которые меняют историю или льют наружу.
GIT_BLOCKED_SUBCOMMANDS = ("push", "clean", "reset", "rebase", "filter-branch")

MAX_OUTPUT_CHARS = 8000


class ShellSkill:
    def __init__(self, policy: PathPolicy, cfg: Any, approvals: Any = None,
                 data_dir: Path | None = None, bus: Any = None) -> None:
        self.policy = policy
        self.cfg = cfg
        self.approvals = approvals
        self.data_dir = data_dir or Path("data")
        self.bus = bus

    # --------------------------------------------------------------- вызов

    async def __call__(self, args: dict[str, Any]) -> SkillResult:
        command = (args.get("command") or "").strip()
        if not command:
            return SkillResult(ok=False, output="не передана команда")

        try:
            workdir = self.policy.resolve(args.get("workdir") or ".")
        except PathDenied as e:
            return SkillResult(ok=False, output=f"отказ: {e}")
        if not workdir.is_dir():
            return SkillResult(ok=False, output=f"не каталог: {workdir}")

        verdict = self._check_command(command)
        if verdict is not None:
            self._audit({"event": "refused", "command": command[:500],
                         "reason": verdict, "workdir": str(workdir)})
            return SkillResult(ok=False, output=f"команда отклонена: {verdict}")

        if self.approvals is not None and self.cfg.require_approval:
            answer = await self.approvals.request(
                "shell", "выполнить команду на компьютере",
                f"{command}\n\nкаталог: {workdir}")
            if not answer["allowed"]:
                self._audit({"event": "denied", "command": command[:500]})
                return SkillResult(ok=False,
                                   output=f"не разрешено: {answer['reason']}")

        return await self._execute(command, workdir)

    # ------------------------------------------------------------- политика

    def _check_command(self, command: str) -> str | None:
        """Вернуть причину отказа или None, если можно."""
        lowered = command.lower()

        for fragment in FORBIDDEN_FRAGMENTS:
            if fragment in lowered:
                return f"содержит запрещённое «{fragment.strip()}»"

        try:
            parts = shlex.split(command, posix=False)
        except ValueError as e:
            return f"не разобрать команду: {e}"
        if not parts:
            return "пустая команда"

        program = Path(parts[0].strip('"')).name.lower()
        if program.endswith(".exe"):
            program = program[:-4]
        if program not in ALLOWED_COMMANDS:
            return (f"«{program}» нет в allowlist "
                    f"(разрешено: {', '.join(sorted(ALLOWED_COMMANDS))})")

        rule = ALLOWED_COMMANDS[program]
        sub = parts[1].lower() if len(parts) > 1 else ""

        # `python -c` и `node -e` — это произвольный код внутри кавычек:
        # гейт по имени программы их не проверяет, а смысл в них любой.
        # Запрещено; для скрипта пусть сначала сохранит файл через fs.
        if program in ("python", "python3") and sub in ("-c", "-m"):
            return ("python -c/-m обходит проверку команд — "
                    "сохрани скрипт файлом и запусти python script.py")
        if program == "node" and sub in ("-e", "--eval", "-p", "--print"):
            return "node -e обходит проверку команд — сохрани скрипт файлом"

        if program == "git" and sub in GIT_BLOCKED_SUBCOMMANDS:
            return f"git {sub} запрещён (меняет историю или льёт наружу)"
        if rule == "install-list" and sub not in ("list", "show", "freeze", "--version", "-v"):
            return "pip: разрешены только list/show/freeze"
        if rule == "test-only" and sub not in ("test", "run", "ci", "--version", "-v"):
            return f"{program}: разрешены только test/run/ci"
        return None

    # ------------------------------------------------------------ исполнение

    async def _execute(self, command: str, workdir: Path) -> SkillResult:
        started = time.time()
        timeout = max(5.0, float(self.cfg.shell_timeout_seconds))
        self._audit({"event": "run", "command": command[:500], "workdir": str(workdir)})
        if self.bus is not None:
            self.bus.emit("harness.shell_run", command=command[:200])

        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *shlex.split(command, posix=False),
                cwd=str(workdir),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                await self._kill(proc)
                self._audit({"event": "timeout", "command": command[:500],
                             "seconds": int(timeout)})
                return SkillResult(
                    ok=False,
                    output=f"команда не уложилась в {int(timeout)}с и была остановлена")
        except FileNotFoundError:
            return SkillResult(ok=False, output=f"нет такого исполняемого файла: {command.split()[0]}")
        except Exception as e:
            log.exception("shell упал")
            return SkillResult(ok=False, output=f"ошибка запуска: {e}")

        elapsed = time.time() - started
        out = (stdout or b"").decode("utf-8", errors="replace")
        err = (stderr or b"").decode("utf-8", errors="replace")
        code = proc.returncode

        self._audit({"event": "finish", "command": command[:500], "code": code,
                     "seconds": round(elapsed, 1), "out_len": len(out)})

        body = out.strip()
        if len(body) > MAX_OUTPUT_CHARS:
            body = body[:MAX_OUTPUT_CHARS] + f"\n...обрезано ({len(out)} символов)"
        parts = [f"$ {command}", f"код: {code}, время: {elapsed:.1f}с"]
        if body:
            parts.append(body)
        if err.strip():
            parts.append("--- stderr ---\n" + err.strip()[:3000])
        if not body and not err.strip():
            parts.append("(пустой вывод)")
        return SkillResult(ok=(code == 0), output="\n".join(parts))

    async def _kill(self, proc: asyncio.subprocess.Process) -> None:
        try:
            if os.name == "nt":
                killer = await asyncio.create_subprocess_exec(
                    "taskkill", "/F", "/T", "/PID", str(proc.pid),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL)
                await killer.wait()
            else:
                proc.kill()
        except Exception as e:
            log.warning("не смогла убить процесс %s: %s", proc.pid, e)
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            pass

    # -------------------------------------------------------------- аудит

    def _audit(self, record: dict[str, Any]) -> None:
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            path = self.data_dir / self.cfg.audit_log
            with path.open("a", encoding="utf-8") as fp:
                fp.write(json.dumps({"ts": time.time(), "tool": "shell", **record},
                                    ensure_ascii=False) + "\n")
        except OSError:
            pass

    # --------------------------------------------------------------- схема

    def allowed_list(self) -> str:
        return ", ".join(sorted(ALLOWED_COMMANDS))

    def skill(self) -> Skill:
        return Skill(
            name="shell",
            description=(
                "Выполнить разрешённую команду на компьютере хозяина: git status/diff/log, "
                "pytest, python, просмотр каталогов и файлов, поиск. "
                "Только из allowlist, каждая команда требует подтверждения хозяина кнопкой. "
                "Не используй для правки файлов — для этого fs; "
                "не используй для кодинг-задач — для этого code_task."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": (f"команда одной строкой; разрешено: "
                                        f"{self.allowed_list()}"),
                    },
                    "workdir": {
                        "type": "string",
                        "description": "рабочий каталог; по умолчанию корень persona-runtime",
                    },
                },
                "required": ["command"],
            },
            execute=self,
        )
