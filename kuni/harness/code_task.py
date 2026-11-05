"""Скилл `code_task`: отдать кодинг внешнему агенту в отдельном процессе.

Почему не свой цикл: свой requires tool-calling, ретраи, обрезку контекста,
применение диффов, откат, разбор ошибок компилятора — полторы тысячи строк,
которые уже написаны и отлажены в Codex CLI. Своя работа должна быть в
политике доступа и мосте в Telegram, а не в движке.

Что здесь есть:
- запуск в отдельном процессе (падение харнесса не роняет персонажа);
- стрим прогресса в чат, иначе она молчит минутами и выглядит мёртвой;
- жёсткий таймаут с убийством дерева процессов;
- аудит-лог каждого запуска;
- рабочая директория только внутри разрешённых корней.

Codex запускается с `-s workspace-write`: писать можно только в рабочей
директории, читать — где угодно, сеть закрыта. На этой машине песочница
`elevated` не работает (нет helper'а), `unelevated` проверен: read-only
реально блокирует запись.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from ..skills import Skill, SkillResult
from .paths import PathDenied, PathPolicy

log = logging.getLogger(__name__)

#: Сколько последних строк прогресса держим для отчёта.
MAX_PROGRESS_LINES = 40
#: Потолок вывода, который уходит обратно в модель.
MAX_RESULT_CHARS = 12_000

ProgressCallback = Callable[[str], Awaitable[None]]


class CodeTaskSkill:
    def __init__(self, policy: PathPolicy, cfg: Any, approvals: Any = None,
                 data_dir: Path | None = None, bus: Any = None,
                 on_progress: ProgressCallback | None = None) -> None:
        self.policy = policy
        self.cfg = cfg
        self.approvals = approvals
        self.data_dir = data_dir or Path("data")
        self.bus = bus
        self.on_progress = on_progress
        self._running: dict[str, asyncio.subprocess.Process] = {}

    # --------------------------------------------------------------- запуск

    async def __call__(self, args: dict[str, Any]) -> SkillResult:
        task = (args.get("task") or "").strip()
        if not task:
            return SkillResult(ok=False, output="не передана задача")
        if not self.cfg.enabled:
            return SkillResult(ok=False, output="руки выключены в конфиге")

        try:
            workdir = self.policy.resolve(args.get("workdir") or ".")
        except PathDenied as e:
            return SkillResult(ok=False, output=f"отказ: {e}")
        if not workdir.is_dir():
            return SkillResult(ok=False, output=f"не каталог: {workdir}")

        engine = self._find_engine()
        if engine is None:
            return SkillResult(
                ok=False,
                output=("не найден движок: нужен codex в PATH. "
                        "Поставь Codex CLI или укажи harness.codex_command"))

        # задачи, которые могут лезть наружу, спрашиваем заранее:
        # лучше отказать до запуска, чем убить наполовину сделанное
        if self.approvals is not None and self.cfg.require_approval:
            verdict = await self.approvals.request(
                "code_task",
                f"кодинг-задача в {workdir.name}",
                f"движок: {Path(engine).name}\n"
                f"песочница: {self.cfg.codex_sandbox}\n"
                f"таймаут: {int(self.cfg.codex_timeout_minutes)} мин\n"
                f"каталог: {workdir}\n\n{task[:900]}")
            if not verdict["allowed"]:
                return SkillResult(ok=False,
                                   output=f"задача не разрешена: {verdict['reason']}")

        return await self._run(engine, task, workdir)

    def _find_engine(self) -> str | None:
        cmd = (self.cfg.codex_command or "codex").strip()
        found = shutil.which(cmd)
        if found:
            return found
        candidate = Path(cmd)
        if candidate.is_file():
            return str(candidate)
        return None

    def _build_argv(self, engine: str, task: str, workdir: Path) -> list[str]:
        argv = [
            engine, "exec",
            "--json",
            "--skip-git-repo-check",
            "-s", self.cfg.codex_sandbox,
            "-C", str(workdir),
        ]
        # elevated-песочница на этой машине не стартует: helper не найден.
        # unelevated проверен и работает, read-only честно блокирует запись.
        argv += ["-c", "windows.sandbox=unelevated"]
        argv.append(task)
        return argv

    @staticmethod
    def _engine_env() -> dict[str, str]:
        """Окружение для codex: подставить ключ из auth.json, если его нет в env.

        Codex хранит креды в ~/.codex/auth.json, но при старте из чужого
        процесса (наш subprocess) читает провайдерский `env_key` — и падает
        с «Missing environment variable». Ключ передаём в памяти, в лог
        и в аудит он не попадает.
        """
        env = dict(os.environ)
        env.setdefault("PYTHONIOENCODING", "utf-8")
        if env.get("OPENAI_API_KEY"):
            return env
        auth = Path.home() / ".codex" / "auth.json"
        if not auth.is_file():
            return env
        try:
            data = json.loads(auth.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return env
        for field in ("OPENAI_API_KEY", "openai_api_key", "api_key"):
            value = data.get(field) if isinstance(data, dict) else None
            if isinstance(value, str) and value.strip():
                env["OPENAI_API_KEY"] = value.strip()
                break
        return env

    async def _run(self, engine: str, task: str, workdir: Path) -> SkillResult:
        key = f"{int(time.time())}-{os.getpid()}"
        argv = self._build_argv(engine, task, workdir)
        started = time.time()
        progress: list[str] = []
        last_message = ""
        errors: list[str] = []
        audit = self._audit_path()
        self._audit(audit, {"event": "start", "task": task[:500],
                            "workdir": str(workdir), "argv": argv[:-1]})
        self.bus and self.bus.emit("harness.code_started", workdir=str(workdir))

        env = self._engine_env()
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(workdir),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            self._running[key] = proc
            timeout = max(60.0, self.cfg.codex_timeout_minutes * 60.0)
            try:
                stdout, stderr = await asyncio.wait_for(
                    self._pump(proc, progress), timeout=timeout)
            except asyncio.TimeoutError:
                await self._kill(proc)
                self._audit(audit, {"event": "timeout", "seconds": int(timeout)})
                return SkillResult(
                    ok=False,
                    output=(f"задача не успела за {int(self.cfg.codex_timeout_minutes)} мин "
                            f"и была остановлена.\n" + self._tail(progress)))
        except FileNotFoundError:
            return SkillResult(ok=False, output=f"движок не запустился: {engine}")
        except Exception as e:
            log.exception("code_task упал")
            return SkillResult(ok=False, output=f"ошибка запуска: {e}")
        finally:
            self._running.pop(key, None)

        elapsed = int(time.time() - started)
        for line in (stdout or "").splitlines():
            parsed = self._parse_event(line)
            if parsed:
                kind, text = parsed
                if kind == "message":
                    last_message = text
                    progress.append(text[:300])
                elif kind in ("command", "file", "search"):
                    progress.append(f"{kind}: {text[:200]}")
                elif kind == "error":
                    errors.append(text[:300])
        if stderr:
            err_text = stderr.strip()
            # codex пишет это, когда stdin не терминал — это не ошибка
            err_text = "\n".join(
                line for line in err_text.splitlines()
                if "Reading additional input from stdin" not in line)
            if err_text.strip():
                errors.append(err_text[-1500:])
            if any("workspace-write" in e or "sandbox-setup" in e for e in errors):
                errors.append("подсказка: на этой машине elevated-песочница не работает, "
                              "нужен harness.codex_sandbox = workspace-write")

        self._audit(audit, {"event": "finish", "seconds": elapsed,
                            "errors": errors[:5], "files": len(progress)})
        self.bus and self.bus.emit("harness.code_finished",
                                   workdir=str(workdir), seconds=elapsed)

        ok = not errors or bool(last_message)
        parts = [f"задача: {task[:200]}",
                 f"каталог: {workdir}",
                 f"время: {elapsed}с"]
        if last_message:
            parts.append("\n--- итог ---\n" + last_message[:6000])
        if progress:
            parts.append("\n--- шаги ---\n" + self._tail(progress))
        if errors:
            parts.append("\n--- ошибки ---\n" + "\n".join(errors)[-3000:])
        return SkillResult(ok=ok, output="\n".join(parts)[:MAX_RESULT_CHARS])

    async def _pump(self, proc: asyncio.subprocess.Process,
                    progress: list[str]) -> tuple[str, str]:
        """Читать stdout построчно, чтобы видеть прогресс, а не ждать конца."""
        chunks: list[str] = []

        async def read_stream(stream, sink: list[str]) -> None:
            while True:
                line = await stream.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace")
                sink.append(text)
                parsed = self._parse_event(text)
                if parsed and self.on_progress:
                    kind, body = parsed
                    if kind in ("message", "command", "file"):
                        progress.append(f"{kind}: {body[:200]}")
                        try:
                            await self.on_progress(self._humanize(kind, body))
                        except Exception:
                            pass

        out: list[str] = []
        await asyncio.gather(
            read_stream(proc.stdout, out),
            read_stream(proc.stderr, chunks),
        )
        await proc.wait()
        return "".join(out), "".join(chunks)

    @staticmethod
    def _humanize(kind: str, body: str) -> str:
        if kind == "command":
            return f"⚙️ {body[:160]}"
        if kind == "file":
            return f"📄 {body[:160]}"
        return body[:160]

    # ------------------------------------------------------------- события

    def _parse_event(self, line: str) -> tuple[str, str] | None:
        """Разобрать JSONL-событие codex в (тип, текст).

        Формат меняется между версиями, поэтому разбираем терпимо: ищем
        известные поля, а на незнакомом возвращаем None и не падаем.
        """
        line = line.strip()
        if not line or not line.startswith("{"):
            return None
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(event, dict):
            return None
        etype = str(event.get("type") or "")

        # новый формат: item.completed + item{type,text,command}
        item = event.get("item")
        if isinstance(item, dict):
            itype = str(item.get("type") or "")
            if itype in ("agent_message", "assistant_message"):
                return "message", str(item.get("text") or "")[:6000]
            if itype == "command_execution":
                return "command", str(item.get("command") or "")[:300]
            if itype in ("file_change", "patch_apply"):
                changes = item.get("changes")
                if isinstance(changes, list):
                    names = [str(c.get("path") or "") for c in changes[:6]
                             if isinstance(c, dict)]
                    return "file", ", ".join(n for n in names if n)[:300]
                return "file", str(item.get("path") or "")[:300]
            if itype == "reasoning":
                return None
            if itype == "error":
                return "error", str(item.get("message") or "")[:800]

        # старый формат: msg.type
        inner = event.get("msg")
        if isinstance(inner, dict):
            mtype = str(inner.get("type") or "")
            if mtype in ("agent_message", "agent_message_delta"):
                return "message", str(inner.get("message") or inner.get("text") or "")[:6000]
            if mtype in ("exec_command_begin", "exec_command"):
                return "command", str(inner.get("command") or "")[:300]
            if mtype == "patch_apply_begin":
                return "file", "patch applied"
            if mtype in ("error", "stream_error"):
                return "error", str(inner.get("message") or "")[:800]

        if etype in ("error", "stream_error"):
            return "error", str(event.get("message") or event.get("error") or "")[:800]
        return None

    # ------------------------------------------------------------- служебное

    @staticmethod
    def _tail(lines: list[str], limit: int = MAX_PROGRESS_LINES) -> str:
        shown = lines[-limit:]
        prefix = "" if len(lines) <= limit else f"...(ещё {len(lines) - limit})\n"
        return prefix + "\n".join(shown)

    async def _kill(self, proc: asyncio.subprocess.Process) -> None:
        """Убить всё дерево: codex запускает свои подпроцессы."""
        try:
            if os.name == "nt":
                subprocess_kill = await asyncio.create_subprocess_exec(
                    "taskkill", "/F", "/T", "/PID", str(proc.pid),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL)
                await subprocess_kill.wait()
            else:
                proc.kill()
        except Exception as e:
            log.warning("не смогла убить codex %s: %s", proc.pid, e)
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            pass

    def _audit_path(self) -> Path:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return self.data_dir / self.cfg.audit_log

    @staticmethod
    def _audit(path: Path, record: dict[str, Any]) -> None:
        try:
            record = {"ts": time.time(), **record}
            with path.open("a", encoding="utf-8") as fp:
                fp.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def stop_all(self) -> None:
        for proc in list(self._running.values()):
            try:
                proc.kill()
            except Exception:
                pass
        self._running.clear()

    # ---------------------------------------------------------------- схема

    def skill(self) -> Skill:
        return Skill(
            name="code_task",
            description=(
                "Отдать кодинг-задачу внешнему агенту (Codex CLI) в отдельном процессе: "
                "он прочитает репозиторий, внесёт правки, запустит тесты и вернёт отчёт. "
                "Используй для всего, что требует нескольких шагов по коду: «добавь фичу», "
                "«починить баг», «написать тесты», «отрефакторить модуль». "
                "Для простого чтения или одной правки лучше fs — быстрее и точнее. "
                "Работает только внутри разрешённых корней и требует разрешения хозяина."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": ("что нужно сделать, конкретно и проверяемо; "
                                        "укажи ожидаемый результат и чем проверить"),
                    },
                    "workdir": {
                        "type": "string",
                        "description": "каталог проекта; по умолчанию корень persona-runtime",
                    },
                },
                "required": ["task"],
            },
            execute=self,
        )
