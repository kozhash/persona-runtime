"""Эпизодическая память и ассоциативный граф (CA3) + семантика (неокортекс).

Три уровня, как в CLS:
  * эпизод  — сырое событие, «односнимковое» кодирование (гиппокамп);
  * пропозиция — атомарный факт из эпизода (DG, разделение паттернов);
  * ребро    — ассоциация с весом Хебба (CA3, завершение паттернов).

Плюс два механизма, которых не было в старой памяти:
  * синаптическое затухание неиспользуемых рёбер;
  * битемпоральные факты: valid-time (когда верно) и transaction-time
    (когда узнали) — чтобы «Жан» мог перестать быть «злым» без потери истории.

Хранилище ничего не знает о LLM: эмбеддинги приходят снаружи.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

import numpy as np

from .schema import connect

log = logging.getLogger(__name__)

# Параметры пластичности Хебба (HeLa-Mem)
HEBBIAN_DECAY = 0.02        # lambda: затухание неиспользуемых рёбер за проход
HEBBIAN_RATE = 0.15         # eta: подкрепление при ко-активации
EDGE_MAX = 3.0              # потолок веса
EDGE_MIN = 0.01             # порог, ниже которого ребро удаляется
SAME_TAGS_LIMIT = 6

KIND_ENTITY = "ENTITY_DESCRIPTION"
KIND_THOUGHT = "THOUGHT"
KIND_EVENT = "EVENT"
KIND_FACT = "FACT"
KIND_OTHER = "OTHER"


@dataclass
class Episode:
    episode_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    ts: float = field(default_factory=time.time)
    text: str = ""
    source: str = "chat"
    speaker: str = ""
    chat_id: int = 0
    #: атрибуция (27.10): кто именно сказал и где это было
    speaker_id: int = 0
    speaker_name: str = ""
    chat_title: str = ""
    stage: str = "labile"
    confidence: float = 0.0
    importance: float = 0.0
    surprise: float = 0.0
    emotion_valence: float = 0.0
    emotion_arousal: float = 0.0
    embedding: list[float] | None = None
    tags: list[str] = field(default_factory=list)
    provenance: str = ""
    # аффективная подпись (версия схемы 4)
    provenance_class: str = "OBSERVATION"
    pad_p: float = 0.0
    pad_a: float = 0.0
    pad_dom: float = 0.0
    actr_valuation: float = 0.0
    is_gold_anchor: bool = False
    encoding_monoamines: str = ""     # JSON {"s","n","d"}
    original_ts: float | None = None
    access_count: int = 0
    last_access: float = 0.0
    checksum: str = ""

    @property
    def when(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(self.ts))

    @property
    def entry_id(self) -> str:
        """Совместимость со старым API (DiaryEntry.entry_id)."""
        return self.episode_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.episode_id,      # совместимость со старым API
            "episode_id": self.episode_id,
            "text": self.text,
            "ts": self.ts,
            "when": self.when,
            "source": self.source,
            "speaker": self.speaker,
            "chat_id": self.chat_id,
            "stage": self.stage,
            "confidence": self.confidence,
            "importance": self.importance,
            "surprise": self.surprise,
            "valence": self.emotion_valence,
            "arousal": self.emotion_arousal,
            "tags": list(self.tags),
            "provenance_class": self.provenance_class,
            "pad": {"p": self.pad_p, "a": self.pad_a, "dom": self.pad_dom},
            "actr_valuation": self.actr_valuation,
            "is_gold_anchor": self.is_gold_anchor,
        }


@dataclass
class Proposition:
    prop_id: str
    episode_id: str
    text: str
    kind: str = KIND_OTHER
    entities: list[str] = field(default_factory=list)
    confidence: float = 0.0
    embedding: list[float] | None = None
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "prop_id": self.prop_id, "episode_id": self.episode_id,
            "text": self.text, "kind": self.kind,
            "entities": list(self.entities), "confidence": self.confidence,
            "ts": self.ts,
        }


@dataclass
class Fact:
    fact_id: str
    subject: str
    predicate: str
    object: str
    serial: int = 0
    confidence: float = 0.0
    origin_episode: str = ""
    valid_from: float = 0.0
    valid_to: float | None = None
    txn_from: float = 0.0
    txn_to: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "fact_id": self.fact_id, "subject": self.subject,
            "predicate": self.predicate, "object": self.object,
            "serial": self.serial, "confidence": self.confidence,
            "valid_from": self.valid_from, "valid_to": self.valid_to,
            "txn_from": self.txn_from, "txn_to": self.txn_to,
        }


def _pack(vec: Sequence[float] | None) -> bytes | None:
    if not vec:
        return None
    return np.asarray(vec, dtype=np.float32).tobytes()


def _unpack(blob: bytes | None) -> list[float] | None:
    if not blob:
        return None
    return np.frombuffer(blob, dtype=np.float32).tolist()


def _checksum(text: str) -> str:
    """Дешёвая метка неизменности содержимого (не криптография)."""
    import hashlib
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:16]


class EpisodicStore:
    """Эпизоды + граф + факты в одной SQLite-базе."""

    def __init__(self, db_path: Any) -> None:
        self.db: sqlite3.Connection = connect(db_path)

    # ------------------------------------------------------------ эпизоды

    def add_episode(self, episode: Episode) -> Episode:
        episode.checksum = _checksum(episode.text)
        self.db.execute(
            """INSERT OR REPLACE INTO episodes
               (episode_id, ts, text, source, speaker, chat_id, stage, confidence,
                importance, surprise, emotion_valence, emotion_arousal, embedding,
                tags, provenance, access_count, last_access, checksum,
                provenance_class, pad_p, pad_a, pad_dom, actr_valuation,
                is_gold_anchor, encoding_monoamines, original_ts,
                speaker_id, speaker_name, chat_title)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (episode.episode_id, episode.ts, episode.text, episode.source,
             episode.speaker, episode.chat_id, episode.stage, episode.confidence,
             episode.importance, episode.surprise, episode.emotion_valence,
             episode.emotion_arousal, _pack(episode.embedding),
             ",".join(episode.tags), episode.provenance, episode.access_count,
             episode.last_access, episode.checksum,
             episode.provenance_class, episode.pad_p, episode.pad_a,
             episode.pad_dom, episode.actr_valuation,
             int(episode.is_gold_anchor), episode.encoding_monoamines,
             episode.original_ts,
             episode.speaker_id, episode.speaker_name, episode.chat_title),
        )
        self.db.commit()
        # первый доступ = момент кодирования (нужно для ACT-R)
        self.record_access(episode.episode_id, episode.ts, source="encoding")
        return episode

    def add(self, episode: Episode) -> Episode:
        """Совместимость со старым API DiaryStore.add(entry)."""
        return self.add_episode(episode)

    def get(self, episode_id: str) -> Episode | None:
        """Совместимость со старым API DiaryStore.get(entry_id)."""
        return self.get_episode(episode_id)

    def linked(self, episode_id: str, limit: int = 3) -> list[tuple[str, float]]:
        """Совместимость: прежний linked() отдавал [(id, weight)]."""
        return [(dst, weight) for dst, _kind, weight in
                self.neighbours(episode_id, limit=limit)]

    def same_tags(self, tags: list[str], exclude_ids: list[str],
                  limit: int = 2) -> list[Episode]:
        """Совместимость: раньше искали по общим тегам.

        В новой схеме теги заменены сущностями пропозиций, поэтому ищем
        по совпадению текста с перечисленными сущностями.
        """
        if not tags:
            return []
        found: list[Episode] = []
        seen: set[str] = set(exclude_ids)
        for tag in tags[:4]:
            for episode in self.search_text(tag, limit=limit * 4):
                if episode.episode_id in seen:
                    continue
                seen.add(episode.episode_id)
                found.append(episode)
                if len(found) >= limit:
                    return found
        return found

    def get_episode(self, episode_id: str) -> Episode | None:
        row = self.db.execute(
            """SELECT episode_id, ts, text, source, speaker, chat_id, stage,
                      confidence, importance, surprise, emotion_valence,
                      emotion_arousal, embedding, tags, provenance, access_count,
                      last_access, checksum, provenance_class, pad_p, pad_a,
                      pad_dom, actr_valuation, is_gold_anchor,
                      encoding_monoamines, original_ts,
                       speaker_id, speaker_name, chat_title
               FROM episodes WHERE episode_id=?""", (episode_id,)).fetchone()
        return self._row_to_episode(row) if row else None

    def delete(self, episode_id: str) -> bool:
        """Совместимость со старым API DiaryStore.delete(entry_id)."""
        return self.delete_episode(episode_id)

    def delete_episode(self, episode_id: str) -> bool:
        cur = self.db.execute("DELETE FROM episodes WHERE episode_id=?", (episode_id,))
        self.db.execute("DELETE FROM propositions WHERE episode_id=?", (episode_id,))
        self.db.execute("DELETE FROM edges WHERE src=? OR dst=?", (episode_id, episode_id))
        self.db.commit()
        return cur.rowcount > 0

    def update_text(self, episode_id: str, text: str) -> None:
        self.db.execute(
            "UPDATE episodes SET text=?, embedding=NULL, checksum=? WHERE episode_id=?",
            (text.strip(), _checksum(text), episode_id))
        self.db.commit()

    def recent(self, limit: int = 50) -> list[Episode]:
        rows = self.db.execute(
            """SELECT episode_id, ts, text, source, speaker, chat_id, stage,
                      confidence, importance, surprise, emotion_valence,
                      emotion_arousal, embedding, tags, provenance, access_count,
                      last_access, checksum, provenance_class, pad_p, pad_a,
                      pad_dom, actr_valuation, is_gold_anchor,
                      encoding_monoamines, original_ts,
                       speaker_id, speaker_name, chat_title
               FROM episodes ORDER BY ts DESC LIMIT ?""", (limit,)).fetchall()
        return [self._row_to_episode(r) for r in rows]

    def older_than(self, ts: float | None = None,
                   stage: str | None = None) -> list[Episode]:
        sql = ("""SELECT episode_id, ts, text, source, speaker, chat_id, stage,
                         confidence, importance, surprise, emotion_valence,
                         emotion_arousal, embedding, tags, provenance, access_count,
                         last_access, checksum, provenance_class, pad_p, pad_a,
                         pad_dom, actr_valuation, is_gold_anchor,
                         encoding_monoamines, original_ts,
                         speaker_id, speaker_name, chat_title
                  FROM episodes WHERE ts < ?""")
        params: list[Any] = [ts if ts is not None else time.time()]
        if stage:
            sql += " AND stage=?"
            params.append(stage)
        sql += " ORDER BY ts"
        rows = self.db.execute(sql, params).fetchall()
        return [self._row_to_episode(r) for r in rows]

    def count(self, stage: str | None = None) -> int:
        if stage:
            return int(self.db.execute(
                "SELECT COUNT(*) FROM episodes WHERE stage=?", (stage,)).fetchone()[0])
        return int(self.db.execute("SELECT COUNT(*) FROM episodes").fetchone()[0])

    def all_embeddings(self, stage: str | None = None) -> tuple[list[str], np.ndarray]:
        sql = "SELECT episode_id, embedding FROM episodes WHERE embedding IS NOT NULL"
        params: tuple[Any, ...] = ()
        if stage:
            sql += " AND stage=?"
            params = (stage,)
        rows = self.db.execute(sql, params).fetchall()
        if not rows:
            return [], np.zeros((0, 1), dtype=np.float32)
        ids = [r[0] for r in rows]
        matrix = np.vstack([np.frombuffer(r[1], dtype=np.float32) for r in rows])
        return ids, matrix

    def search_text(self, query: str, limit: int = 20) -> list[Episode]:
        rows = self.db.execute(
            """SELECT episode_id, ts, text, source, speaker, chat_id, stage,
                      confidence, importance, surprise, emotion_valence,
                      emotion_arousal, embedding, tags, provenance, access_count,
                      last_access, checksum, provenance_class, pad_p, pad_a,
                      pad_dom, actr_valuation, is_gold_anchor,
                      encoding_monoamines, original_ts
               FROM episodes WHERE text LIKE ? ORDER BY ts DESC LIMIT ?""",
            (f"%{query.strip()}%", limit)).fetchall()
        return [self._row_to_episode(r) for r in rows]

    def touch(self, episode_id: str, now: float | None = None) -> None:
        """Отметить обращение: усиливает след (practice effect)."""
        stamp = now or time.time()
        self.db.execute(
            "UPDATE episodes SET access_count=access_count+1, last_access=? "
            "WHERE episode_id=?", (stamp, episode_id))
        self.db.commit()
        self.record_access(episode_id, stamp, source="retrieval")

    # -------------------------------------------------------- ACT-R доступы

    def record_access(self, node_id: str, ts: float | None = None,
                      source: str = "retrieval") -> None:
        """Записать факт обращения во времени — основа ACT-R base-level.

        Без массива времён формула ln(sum t^-d) не считается корректно;
        одного last_access мало.
        """
        self.db.execute(
            "INSERT INTO memory_access (node_id, ts, source) VALUES (?,?,?)",
            (node_id, ts if ts is not None else time.time(), source))
        self.db.commit()

    def access_times(self, node_id: str, limit: int = 50) -> list[float]:
        rows = self.db.execute(
            "SELECT ts FROM memory_access WHERE node_id=? ORDER BY ts DESC LIMIT ?",
            (node_id, limit)).fetchall()
        return [float(r[0]) for r in rows]

    def access_map(self, limit_per_node: int = 20) -> dict[str, list[float]]:
        """Все времена обращений одним запросом — дешевле, чем по одному."""
        rows = self.db.execute(
            "SELECT node_id, ts FROM memory_access ORDER BY node_id, ts DESC"
        ).fetchall()
        result: dict[str, list[float]] = {}
        for node_id, ts in rows:
            bucket = result.setdefault(node_id, [])
            if len(bucket) < limit_per_node:
                bucket.append(float(ts))
        return result

    # --------------------------------------------------- аффект и происхождение

    def set_affect(self, episode_id: str, *, pad_p: float | None = None,
                   pad_a: float | None = None, pad_dom: float | None = None,
                   actr_valuation: float | None = None,
                   provenance_class: str | None = None,
                   is_gold_anchor: bool | None = None) -> None:
        """Обновить аффективные поля эпизода."""
        fields: list[str] = []
        params: list[Any] = []
        for name, value in (("pad_p", pad_p), ("pad_a", pad_a),
                            ("pad_dom", pad_dom),
                            ("actr_valuation", actr_valuation),
                            ("provenance_class", provenance_class),
                            ("is_gold_anchor", None if is_gold_anchor is None
                             else int(is_gold_anchor))):
            if value is not None:
                fields.append(f"{name}=?")
                params.append(value)
        if not fields:
            return
        params.append(episode_id)
        self.db.execute(f"UPDATE episodes SET {', '.join(fields)} WHERE episode_id=?",
                        params)
        self.db.commit()

    def demote(self, episode_id: str, *, confidence_delta: float = 0.0,
               valuation_multiplier: float = 1.0,
               stage: str | None = None) -> None:
        """Понизить значимость записи (санитарный аудит сна)."""
        self.db.execute(
            """UPDATE episodes SET
                   confidence = MAX(-1.0, MIN(1.0, confidence + ?)),
                   actr_valuation = MAX(0.0, actr_valuation * ?),
                   stage = COALESCE(?, stage)
               WHERE episode_id=?""",
            (confidence_delta, valuation_multiplier, stage, episode_id))
        self.db.commit()

    def search_by_pad(self, pad: tuple[float, float, float],
                      limit: int = 5, max_distance: float = 1.0,
                      require_arousal: float = 0.0,
                      exclude_self_inference: bool = False) -> list[Episode]:
        """Найти эпизоды с близкой аффективной подписью.

        Это замена текстового `LIKE '%грусть%'`: сравниваем числа, а не слова.
        """
        rows = self.db.execute(
            """SELECT episode_id, ts, text, source, speaker, chat_id, stage,
                      confidence, importance, surprise, emotion_valence,
                      emotion_arousal, embedding, tags, provenance, access_count,
                      last_access, checksum, provenance_class, pad_p, pad_a,
                      pad_dom, actr_valuation, is_gold_anchor,
                      encoding_monoamines, original_ts
               FROM episodes
               WHERE stage != 'archived' AND pad_a >= ?""",
            (require_arousal,)).fetchall()
        scored: list[tuple[float, Episode]] = []
        for row in rows:
            episode = self._row_to_episode(row)
            if exclude_self_inference and episode.provenance_class == "SELF_INFERENCE":
                continue
            distance = ((episode.pad_p - pad[0]) ** 2
                        + (episode.pad_a - pad[1]) ** 2
                        + (episode.pad_dom - pad[2]) ** 2) ** 0.5
            if distance <= max_distance:
                scored.append((distance, episode))
        scored.sort(key=lambda item: item[0])
        return [episode for _, episode in scored[:limit]]

    def gold_anchors(self, limit: int = 3, min_pleasure: float = 0.2) -> list[Episode]:
        """Положительные якоря для counterweight."""
        rows = self.db.execute(
            """SELECT episode_id, ts, text, source, speaker, chat_id, stage,
                      confidence, importance, surprise, emotion_valence,
                      emotion_arousal, embedding, tags, provenance, access_count,
                      last_access, checksum, provenance_class, pad_p, pad_a,
                      pad_dom, actr_valuation, is_gold_anchor,
                      encoding_monoamines, original_ts
               FROM episodes
               WHERE is_gold_anchor=1 AND pad_p >= ? AND stage != 'archived'
               ORDER BY pad_p DESC LIMIT ?""", (min_pleasure, limit)).fetchall()
        return [self._row_to_episode(r) for r in rows]

    def set_stage(self, episode_id: str, stage: str) -> None:
        self.db.execute("UPDATE episodes SET stage=? WHERE episode_id=?", (stage, episode_id))
        self.db.commit()

    @staticmethod
    def _row_to_episode(row: tuple) -> Episode:
        return Episode(
            episode_id=row[0], ts=row[1], text=row[2], source=row[3], speaker=row[4],
            chat_id=row[5], stage=row[6], confidence=row[7], importance=row[8],
            surprise=row[9], emotion_valence=row[10], emotion_arousal=row[11],
            embedding=_unpack(row[12]),
            tags=[t for t in (row[13] or "").split(",") if t],
            provenance=row[14], access_count=row[15],
            last_access=row[16], checksum=row[17],
            provenance_class=row[18] if len(row) > 18 else "OBSERVATION",
            pad_p=row[19] if len(row) > 19 else 0.0,
            pad_a=row[20] if len(row) > 20 else 0.0,
            pad_dom=row[21] if len(row) > 21 else 0.0,
            actr_valuation=row[22] if len(row) > 22 else 0.0,
            speaker_id=row[26] if len(row) > 26 else 0,
            speaker_name=row[27] if len(row) > 27 else "",
            chat_title=row[28] if len(row) > 28 else "",
            is_gold_anchor=bool(row[23]) if len(row) > 23 else False,
            encoding_monoamines=row[24] if len(row) > 24 else "",
            original_ts=row[25] if len(row) > 25 else None,
        )

    # --------------------------------------------------------- пропозиции

    def add_proposition(self, prop: Proposition) -> Proposition:
        self.db.execute(
            """INSERT OR REPLACE INTO propositions
               (prop_id, episode_id, text, kind, entities, confidence, embedding, ts)
               VALUES (?,?,?,?,?,?,?,?)""",
            (prop.prop_id, prop.episode_id, prop.text, prop.kind,
             json.dumps(prop.entities, ensure_ascii=False),
             prop.confidence, _pack(prop.embedding), prop.ts))
        self.db.commit()
        return prop

    def propositions_of(self, episode_id: str) -> list[Proposition]:
        rows = self.db.execute(
            """SELECT prop_id, episode_id, text, kind, entities, confidence,
                      embedding, ts FROM propositions WHERE episode_id=?
               ORDER BY ts""", (episode_id,)).fetchall()
        return [Proposition(
            prop_id=r[0], episode_id=r[1], text=r[2], kind=r[3],
            entities=json.loads(r[4]) if r[4] else [],
            confidence=r[5], embedding=_unpack(r[6]), ts=r[7]) for r in rows]

    def search_propositions(self, query: str, limit: int = 20) -> list[Proposition]:
        rows = self.db.execute(
            """SELECT prop_id, episode_id, text, kind, entities, confidence,
                      embedding, ts FROM propositions
               WHERE text LIKE ? ORDER BY ts DESC LIMIT ?""",
            (f"%{query.strip()}%", limit)).fetchall()
        return [Proposition(
            prop_id=r[0], episode_id=r[1], text=r[2], kind=r[3],
            entities=json.loads(r[4]) if r[4] else [],
            confidence=r[5], embedding=_unpack(r[6]), ts=r[7]) for r in rows]

    # -------------------------------------------------------------- граф

    def link(self, src: str, dst: str, kind: str = "hebbian",
             weight: float = 0.1, confidence: float = 0.5,
             valid_from: float | None = None, valid_to: float | None = None,
             now: float | None = None) -> None:
        """Создать или усилить ребро (правило Хебба: co-activation -> stronger)."""
        if src == dst:
            return
        stamp = now or time.time()
        existing = self.db.execute(
            "SELECT weight FROM edges WHERE src=? AND dst=? AND kind=?",
            (src, dst, kind)).fetchone()
        if existing:
            new_weight = min(EDGE_MAX, float(existing[0]) + weight)
            self.db.execute(
                "UPDATE edges SET weight=?, confidence=?, last_reinforced=? "
                "WHERE src=? AND dst=? AND kind=?",
                (new_weight, confidence, stamp, src, dst, kind))
        else:
            self.db.execute(
                """INSERT INTO edges (src, dst, kind, weight, confidence,
                                      valid_from, valid_to, txn_from, last_reinforced)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (src, dst, kind, weight, confidence,
                 valid_from if valid_from is not None else stamp, valid_to,
                 stamp, stamp))
        self.db.commit()

    def neighbours(self, node: str, limit: int = 10,
                   kinds: Iterable[str] | None = None) -> list[tuple[str, str, float]]:
        """Соседи узла: [(id, kind, weight)]. Отрицательные (prevents) сохраняются."""
        sql = "SELECT dst, kind, weight FROM edges WHERE src=?"
        params: list[Any] = [node]
        kinds_list = list(kinds or [])
        if kinds_list:
            sql += " AND kind IN (%s)" % ",".join("?" * len(kinds_list))
            params.extend(kinds_list)
        sql += " ORDER BY weight DESC LIMIT ?"
        params.append(limit)
        return [(r[0], r[1], float(r[2])) for r in self.db.execute(sql, params).fetchall()]

    def edge_weight(self, src: str, dst: str, kind: str = "hebbian") -> float:
        row = self.db.execute(
            "SELECT weight FROM edges WHERE src=? AND dst=? AND kind=?",
            (src, dst, kind)).fetchone()
        return float(row[0]) if row else 0.0

    def decay_edges(self, decay: float = HEBBIAN_DECAY,
                    now: float | None = None) -> int:
        """Синаптическое затухание: слабые неиспользуемые рёбра исчезают."""
        stamp = now or time.time()
        self.db.execute(
            "UPDATE edges SET weight = weight * (1.0 - ?) WHERE kind='hebbian'",
            (decay,))
        cur = self.db.execute("DELETE FROM edges WHERE weight <= ?", (EDGE_MIN,))
        self.db.commit()
        log.debug("затухание рёбер: удалено %d", cur.rowcount)
        return cur.rowcount

    def edge_count(self) -> int:
        return int(self.db.execute("SELECT COUNT(*) FROM edges").fetchone()[0])

    # ------------------------------------------------------------- факты

    def upsert_fact(self, subject: str, predicate: str, object_: str,
                    confidence: float = 0.5, origin_episode: str = "",
                    valid_from: float | None = None,
                    now: float | None = None) -> Fact:
        """Новый факт закрывает предыдущий по тому же (subject, predicate).

        Это детерминированное разрешение конфликтов вместо «пусть LLM решит»:
        старый факт получает valid_to, новый — больший serial.
        """
        stamp = now or time.time()
        prev = self.db.execute(
            """SELECT fact_id, serial FROM facts
               WHERE subject=? AND predicate=? AND valid_to IS NULL
               ORDER BY serial DESC LIMIT 1""",
            (subject, predicate)).fetchone()
        next_serial = 1
        if prev:
            next_serial = int(prev[1]) + 1
            self.db.execute(
                "UPDATE facts SET valid_to=?, txn_to=? WHERE fact_id=?",
                (stamp, stamp, prev[0]))
        else:
            row = self.db.execute("SELECT MAX(serial) FROM facts").fetchone()
            next_serial = (int(row[0]) + 1) if row and row[0] is not None else 1
        fact = Fact(
            fact_id=uuid.uuid4().hex[:12], subject=subject, predicate=predicate,
            object=object_, serial=next_serial, confidence=confidence,
            origin_episode=origin_episode,
            valid_from=valid_from if valid_from is not None else stamp,
            txn_from=stamp,
        )
        self.db.execute(
            """INSERT INTO facts (fact_id, subject, predicate, object, serial,
                                  confidence, origin_episode, valid_from, valid_to,
                                  txn_from, txn_to)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (fact.fact_id, fact.subject, fact.predicate, fact.object, fact.serial,
             fact.confidence, fact.origin_episode, fact.valid_from, None,
             fact.txn_from, None))
        self.db.commit()
        return fact

    def current_facts(self, subject: str | None = None) -> list[Fact]:
        sql = ("SELECT fact_id, subject, predicate, object, serial, confidence, "
               "origin_episode, valid_from, valid_to, txn_from, txn_to "
               "FROM facts WHERE valid_to IS NULL")
        params: list[Any] = []
        if subject:
            sql += " AND subject=?"
            params.append(subject)
        sql += " ORDER BY serial DESC"
        return [self._row_to_fact(r) for r in self.db.execute(sql, params).fetchall()]

    def resolve_conflict(self, subject: str, predicate: str) -> Fact | None:
        """Детерминированно: побеждает факт с максимальным serial."""
        row = self.db.execute(
            """SELECT fact_id, subject, predicate, object, serial, confidence,
                      origin_episode, valid_from, valid_to, txn_from, txn_to
               FROM facts WHERE subject=? AND predicate=?
               ORDER BY serial DESC LIMIT 1""", (subject, predicate)).fetchone()
        return self._row_to_fact(row) if row else None

    @staticmethod
    def _row_to_fact(row: tuple) -> Fact:
        return Fact(
            fact_id=row[0], subject=row[1], predicate=row[2], object=row[3],
            serial=row[4], confidence=row[5], origin_episode=row[6],
            valid_from=row[7], valid_to=row[8], txn_from=row[9], txn_to=row[10])

    # --------------------------------------------------- синаптические теги

    def tag(self, episode_id: str, strength: float, ttl_seconds: float = 7200.0,
            now: float | None = None) -> str:
        """Поставить синаптический тег (STC): живёт TTL без захвата."""
        stamp = now or time.time()
        tag_id = uuid.uuid4().hex[:12]
        self.db.execute(
            """INSERT INTO syn_tags (tag_id, episode_id, strength, created_at,
                                     expires_at, captured)
               VALUES (?,?,?,?,?,0)""",
            (tag_id, episode_id, strength, stamp, stamp + ttl_seconds))
        self.db.commit()
        return tag_id

    def live_tags(self, now: float | None = None) -> list[dict[str, Any]]:
        stamp = now or time.time()
        rows = self.db.execute(
            """SELECT tag_id, episode_id, strength, created_at, expires_at
               FROM syn_tags WHERE captured=0 AND expires_at > ?
               ORDER BY strength DESC""", (stamp,)).fetchall()
        return [{"tag_id": r[0], "episode_id": r[1], "strength": r[2],
                 "created_at": r[3], "expires_at": r[4]} for r in rows]

    def capture_tags(self, episode_ids: Iterable[str], now: float | None = None) -> int:
        """Захват PRP: помеченные эпизоды переводятся в долговременную память."""
        stamp = now or time.time()
        ids = list(episode_ids)
        if not ids:
            return 0
        placeholders = ",".join("?" * len(ids))
        cur = self.db.execute(
            f"UPDATE syn_tags SET captured=1, captured_at=? "
            f"WHERE captured=0 AND episode_id IN ({placeholders})", (stamp, *ids))
        for episode_id in ids:
            self.db.execute("UPDATE episodes SET stage='consolidated' WHERE episode_id=?",
                            (episode_id,))
        self.db.commit()
        return cur.rowcount

    def expire_tags(self, now: float | None = None) -> int:
        """Теги, не дождавшиеся захвата, распадаются (E-LTP гаснет)."""
        stamp = now or time.time()
        cur = self.db.execute(
            "DELETE FROM syn_tags WHERE captured=0 AND expires_at <= ?", (stamp,))
        self.db.commit()
        return cur.rowcount

    # ---------------------------------------------------------- статистика

    def stats(self) -> dict[str, Any]:
        ids, matrix = self.all_embeddings()
        return {
            "entries": self.count(),
            "labile": self.count("labile"),
            "consolidated": self.count("consolidated"),
            "embedded": int(matrix.shape[0]) if matrix.size else 0,
            "edges": self.edge_count(),
            "propositions": int(self.db.execute(
                "SELECT COUNT(*) FROM propositions").fetchone()[0]),
            "facts": int(self.db.execute("SELECT COUNT(*) FROM facts").fetchone()[0]),
            "live_tags": len(self.live_tags()),
        }
