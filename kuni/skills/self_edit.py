"""Скилл самоправки: она переписывает свои файлы через терминал-процесс.

Правила выживания (неуязвимость важнее свободы):
1. Каждая правка = бэкап старого файла в data/self_edits/<время>__<имя>.bak
2. После записи гоняются тесты (pytest). Упали — откат автоматически.
3. Защищённые пути править нельзя: сам исполнитель, шина, данные, venv.
   Её доступ к собственному терминалу и откату неуничтожим.
4. Вне проекта не пишем. Размер и типы файлов ограничены.
5. История правок — data/self_edits/edits.jsonl (кто, что, когда, тесты).
Откат руками: `kuni rollback --last N`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from . import SkillResult

log = logging.getLogger(__name__)

# что она НЕ имеет права править никогда (доступ к терминалу и откат святы)
PROTECTED_PREFIXES = (
    "kuni/skills/self_edit.py",
    "kuni/core/bus.py",
    "data/",
    ".venv/",
    ".git/",
    "node_modules/",
)
ALLOWED_SUFFIXES = {".py", ".md", ".toml", ".html", ".js", ".json", ".yml", ".yaml", ".sh", ".txt"}
MAX_FILE_SIZE = 400_000


class SelfEditSkill:
    def __init__(self, root: Path, python_exe: str | None = None,
                 test_cmd: Callable[[], list[str]] | None = None,
                 bus: Any = None) -> None:
        self.root = Path(root).resolve()
        self.python = python_exe or sys.executable
        self._test_cmd = test_cmd or (lambda: [self.python, "-m", "pytest", "-q", "--timeout=120"])
        self.edits_dir = self.root / "data" / "self_edits"
        self.edits_dir.mkdir(parents=True, exist_ok=True)
        self.bus = bus

    # ------------------------------------------------------------- проверки

    def _validate(self, raw_path: str) -> Path:
        path = Path(raw_path.replace("\\", "/")).as_posix().lstrip("/")
        if ".." in path or Path(path).is_absolute():
            raise ValueError("путь вне проекта запрещён")
        lowered = path.lower()
        if any(lowered.startswith(p) for p in PROTECTED_PREFIXES):
            raise ValueError(f"«{path}» защищён: исполнитель, шина и данные неприкосновенны")
        if Path(path).suffix.lower() not in ALLOWED_SUFFIXES:
            raise ValueError(f"тип файла не разрешён ({Path(path).suffix or 'без типа'})")
        full = self.root / path
        if not full.parent.exists():
            full.parent.mkdir(parents=True, exist_ok=True)
        return full

    async def _run_tests(self) -> tuple[bool, str]:
        cmd = self._test_cmd()
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=str(self.root),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=300)
        except (OSError, asyncio.TimeoutError) as e:
            return False, f"тесты не запустились: {e}"
        text = out.decode("utf-8", errors="replace")[-1500:] if out else ""
        return proc.returncode == 0, text

    # --------------------------------------------------------------- действия

    async def __call__(self, args: dict[str, Any]) -> SkillResult:
        action = str(args.get("action") or "write").lower()
        try:
            if action == "read":
                return self._read(str(args.get("path") or ""))
            if action == "history":
                return self._history()
            if action == "write":
                return await self._write(str(args.get("path") or ""),
                                         str(args.get("content") or ""))
            return SkillResult(ok=False, output=f"неизвестное действие: {action}")
        except ValueError as e:
            return SkillResult(ok=False, output=f"отказано: {e}")

    def _read(self, raw_path: str) -> SkillResult:
        path = self._validate(raw_path)
        if not path.exists():
            return SkillResult(ok=False, output=f"{raw_path} не существует")
        text = path.read_text(encoding="utf-8", errors="replace")
        return SkillResult(ok=True, output=text[:6000] + ("\n…[обрезано]" if len(text) > 6000 else ""))

    def _history(self) -> SkillResult:
        log_file = self.edits_dir / "edits.jsonl"
        if not log_file.exists():
            return SkillResult(ok=True, output="правок ещё не было")
        rows = [json.loads(line) for line in
                log_file.read_text(encoding="utf-8").splitlines()[-10:]][::-1]
        lines = [f"- {r['when']} {r['path']} ({'тесты ок' if r['ok'] else 'откат'})" for r in rows]
        return SkillResult(ok=True, output="\n".join(lines) or "пусто")

    def _term(self, text: str, kind: str = "cmd") -> None:
        """Строка в веб-терминал дашборда (что она делает в консоли)."""
        if self.bus:
            self.bus.emit("terminal.line", text=text[:500], kind=kind)

    async def _write(self, raw_path: str, content: str) -> SkillResult:
        if not raw_path or not content.strip():
            return SkillResult(ok=False, output="пустой путь или содержимое")
        if len(content) > MAX_FILE_SIZE:
            return SkillResult(ok=False, output="слишком большой файл")
        path = self._validate(raw_path)
        rel = path.relative_to(self.root).as_posix()

        original = path.read_text(encoding="utf-8", errors="replace") if path.exists() else None
        stamp = time.strftime("%Y%m%d_%H%M%S")
        backup = self.edits_dir / f"{stamp}__{rel.replace('/', '__')}.bak"
        backup.write_text(original if original is not None else "", encoding="utf-8")

        self._term(f"$ self_edit write {rel}", "cmd")
        self._term(f"backup -> data/self_edits/{backup.name}", "dim")
        path.write_text(content, encoding="utf-8")
        self._term("$ pytest -q  (гоняю тесты…)", "cmd")
        tests_ok, tests_out = await self._run_tests()
        for line in tests_out.strip().splitlines()[-6:]:
            self._term(line, "out")

        record = {"ts": time.time(), "when": time.strftime("%Y-%m-%d %H:%M:%S"),
                  "path": rel, "ok": tests_ok, "backup": backup.name}
        with open(self.edits_dir / "edits.jsonl", "a", encoding="utf-8") as fp:
            fp.write(json.dumps(record, ensure_ascii=False) + "\n")
        if self.bus:
            self.bus.emit("self_edit.applied", **record)

        if tests_ok:
            self._term(f"OK: {rel} принята, тесты зелёные", "ok")
            return SkillResult(ok=True,
                               output=f"правка {rel} принята, тесты зелёные. бэкап: {backup.name}")
        # откат
        if original is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(original, encoding="utf-8")
        self._term(f"FAIL: тесты упали — {rel} ОТКАЧЕНА автоматически", "err")
        log.warning("self_edit: тесты упали, %s откачен", rel)
        return SkillResult(ok=False,
                           output=f"тесты упали — правка {rel} ОТКАЧЕНА автоматически. "
                                  f"Последние строки pytest:\n{tests_out[-600:]}")

    def skill(self) -> Any:
        from . import Skill
        return Skill(
            name="self_edit",
            description="Правишь собственные файлы (промпты, код, конфиг): read — прочитать, "
                        "write — переписать (автотесты + автооткат при падении), history — журнал. "
                        "Защищённое (исполнитель, шина, data/) править нельзя.",
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["read", "write", "history"]},
                    "path": {"type": "string", "description": "путь от корня проекта, напр. prompts/character_base.md"},
                    "content": {"type": "string", "description": "новое содержимое целиком (для write)"},
                },
                "required": ["action"],
            },
            execute=self.__call__,
        )
