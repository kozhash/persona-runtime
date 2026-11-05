"""Ночная очередь: сообщения, пришедшие во время сна.

Раньше `handle_message` при ночном режиме писал в лог «прочтётся утром»
и возвращал управление. Никакой очереди не было — сообщение терялось
навсегда. В переписке это выглядело как игнорирование.

Теперь: ночью сообщение кладётся в очередь, а утром она разбирается
одной пачкой. Ответ приходит один — на всю накопленную переписку,
а не по сообщению на каждое. Это и человечнее, и дешевле.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class QueuedMessage:
    chat_id: int
    message_id: int
    sender_user_id: int
    text: str
    ts: float = field(default_factory=time.time)
    chat_title: str = ""
    chat_type: str = ""
    is_voice: bool = False
    is_photo: bool = False
    is_sticker: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "chat_id": self.chat_id,
            "message_id": self.message_id,
            "sender_user_id": self.sender_user_id,
            "text": self.text,
            "ts": self.ts,
            "chat_title": self.chat_title,
            "chat_type": self.chat_type,
            "is_voice": self.is_voice,
            "is_photo": self.is_photo,
            "is_sticker": self.is_sticker,
        }


class NightQueue:
    """Постоянная очередь на SQLite: переживает перезапуск."""

    def __init__(self, db_path: Path) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path))
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS night_queue (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   chat_id INTEGER NOT NULL,
                   message_id INTEGER NOT NULL,
                   sender_user_id INTEGER NOT NULL DEFAULT 0,
                   text TEXT NOT NULL DEFAULT '',
                   ts REAL NOT NULL,
                   chat_title TEXT NOT NULL DEFAULT '',
                   chat_type TEXT NOT NULL DEFAULT '',
                   is_voice INTEGER NOT NULL DEFAULT 0,
                   is_photo INTEGER NOT NULL DEFAULT 0,
                   is_sticker INTEGER NOT NULL DEFAULT 0,
                   handled INTEGER NOT NULL DEFAULT 0
               )"""
        )
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_nq_chat ON night_queue(chat_id, handled, ts)")
        self.db.commit()

    def push(self, message: QueuedMessage) -> None:
        self.db.execute(
            """INSERT INTO night_queue
               (chat_id, message_id, sender_user_id, text, ts, chat_title,
                chat_type, is_voice, is_photo, is_sticker, handled)
               VALUES (?,?,?,?,?,?,?,?,?,?,0)""",
            (message.chat_id, message.message_id, message.sender_user_id,
             message.text, message.ts, message.chat_title, message.chat_type,
             int(message.is_voice), int(message.is_photo), int(message.is_sticker)),
        )
        self.db.commit()
        log.info("ночное сообщение в очередь: чат %s, %d симв.",
                 message.chat_id, len(message.text))

    def by_chat(self) -> dict[int, list[QueuedMessage]]:
        """Необработанные сообщения, сгруппированные по чату."""
        rows = self.db.execute(
            """SELECT chat_id, message_id, sender_user_id, text, ts, chat_title,
                      chat_type, is_voice, is_photo, is_sticker
               FROM night_queue WHERE handled=0 ORDER BY chat_id, ts"""
        ).fetchall()
        grouped: dict[int, list[QueuedMessage]] = {}
        for row in rows:
            message = QueuedMessage(
                chat_id=row[0], message_id=row[1], sender_user_id=row[2],
                text=row[3], ts=row[4], chat_title=row[5], chat_type=row[6],
                is_voice=bool(row[7]), is_photo=bool(row[8]),
                is_sticker=bool(row[9]),
            )
            grouped.setdefault(message.chat_id, []).append(message)
        return grouped

    def mark_handled(self, chat_id: int, message_ids: list[int] | None = None) -> int:
        if message_ids is None:
            cur = self.db.execute(
                "UPDATE night_queue SET handled=1 WHERE chat_id=? AND handled=0",
                (chat_id,))
        else:
            if not message_ids:
                return 0
            placeholders = ",".join("?" * len(message_ids))
            cur = self.db.execute(
                f"UPDATE night_queue SET handled=1 WHERE chat_id=? "
                f"AND message_id IN ({placeholders})",
                (chat_id, *message_ids))
        self.db.commit()
        return cur.rowcount

    def pending_count(self) -> int:
        return int(self.db.execute(
            "SELECT COUNT(*) FROM night_queue WHERE handled=0").fetchone()[0])

    def purge(self, older_than_sec: float = 7 * 86400) -> int:
        """Убрать обработанное старше недели, чтобы база не росла."""
        cutoff = time.time() - older_than_sec
        cur = self.db.execute(
            "DELETE FROM night_queue WHERE handled=1 AND ts < ?", (cutoff,))
        self.db.commit()
        return cur.rowcount
