"""Скилл `files`: её собственный архив — сохранить, прочитать, отправить.

Отличие от `fs`: `fs` — руки в чужих проектах, а здесь — личная папка
`kuni-vault`, которая принадлежит только ей. Здесь можно держать заметки,
скачанные документы, черновики, что угодно. И отсюда же можно отправить
файл человеку в Telegram.

Почему отдельный репозиторий: её файлы не должны смешиваться с кодом
проекта. В vault свой git, свои коммиты — если она что-то испортит, откат
не заденет рабочий репозиторий.

Действия:
  save     — сохранить текст как файл (md, txt, json, любой)
  read     — прочитать файл из архива
  list     — что лежит в архиве
  delete   — удалить (через одобрение)
  send     — отправить файл в телеграм-чат
  fetch    — скачать вложение из сообщения Telegram в архив
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from ..skills import Skill, SkillResult
from ..harness.paths import PathDenied, PathPolicy

log = logging.getLogger(__name__)

#: Расширения, которые считаем документами. Остальное — тоже можно,
#: но эти чаще всего нужны.
DOC_SUFFIXES = {".md", ".txt", ".json", ".csv", ".yaml", ".yml", ".toml",
                ".py", ".html", ".xml", ".log", ".pdf", ".docx", ".xlsx",
                ".png", ".jpg", ".jpeg", ".webp", ".zip", ".gif"}

MAX_SAVE_BYTES = 2_000_000


class FilesSkill:
    def __init__(self, vault: Path, telegram: Any = None,
                 approvals: Any = None, policy: PathPolicy | None = None,
                 bus: Any = None) -> None:
        self.vault = Path(vault)
        self.telegram = telegram
        self.approvals = approvals
        self.bus = bus
        # внутри архива разрешено всё, кроме секретов; политика та же
        self.policy = policy

    # ------------------------------------------------------------- вызов

    async def __call__(self, args: dict[str, Any]) -> SkillResult:
        action = str(args.get("action") or "list").strip().lower()
        try:
            if action == "save":
                return self._save(args)
            if action == "read":
                return self._read(args)
            if action == "list":
                return self._list(args)
            if action == "delete":
                return await self._delete(args)
            if action == "send":
                return await self._send(args)
            if action == "fetch":
                return await self._fetch(args)
            return SkillResult(ok=False, output=f"неизвестное действие files: {action}")
        except PathDenied as e:
            return SkillResult(ok=False, output=f"отказ: {e}")
        except Exception as e:
            log.exception("files упал на %s", action)
            return SkillResult(ok=False, output=f"ошибка files.{action}: {e}")

    # ------------------------------------------------------------- пути

    def _resolve(self, raw: str, must_exist: bool = False) -> Path:
        """Путь внутри архива: `..` наружу не выпускаем."""
        name = str(raw or "").strip().lstrip("/\\")
        if not name:
            raise PathDenied("пустой путь")
        path = (self.vault / name).resolve()
        vault = self.vault.resolve()
        if path != vault and not path.is_relative_to(vault):
            raise PathDenied(f"путь вне архива: {name}")
        if must_exist and not path.exists():
            raise PathDenied(f"в архиве нет файла: {name}")
        return path

    # ------------------------------------------------------------- действия

    def _save(self, args: dict[str, Any]) -> SkillResult:
        name = str(args.get("name") or args.get("path") or "").strip()
        if not name:
            return SkillResult(ok=False, output="нужно имя файла")
        content = args.get("content")
        if content is None:
            return SkillResult(ok=False, output="не передан content")
        text = str(content)
        data = text.encode("utf-8")
        if len(data) > MAX_SAVE_BYTES:
            return SkillResult(ok=False,
                               output=f"слишком большой файл: {len(data)} байт")
        # без расширения — считаем заметкой
        if not Path(name).suffix:
            name += ".md"
        path = self._resolve(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        existed = path.exists()
        path.write_bytes(data)
        self._git_commit(f"{'обновила' if existed else 'сохранила'} {name}")
        self._emit("files.saved", name=name, bytes=len(data))
        return SkillResult(ok=True,
                           output=(f"{'обновила' if existed else 'сохранила'} "
                                   f"{path.relative_to(self.vault).as_posix()} "
                                   f"({len(data)} байт)"))

    def _read(self, args: dict[str, Any]) -> SkillResult:
        path = self._resolve(str(args.get("name") or args.get("path") or ""),
                             must_exist=True)
        if path.is_dir():
            return self._list({"path": str(path.relative_to(self.vault))})
        size = path.stat().st_size
        if size > MAX_SAVE_BYTES:
            return SkillResult(ok=False, output=f"файл слишком большой: {size} байт")
        raw = path.read_bytes()
        if b"\x00" in raw[:2048]:
            return SkillResult(ok=True,
                               output=f"{path.name}: бинарный файл, {size} байт "
                                      f"(не текст)")
        text = raw.decode("utf-8", errors="replace")
        return SkillResult(ok=True,
                           output=f"{path.relative_to(self.vault).as_posix()} "
                                  f"({size} байт)\n{text[:60000]}")

    def _list(self, args: dict[str, Any]) -> SkillResult:
        sub = str(args.get("path") or "").strip()
        base = self._resolve(sub) if sub else self.vault.resolve()
        if not base.is_dir():
            return SkillResult(ok=False, output=f"не каталог: {sub}")
        rows: list[str] = []
        for entry in sorted(base.rglob("*")):
            if ".git" in entry.parts:
                continue
            if entry.is_dir():
                continue
            rel = entry.relative_to(self.vault).as_posix()
            stamp = time.strftime("%Y-%m-%d %H:%M",
                                  time.localtime(entry.stat().st_mtime))
            rows.append(f"{entry.stat().st_size:>9}  {stamp}  {rel}")
        if not rows:
            return SkillResult(ok=True, output="архив пуст")
        head = f"архив: {len(rows)} файлов\n"
        return SkillResult(ok=True, output=head + "\n".join(rows[:200]))

    async def _delete(self, args: dict[str, Any]) -> SkillResult:
        name = str(args.get("name") or args.get("path") or "")
        path = self._resolve(name, must_exist=True)
        if path.is_dir():
            return SkillResult(ok=False, output="каталоги не удаляю — только файлы")
        if self.approvals is not None:
            verdict = await self.approvals.request(
                "files.delete", f"удалить из архива {path.name}",
                f"{path}\nразмер: {path.stat().st_size} байт")
            if not verdict["allowed"]:
                return SkillResult(ok=False, output=f"не разрешено: {verdict['reason']}")
        path.unlink()
        self._git_commit(f"удалила {name}")
        return SkillResult(ok=True, output=f"удалила {name}")

    async def _send(self, args: dict[str, Any]) -> SkillResult:
        if self.telegram is None:
            return SkillResult(ok=False, output="телеграм недоступен")
        path = self._resolve(str(args.get("name") or args.get("path") or ""),
                             must_exist=True)
        chat_id = int(args.get("chat_id") or 0)
        if not chat_id:
            return SkillResult(ok=False, output="нужен chat_id")
        caption = str(args.get("caption") or "")[:1000]
        message_id = await self.telegram.send_document(chat_id, path, caption)
        return SkillResult(ok=True,
                           output=f"отправила {path.name} в {chat_id} "
                                  f"(msg {message_id})")

    async def _fetch(self, args: dict[str, Any]) -> SkillResult:
        """Скачать вложение из Telegram в архив."""
        if self.telegram is None:
            return SkillResult(ok=False, output="телеграм недоступен")
        chat_id = int(args.get("chat_id") or 0)
        message_id = int(args.get("message_id") or 0)
        if not chat_id or not message_id:
            return SkillResult(ok=False, output="нужны chat_id и message_id")
        message = await self.telegram.get_message(chat_id, message_id)
        content = (message or {}).get("content") or {}
        file_id, ext = self._attachment_of(content)
        if not file_id:
            return SkillResult(ok=False, output="в сообщении нет вложения")
        local = await self.telegram.download_file(file_id)
        if not local or not local.exists():
            return SkillResult(ok=False, output="не скачалось")
        name = str(args.get("name") or f"вложение-{message_id}{ext}")
        if not Path(name).suffix:
            name += ext
        target = self._resolve(name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(local.read_bytes())
        self._git_commit(f"скачала {name}")
        return SkillResult(ok=True,
                           output=f"сохранила в архив: {name} "
                                  f"({target.stat().st_size} байт)")

    @staticmethod
    def _attachment_of(content: dict[str, Any]) -> tuple[int, str]:
        ctype = str(content.get("@type") or "")
        mapping = {
            "messageDocument": ("document", ".bin"),
            "messagePhoto": ("photo", ".jpg"),
            "messageVideo": ("video", ".mp4"),
            "messageAudio": ("audio", ".mp3"),
            "messageVoiceNote": ("voice", ".ogg"),
            "messageAnimation": ("animation", ".gif"),
        }
        if ctype not in mapping:
            return 0, ""
        key, ext = mapping[ctype]
        node = content.get(key) or {}
        if ctype == "messagePhoto":
            sizes = node.get("sizes") or []
            if sizes:
                node = sizes[-1].get("photo") or {}
        elif ctype in ("messageVideo", "messageAudio", "messageAnimation"):
            node = node.get(key) or {}
        file_id = int(node.get("id") or 0)
        name = str(node.get("file_name") or "")
        if name and "." in name:
            ext = "." + name.rsplit(".", 1)[-1][:8]
        return file_id, ext

    # ------------------------------------------------------------- git

    def _git_commit(self, message: str) -> None:
        """Коммит в архив: история правок бесплатно."""
        import subprocess
        try:
            subprocess.run(["git", "add", "-A"], cwd=str(self.vault),
                           capture_output=True, timeout=20)
            subprocess.run(["git", "-c", "user.name=kuni",
                            "-c", "user.email=kuni@local",
                            "commit", "-q", "-m", message[:200]],
                           cwd=str(self.vault), capture_output=True, timeout=20)
        except Exception as e:
            log.debug("git в архиве не сработал: %s", e)

    def _emit(self, topic: str, **payload: Any) -> None:
        if self.bus is not None:
            self.bus.emit(topic, **payload)

    # ------------------------------------------------------------- схема

    def skill(self) -> Skill:
        return Skill(
            name="files",
            description=(
                "Свой личный архив (kuni-vault): сохранить текст как файл, прочитать, "
                "посмотреть список, удалить, отправить файл в телеграм, скачать "
                "вложение из сообщения в архив. Для своего: заметки, черновики, "
                "документы. Для правки чужого кода — fs."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["save", "read", "list", "delete", "send", "fetch"],
                    },
                    "name": {"type": "string",
                             "description": "имя файла внутри архива, напр. заметки/идея.md"},
                    "content": {"type": "string",
                                "description": "содержимое для save"},
                    "path": {"type": "string", "description": "подкаталог для list"},
                    "chat_id": {"type": "integer",
                                "description": "куда отправить (для send/fetch)"},
                    "message_id": {"type": "integer",
                                   "description": "какое сообщение забрать (для fetch)"},
                    "caption": {"type": "string"},
                },
                "required": ["action"],
            },
            execute=self,
        )
