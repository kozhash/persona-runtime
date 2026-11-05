"""Лог эмоциональных состояний: что повлияло и на какое сообщение.

Зачем отдельная таблица, а не расширение `emotions`: `emotions` хранит
снимки состояния для истории и восстановления после рестарта. Здесь —
журнал причин: какое событие, от кого, какое сообщение, как сдвинулись
оси. По нему можно ответить «почему она обиделась» через месяц.

Пишется на каждую смену состояния: appraisal-импульс, дельты моноаминов,
эмоция до и после, и привязка к message_id.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from typing import Any

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS emotion_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    chat_id INTEGER NOT NULL DEFAULT 0,
    message_id INTEGER NOT NULL DEFAULT 0,
    author_id INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT '',
    event TEXT NOT NULL DEFAULT '',
    appraisal TEXT NOT NULL DEFAULT '{}',
    mono_before TEXT NOT NULL DEFAULT '{}',
    mono_after TEXT NOT NULL DEFAULT '{}',
    delta TEXT NOT NULL DEFAULT '{}',
    emotion_before TEXT NOT NULL DEFAULT '{}',
    emotion_after TEXT NOT NULL DEFAULT '{}',
    reason TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_emotion_log_ts ON emotion_log(ts);
CREATE INDEX IF NOT EXISTS idx_emotion_log_chat ON emotion_log(chat_id);
"""


class EmotionLog:
    """Журнал «почему состояние изменилось»."""

    def __init__(self, db: sqlite3.Connection, bus: Any = None) -> None:
        self.db = db
        self.bus = bus
        self.db.executescript(SCHEMA)
        self.db.commit()

    def record(
        self,
        *,
        chat_id: int = 0,
        message_id: int = 0,
        author_id: int = 0,
        source: str = "",
        event: str = "",
        appraisal: dict[str, Any] | None = None,
        mono_before: dict[str, float] | None = None,
        mono_after: dict[str, float] | None = None,
        emotion_before: dict[str, Any] | None = None,
        emotion_after: dict[str, Any] | None = None,
        reason: str = "",
    ) -> int:
        before = mono_before or {}
        after = mono_after or {}
        delta = {}
        for axis in ("s", "n", "d"):
            if axis in before and axis in after:
                delta[axis] = round(after[axis] - before[axis], 4)

        cur = self.db.execute(
            """INSERT INTO emotion_log
               (ts, chat_id, message_id, author_id, source, event, appraisal,
                mono_before, mono_after, delta, emotion_before, emotion_after,
                reason)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                time.time(), int(chat_id), int(message_id), int(author_id),
                source, event,
                json.dumps(appraisal or {}, ensure_ascii=False),
                json.dumps(before, ensure_ascii=False),
                json.dumps(after, ensure_ascii=False),
                json.dumps(delta, ensure_ascii=False),
                json.dumps(emotion_before or {}, ensure_ascii=False),
                json.dumps(emotion_after or {}, ensure_ascii=False),
                reason[:500],
            ),
        )
        self.db.commit()
        if self.bus is not None:
            self.bus.emit("emotion.logged", chat_id=chat_id,
                          message_id=message_id, event=event,
                          delta=delta, reason=reason[:160])
        return int(cur.lastrowid or 0)

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        """Последние записи — для дашборда: что на неё влияло."""
        rows = self.db.execute(
            """SELECT ts, chat_id, message_id, author_id, source, event,
                      appraisal, delta, emotion_before, emotion_after, reason
               FROM emotion_log ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            def _load(idx: int) -> Any:
                try:
                    return json.loads(row[idx] or "{}")
                except (json.JSONDecodeError, TypeError):
                    return {}
            out.append({
                "ts": row[0], "chat_id": row[1], "message_id": row[2],
                "author_id": row[3], "source": row[4], "event": row[5],
                "appraisal": _load(6), "delta": _load(7),
                "emotion_before": _load(8), "emotion_after": _load(9),
                "reason": row[10],
            })
        return out

    def for_message(self, chat_id: int, message_id: int) -> list[dict[str, Any]]:
        """Всё, что сообщение сделало с её состоянием."""
        rows = self.db.execute(
            """SELECT ts, event, delta, reason FROM emotion_log
               WHERE chat_id=? AND message_id=? ORDER BY id""",
            (chat_id, message_id),
        ).fetchall()
        return [{"ts": r[0], "event": r[1], "delta": r[2], "reason": r[3]}
                for r in rows]
