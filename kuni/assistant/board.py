"""Ассистент-режим: приватный канал = доска заданий.

Цикл: каждые check_interval минут читает новые сообщения канала -> LLM
раскладывает их в задачи (на сегодня/завтра/дату) -> в run_hour (и сразу,
если задача «сейчас») исполняет: технические СТРОГО делегирует Antigravity CLI
(agy), нетехнические делает сама своими скиллами. Отчёты — в личку {papik}.

Режимы (mode): off | companion (обычная персона) | assistant (поручения
выполняются). Переключается с дашборда/ТГ.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from typing import Any

log = logging.getLogger(__name__)

TECH_MARKERS = ("код", "скрипт", "bug", "баг", "refactor", "рефактор", "файл",
                "напиши программу", "деплой", "deploy", "test", "тест",
                "парсер", "api", "бот", "репозитор", "git", "python", "js",
                "html", "css", "sql", "сервер")


class AssistantModule:
    def __init__(self, app: Any) -> None:
        self.app = app
        self.bus = app.bus
        self.db: sqlite3.Connection = app.db
        self._ensure_table()
        self._seen_msg_ids: set[int] = set()
        for row in self.db.execute("SELECT msg_id FROM tasks").fetchall():
            self._seen_msg_ids.add(row[0])

    def _ensure_table(self) -> None:
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS tasks (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   msg_id INTEGER,
                   text TEXT NOT NULL,
                   assignee TEXT NOT NULL DEFAULT 'agy',
                   due_ts REAL NOT NULL,
                   status TEXT NOT NULL DEFAULT 'pending',
                   result TEXT DEFAULT '',
                   created_ts REAL NOT NULL
               )"""
        )
        self.db.commit()

    # ------------------------------------------------------------- приём задач

    MODE_ALIASES = {"on": "assistant", "вкл": "assistant", "true": "assistant",
                    "1": "assistant", "выкл": "off", "false": "off", "0": "off"}

    def mode(self) -> str:
        raw = str(self.app.config.current.assistant.mode or "off").strip().lower()
        return self.MODE_ALIASES.get(raw, raw if raw in ("off", "companion", "assistant") else "off")

    def set_mode(self, mode: str) -> None:
        mode = self.MODE_ALIASES.get(str(mode).strip().lower(), str(mode).strip().lower())
        if mode not in ("off", "companion", "assistant"):
            raise ValueError("режимы: off | companion | assistant")
        import re
        text = self.app.config.path.read_text(encoding="utf-8")
        if re.search(r'^mode\s*=', text, re.M):
            text = re.sub(r'^(mode\s*=\s*)"[^"]*"', rf'\1"{mode}"', text, count=1, flags=re.M)
        else:
            text = text.rstrip() + f'\n[assistant]\nmode = "{mode}"\n'
        self.app.config.path.write_text(text, encoding="utf-8")
        self.app.config.maybe_reload(force=True)
        self.bus.emit("assistant.mode", mode=mode)
        log.info("режим ассистента: %s", mode)

    async def poll_channel(self) -> None:
        """Читает новые сообщения канала-доски и раскладывает их в задачи."""
        cfg = self.app.config.current.assistant
        if self.mode() != "assistant" or not cfg.channel_id:
            return
        try:
            messages = await self.app.telegram.get_chat_history(cfg.channel_id, limit=15)
        except Exception as e:
            log.warning("канал задач %s не читается: %s", cfg.channel_id, str(e)[:80])
            return
        fresh = [m for m in messages
                 if m.get("id") and m["id"] not in self._seen_msg_ids
                 and not m.get("is_outgoing")]
        for message in fresh:
            content = message.get("content", {})
            if content.get("@type") != "messageText":
                continue
            text = (content.get("text") or {}).get("text", "").strip()
            if not text:
                continue
            await self._parse_into_tasks(text, message["id"])

    async def _parse_into_tasks(self, text: str, msg_id: int) -> list[dict]:
        """LLM раскладывает сообщение в задачи: {text, when, delegate}.

        Повторный разбор того же сообщения (msg_id) задач не создаёт.
        """
        if msg_id in self._seen_msg_ids:
            return []
        self._seen_msg_ids.add(msg_id)
        instruction = self.app.prompts.get("task_parse")
        try:
            data = await self.app._chat_resilient_json(
                [{"role": "system", "content": instruction or DEFAULT_TASK_PARSE},
                 {"role": "user", "content": text}],
                function_label="tasks",
            )
        except Exception:
            log.warning("парсер задач не справился с: %s", text[:60])
            return []
        tasks = data.get("tasks") if isinstance(data, dict) else None
        if not isinstance(tasks, list):
            return []
        now = time.time()
        created = []
        for t in tasks[:10]:
            if not isinstance(t, dict) or not str(t.get("text") or "").strip():
                continue
            when = str(t.get("when") or "today").lower()
            delegate = str(t.get("delegate") or "").lower()
            if delegate not in ("agy", "self"):
                delegate = "agy" if self._looks_technical(str(t["text"])) else "self"
            due = now
            if when == "tomorrow":
                due = now + 86400
            elif when not in ("today", "now"):
                due = now  # дата парсится грубо — исполнители всё равно стартуют по расписанию
            cur = self.db.execute(
                "INSERT INTO tasks (msg_id, text, assignee, due_ts, status, created_ts) "
                "VALUES (?,?,?,?, 'pending', ?)",
                (msg_id, str(t["text"]).strip()[:1000], delegate, due, now))
            self.db.commit()
            task = {"id": cur.lastrowid, "text": str(t["text"]).strip(),
                    "assignee": delegate, "when": when}
            created.append(task)
            self.bus.emit("task.created", **task)
        if created:
            log.info("из сообщения %s создано задач: %d", msg_id, len(created))
        return created

    @staticmethod
    def _looks_technical(text: str) -> bool:
        lowered = text.lower()
        return any(marker in lowered for marker in TECH_MARKERS)

    # ------------------------------------------------------------- исполнение

    async def run_due(self) -> None:
        """Исполняет pending-задачи, время которых пришло.

        Захват атомарный: `UPDATE ... WHERE status='pending'` с проверкой
        rowcount. Иначе два запуска (или повторный тик) берут одну задачу,
        выполняют её дважды и дважды отчитываются — это видно в логах.
        """
        if self.mode() != "assistant":
            return
        rows = self.db.execute(
            "SELECT id, text, assignee FROM tasks "
            "WHERE status='pending' AND due_ts <= ? ORDER BY id",
            (time.time(),)).fetchall()
        for task_id, text, assignee in rows:
            claimed = self.db.execute(
                "UPDATE tasks SET status='running' WHERE id=? AND status='pending'",
                (task_id,))
            self.db.commit()
            if claimed.rowcount != 1:
                log.debug("задача %s уже взята другим тиком — пропускаю", task_id)
                continue
            self.bus.emit("task.started", id=task_id, text=text[:80], assignee=assignee)
            try:
                if assignee == "agy":
                    result = await self._run_agy(text)
                else:
                    result = await self._run_self(text)
                status = "done"
            except Exception as e:
                result, status = f"ошибка: {str(e)[:300]}", "failed"
            self.db.execute("UPDATE tasks SET status=?, result=? WHERE id=?",
                            (status, result[:4000], task_id))
            self.db.commit()
            self.bus.emit("task.finished", id=task_id, status=status, result=result[:200])
            await self._report(task_id, text, status, result)

    def recover_stale(self, older_than_sec: float = 1800.0) -> int:
        """Вернуть в pending задачи, зависшие в running после падения.

        Без этого задача навсегда остаётся 'running' и больше не выполнится.
        """
        cutoff = time.time() - older_than_sec
        cur = self.db.execute(
            "UPDATE tasks SET status='pending' "
            "WHERE status='running' AND created_ts < ?", (cutoff,))
        self.db.commit()
        if cur.rowcount:
            log.warning("восстановлено зависших задач: %d", cur.rowcount)
        return cur.rowcount

    def reset_all_running(self) -> int:
        """Сбросить все 'running' при старте: они остались от прошлого процесса."""
        cur = self.db.execute("UPDATE tasks SET status='pending' WHERE status='running'")
        self.db.commit()
        if cur.rowcount:
            log.warning("сброшено задач из 'running' при старте: %d", cur.rowcount)
        return cur.rowcount

    async def _run_agy(self, task_text: str) -> str:
        """Делегирование технической задачи в Antigravity CLI.

        Логика запуска живёт в одном месте — скилле antigravity
        (`kuni/skills/antigravity.py`). Здесь только маршрутизация:
        дублировать subprocess-код незачем.
        """
        skill = self.app.skills.get("antigravity")
        if skill is None:
            raise RuntimeError("скилл antigravity не зарегистрирован")
        outcome = await skill.execute({"task": task_text})
        if not outcome.ok:
            raise RuntimeError(outcome.output[:300])
        return outcome.output

    async def _run_self(self, task_text: str) -> str:
        """Нетехническая задача: сама, своими скиллами (web_search/draw/память...)."""
        system = self.app.persona.build(
            extra="ASSISTANT MODE: this is a TASK from your creator, execute it with your "
                  "tools (web_search, draw, remember) and return a short result in Russian.")
        try:
            reply, _ = await self.app._chat_with_tools(
                [{"role": "system", "content": system},
                 {"role": "user", "content": f"Задача: {task_text}"}],
                chat_label="assistant")
        except Exception as e:
            return f"сама не справилась: {e}"
        return reply

    async def _report(self, task_id: int, text: str, status: str, result: str) -> None:
        cfg = self.app.config.current.assistant
        if not cfg.report_to_papik:
            return
        papik = self.app.config.current.general.papik_chat_id
        if not papik:
            return
        mark = "✅" if status == "done" else "❌"
        message = f"{mark} задача #{task_id} «{text[:60]}» — {status}\n{result[:500]}"
        try:
            # отчёты о работе идут сразу, без театра набора текста
            await self.app.telegram.send_text(papik, message)
        except Exception:
            log.warning("отчёт в личку не ушёл")

    # -------------------------------------------------------------- просмотр

    def snapshot(self) -> list[dict]:
        rows = self.db.execute(
            "SELECT id, text, assignee, status, result, due_ts FROM tasks "
            "ORDER BY id DESC LIMIT 30").fetchall()
        return [{"id": r[0], "text": r[1], "assignee": r[2], "status": r[3],
                 "result": (r[4] or "")[:200], "due_ts": r[5]} for r in rows]


DEFAULT_TASK_PARSE = (
    "Разбери сообщение-поручение на задачи. Ответь СТРОГО JSON: "
    '{"tasks": [{"text": "чёткая формулировка задачи", "when": "today|tomorrow|now", '
    '"delegate": "agy|self"}]}. agy — технические (код, файлы, скрипты, деплой), '
    "self — остальное. Нет задач — пустой массив."
)
