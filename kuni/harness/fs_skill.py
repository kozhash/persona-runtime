"""Скилл `fs`: читать, писать, править и искать в разрешённых корнях.

Почему отдельный скилл, а не расширение `self_edit`: `self_edit` намеренно
умеет править только репозиторий kuni и гоняет pytest после каждой записи.
Для работы с чужими проектами это и медленно, и неверно. Здесь — общий
инструмент с той же защитой путей, но без привязки к одному репозиторию.

Действия:
  read      — прочитать файл (с номерами строк)
  write     — перезаписать файл целиком
  edit      — заменить точный фрагмент (как exact-edit, безопаснее write)
  append    — дописать в конец
  ls        — список файлов в каталоге
  glob      — найти файлы по маске (**/*.py)
  grep      — найти строку по файлам (с номерами строк)
  mkdir     — создать каталог
  stat      — размер, mtime, тип
  delete    — удалить файл (только через одобрение)
"""
from __future__ import annotations

import fnmatch
import logging
import os
import time
from pathlib import Path
from typing import Any

from ..skills import Skill, SkillResult
from .paths import PathDenied, PathPolicy

log = logging.getLogger(__name__)

MAX_GREP_HITS = 80
MAX_LS_ENTRIES = 300


class FsSkill:
    def __init__(self, policy: PathPolicy, approvals: Any = None,
                 bus: Any = None) -> None:
        self.policy = policy
        self.approvals = approvals
        self.bus = bus
        self._backups: dict[str, str] = {}

    # ------------------------------------------------------------- действия

    async def __call__(self, args: dict[str, Any]) -> SkillResult:
        action = (args.get("action") or "").strip().lower()
        try:
            if action == "read":
                return self._read(args)
            if action == "write":
                return self._write(args)
            if action == "edit":
                return self._edit(args)
            if action == "append":
                return self._append(args)
            if action == "ls":
                return self._ls(args)
            if action == "glob":
                return self._glob(args)
            if action == "grep":
                return self._grep(args)
            if action == "mkdir":
                return self._mkdir(args)
            if action == "stat":
                return self._stat(args)
            if action == "delete":
                return await self._delete(args)
            return SkillResult(ok=False, output=f"неизвестное действие fs: {action}")
        except PathDenied as e:
            log.warning("fs отказал (%s): %s", action, e)
            return SkillResult(ok=False, output=f"отказ: {e}")
        except Exception as e:
            log.exception("fs упал на %s", action)
            return SkillResult(ok=False, output=f"ошибка fs.{action}: {e}")

    def _read(self, args: dict[str, Any]) -> SkillResult:
        raw = args.get("path") or ""
        path, text, truncated = self.policy.read_text(raw)
        lines = text.splitlines()
        start = max(1, int(args.get("start_line") or 1))
        end = int(args.get("end_line") or 0) or len(lines)
        end = min(end, len(lines))
        numbered = "\n".join(f"{i:5d}| {lines[i - 1]}" for i in range(start, end + 1))
        head = f"{path}  ({len(lines)} строк, {path.stat().st_size} байт)"
        tail = "\n...[файл обрезан по размеру]" if truncated else ""
        return SkillResult(ok=True, output=f"{head}\n{numbered}{tail}")

    def _write(self, args: dict[str, Any]) -> SkillResult:
        path = self.policy.resolve(args.get("path") or "")
        content = args.get("content")
        if content is None:
            return SkillResult(ok=False, output="не передан content")
        data = str(content).encode("utf-8")
        self.policy.check_write_size(data)
        existed = path.exists()
        if existed:
            self._backup(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        verb = "перезаписан" if existed else "создан"
        return SkillResult(ok=True,
                           output=f"{verb}: {path} ({len(data)} байт, {content.count(chr(10)) + 1} строк)")

    def _edit(self, args: dict[str, Any]) -> SkillResult:
        path = self.policy.resolve(args.get("path") or "", must_exist=True)
        old = args.get("old") or args.get("old_text") or ""
        new = args.get("new") or args.get("new_text") or ""
        if not old:
            return SkillResult(ok=False, output="не передан old (что искать)")
        text = path.read_text(encoding="utf-8", errors="replace")
        count = text.count(old)
        if count == 0:
            return SkillResult(ok=False, output=f"фрагмент не найден в {path.name}")
        if count > 1 and not args.get("all"):
            return SkillResult(
                ok=False,
                output=(f"фрагмент встречается {count} раз в {path.name} — "
                        f"уточни old или передай all=true"))
        self._backup(path)
        updated = text.replace(old, new) if args.get("all") else text.replace(old, new, 1)
        path.write_text(updated, encoding="utf-8")
        return SkillResult(ok=True,
                           output=f"заменено вхождений: {count if args.get('all') else 1} в {path}")

    def _append(self, args: dict[str, Any]) -> SkillResult:
        path = self.policy.resolve(args.get("path") or "")
        content = str(args.get("content") or "")
        if not content:
            return SkillResult(ok=False, output="не передан content")
        if path.exists():
            self._backup(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fp:
            fp.write(content)
        return SkillResult(ok=True, output=f"дописано {len(content)} символов в {path}")

    def _ls(self, args: dict[str, Any]) -> SkillResult:
        path = self.policy.resolve(args.get("path") or ".", must_exist=True)
        if not path.is_dir():
            return SkillResult(ok=False, output=f"не каталог: {path}")
        entries = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        out = []
        for entry in entries[:MAX_LS_ENTRIES]:
            try:
                size = entry.stat().st_size if entry.is_file() else 0
            except OSError:
                size = 0
            marker = "/" if entry.is_dir() else ""
            out.append(f"{'d' if entry.is_dir() else '-'} {size:>10}  {entry.name}{marker}")
        more = f"\n...ещё {len(entries) - MAX_LS_ENTRIES}" if len(entries) > MAX_LS_ENTRIES else ""
        return SkillResult(ok=True, output=f"{path}  ({len(entries)} записей)\n" + "\n".join(out) + more)

    def _glob(self, args: dict[str, Any]) -> SkillResult:
        pattern = str(args.get("pattern") or "*")
        base = self.policy.resolve(args.get("path") or ".")
        if not base.is_dir():
            return SkillResult(ok=False, output=f"не каталог: {base}")
        hits: list[Path] = []
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in (
                ".git", "__pycache__", "node_modules", ".venv", "venv", ".mypy_cache")]
            for name in files:
                candidate = Path(root) / name
                rel = candidate.relative_to(base).as_posix()
                if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(name, pattern):
                    hits.append(candidate)
            if len(hits) >= 500:
                break
        shown = hits[:200]
        body = "\n".join(str(p.relative_to(base).as_posix()) for p in shown)
        more = f"\n...ещё {len(hits) - len(shown)}" if len(hits) > len(shown) else ""
        return SkillResult(ok=True,
                           output=f"найдено {len(hits)} в {base}\n{body}{more}")

    def _grep(self, args: dict[str, Any]) -> SkillResult:
        needle = str(args.get("query") or args.get("pattern") or "")
        if not needle:
            return SkillResult(ok=False, output="не передан query")
        base = self.policy.resolve(args.get("path") or ".")
        glob_pat = str(args.get("glob") or "*.py")
        case = bool(args.get("case_sensitive"))
        needle_cmp = needle if case else needle.lower()
        hits: list[str] = []
        files_scanned = 0
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in (
                ".git", "__pycache__", "node_modules", ".venv", "venv", ".mypy_cache")]
            for name in files:
                if not fnmatch.fnmatch(name, glob_pat):
                    continue
                path = Path(root) / name
                try:
                    raw = path.read_bytes()
                except OSError:
                    continue
                if b"\x00" in raw[:2048]:
                    continue          # бинарник
                files_scanned += 1
                for lineno, line in enumerate(
                        raw.decode("utf-8", errors="replace").splitlines(), 1):
                    hay = line if case else line.lower()
                    if needle_cmp in hay:
                        rel = path.relative_to(base).as_posix()
                        hits.append(f"{rel}:{lineno}: {line.strip()[:180]}")
                        if len(hits) >= MAX_GREP_HITS:
                            break
                if len(hits) >= MAX_GREP_HITS:
                    break
            if len(hits) >= MAX_GREP_HITS:
                break
        if not hits:
            return SkillResult(ok=True,
                               output=f"не найдено «{needle}» ({files_scanned} файлов проверено)")
        return SkillResult(ok=True,
                           output=(f"найдено {len(hits)} совпадений "
                                   f"({files_scanned} файлов):\n" + "\n".join(hits)))

    def _mkdir(self, args: dict[str, Any]) -> SkillResult:
        path = self.policy.resolve(args.get("path") or "")
        path.mkdir(parents=True, exist_ok=True)
        return SkillResult(ok=True, output=f"каталог готов: {path}")

    def _stat(self, args: dict[str, Any]) -> SkillResult:
        path = self.policy.resolve(args.get("path") or "", must_exist=True)
        st = path.stat()
        kind = "каталог" if path.is_dir() else "файл"
        return SkillResult(ok=True, output=(
            f"{path}\nтип: {kind}\nразмер: {st.st_size} байт\n"
            f"изменён: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(st.st_mtime))}"))

    async def _delete(self, args: dict[str, Any]) -> SkillResult:
        path = self.policy.resolve(args.get("path") or "", must_exist=True)
        if path.is_dir():
            return SkillResult(ok=False, output="удаление каталогов запрещено — только файлы")
        if self.approvals is not None:
            verdict = await self.approvals.request(
                "fs.delete", f"удалить файл {path.name}",
                f"удалить {path}\nразмер: {path.stat().st_size} байт")
            if not verdict["allowed"]:
                return SkillResult(ok=False, output=f"удаление не разрешено: {verdict['reason']}")
        self._backup(path)
        path.unlink()
        return SkillResult(ok=True, output=f"удалён: {path} (бэкап рядом)")

    # ------------------------------------------------------------- бэкапы

    def _backup(self, path: Path) -> None:
        """Копия перед изменением: дешевле, чем объяснять, куда делся код."""
        try:
            if not path.exists():
                return
            stamp = time.strftime("%Y%m%d-%H%M%S")
            backup = path.with_name(f"{path.name}.bak-{stamp}")
            backup.write_bytes(path.read_bytes())
            self._backups[str(path)] = str(backup)
        except OSError as e:
            log.warning("не смогла сделать бэкап %s: %s", path, e)

    # ------------------------------------------------------------- схема

    def skill(self) -> Skill:
        return Skill(
            name="fs",
            description=(
                "Файлы на компьютере хозяина: прочитать, записать, точно заменить "
                "фрагмент, дописать, посмотреть каталог, найти по маске, найти строку "
                "по коду, создать каталог, узнать размер, удалить файл. "
                "Работает только внутри разрешённых корней; секреты закрыты. "
                "Для правки существующего файла предпочитай edit, а не write — "
                "write перезаписывает целиком."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["read", "write", "edit", "append", "ls",
                                 "glob", "grep", "mkdir", "stat", "delete"],
                        "description": "что сделать",
                    },
                    "path": {"type": "string",
                             "description": "путь; для ls/glob/grep — каталог"},
                    "content": {"type": "string",
                                "description": "содержимое для write/append"},
                    "old": {"type": "string",
                            "description": "точный фрагмент для edit"},
                    "new": {"type": "string",
                            "description": "на что заменить в edit"},
                    "all": {"type": "boolean",
                            "description": "заменить все вхождения в edit"},
                    "pattern": {"type": "string",
                                "description": "маска для glob, например **/*.py"},
                    "query": {"type": "string",
                              "description": "что искать в grep"},
                    "glob": {"type": "string",
                             "description": "фильтр файлов для grep, по умолчанию *.py"},
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                    "case_sensitive": {"type": "boolean"},
                },
                "required": ["action"],
            },
            execute=self,
        )
