"""Дневник — фасад над эпизодической памятью.

Совместим по смыслу со старым API (add / recall / flash / stats), но
внутри работает новая модель:

  add()    — эпизод попадает в лабильный буфер (E-LTP) и получает
             синаптический тег. Тяжёлая работа не делается на входе:
             это защита от write-time regret.
  recall() — полный двухпутевой поиск (семантика + лексика + граф).
  flash()  — непроизвольное вспоминание: слова + настроение + ассоциации.
  promote()— явный захват (PRP) сейчас, не дожидаясь ночи.

Эмбеддинги приходят извне (embedder), хранилище — EpisodicStore.
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Sequence

from . import decay as decay_mod
from .episodic import (KIND_EVENT, KIND_OTHER, Episode, EpisodicStore,
                       Proposition)
from .retrieval import dual_path_retrieve, lexical_scores, tokenize

log = logging.getLogger(__name__)

LABILE_TTL = 7200.0          # 2 часа: сколько живёт тег без захвата
AUTO_ADMIT = 0.62            # балл, при котором эпизод захватывается сразу
FLASH_FRESH_WINDOW = 300.0   # не всплывать раньше N секунд после записи


class Diary:
    """Долговременная память с ассоциативным графом и лабильным буфером."""

    def __init__(self, store: EpisodicStore, embedder: Any, bus: Any | None = None) -> None:
        self.store = store
        self.embedder = embedder
        self.bus = bus

    # ------------------------------------------------------------- запись

    async def add(self, text: str, *, tags: list[str] | None = None,
                  source: str = "chat", chat_id: int = 0, speaker: str = "",
                  importance: float | None = None, surprise: float = 0.0,
                  emotion: float = 0.0, provenance: str = "",
                  kind: str = KIND_EVENT,
                  promote: bool | None = None,
                  speaker_id: int = 0, speaker_name: str = "",
                  chat_title: str = "") -> Episode:
        """Записать эпизод. По умолчанию — в лабильный буфер с тегом."""
        text = (text or "").strip()
        if not text:
            raise ValueError("пустая запись дневника")
        stamp = time.time()
        episode = Episode(
            episode_id=uuid.uuid4().hex[:12],
            ts=stamp, text=text, source=source, chat_id=chat_id,
            speaker=speaker, stage="labile",
            speaker_id=speaker_id, speaker_name=speaker_name,
            chat_title=chat_title,
            importance=importance if importance is not None else 0.4,
            surprise=max(0.0, min(1.0, surprise)),
            emotion_valence=0.0,
            emotion_arousal=max(0.0, min(1.0, emotion)),
            provenance=provenance or source,
        )
        if self.embedder is not None:
            try:
                episode.embedding = await self.embedder.embed_one(text)
            except Exception:
                log.warning("эмбеддинг не получен — запись найдётся только по тексту")
                episode.embedding = None
        self.store.add_episode(episode)

        # ассоциативные рёбра на входе — только дешёвые, по готовым векторам
        if episode.embedding:
            self._link_new(episode)

        # синаптический тег: кандидат на ночную консолидацию
        tag_strength = decay_mod.score_for_admission(
            importance=episode.importance, surprise=episode.surprise,
            emotion=episode.emotion_arousal, novelty=1.0 if not episode.embedding else 0.6)
        self.store.tag(episode.episode_id, tag_strength, ttl_seconds=LABILE_TTL, now=stamp)

        # сильный сигнал захватывается сразу, не дожидаясь сна
        if promote is True or (promote is None and tag_strength >= AUTO_ADMIT):
            self.store.capture_tags([episode.episode_id], now=stamp)

        if self.bus:
            self.bus.emit("diary.written", entry=episode.to_dict())
        log.info("дневник +%s [%s] tag=%.2f %s",
                 episode.episode_id, source, tag_strength, text[:70].replace("\n", " "))
        return episode

    def _link_new(self, episode: Episode) -> None:
        """Связать новый эпизод с ближайшими по смыслу (рёбра Хебба)."""
        try:
            ids, matrix = self.store.all_embeddings()
            if matrix.size == 0 or not ids:
                return
            index_of = {eid: i for i, eid in enumerate(ids)}
            row = index_of.get(episode.episode_id)
            if row is None:
                return
            from .retrieval import cosine_scores
            import numpy as np
            sims = cosine_scores(matrix[row], matrix)
            order = np.argsort(-sims)
            linked = 0
            for idx in order:
                candidate = ids[int(idx)]
                if candidate == episode.episode_id:
                    continue
                similarity = float(sims[int(idx)])
                if similarity < 0.45:
                    break
                self.store.link(episode.episode_id, candidate, kind="hebbian",
                                weight=0.2 * similarity, confidence=similarity)
                linked += 1
                if linked >= 5:
                    break
        except Exception:
            log.exception("связи графа не записались — не критично")

    async def promote(self, episode_id: str) -> bool:
        """Явный захват: эпизод закрепляется в долговременной памяти сейчас."""
        return self.store.capture_tags([episode_id]) > 0

    # ------------------------------------------------------------- чтение

    def _neighbour_map(self, ids: Sequence[str], hops: int = 2) -> dict[str, list]:
        """Собрать окрестности для flip-пути (в пределах среза)."""
        allowed = set(ids)
        result: dict[str, list] = {}
        for episode_id in ids:
            neighbours = self.store.neighbours(episode_id, limit=8)
            filtered = [n for n in neighbours if n[0] in allowed]
            if filtered:
                result[episode_id] = filtered
        return result

    async def recall(self, query: str, k: int = 5,
                     min_similarity: float = 0.0) -> list[dict[str, Any]]:
        """Похожие воспоминания по смыслу И по ассоциациям."""
        nodes = await self._search(query, top_k=k)
        return [n.to_dict() for n in nodes]

    async def _search(self, query: str, top_k: int = 5,
                      include_archived: bool = False,
                      now: float | None = None) -> list:
        episodes = self.store.recent(limit=4000)
        if not include_archived:
            episodes = [e for e in episodes if e.stage != "archived"]
        if not episodes:
            return []
        ids = [e.episode_id for e in episodes]
        texts = [e.text for e in episodes]
        stamps = [e.ts for e in episodes]
        kinds = [KIND_OTHER] * len(episodes)
        # 27.10: атрибуция идёт параллельным массивом вместе с текстами —
        # иначе на выходе поиска теряется, кто это сказал и где.
        attributions = [
            {"chat_id": e.chat_id, "speaker_id": e.speaker_id,
             "speaker_name": e.speaker_name, "chat_title": e.chat_title}
            for e in episodes
        ]
        _, matrix = self.store.all_embeddings()
        all_ids, _ = self.store.all_embeddings()
        index_of = {eid: i for i, eid in enumerate(all_ids)}
        rows = [index_of[i] for i in ids if i in index_of]
        import numpy as np
        sub = matrix[rows] if rows else np.zeros((0, 1), dtype=np.float32)

        query_vec = None
        if self.embedder is not None:
            try:
                query_vec = await self.embedder.embed_one(query)
            except Exception:
                log.warning("recall: эмбеддинг недоступен, ищу по тексту")
        return dual_path_retrieve(
            query=query, query_embedding=query_vec,
            episode_ids=ids, texts=texts, timestamps=stamps, kinds=kinds,
            matrix=sub, neighbours=self._neighbour_map(ids),
            top_k=top_k, now=now, attributions=attributions,
        )

    def recall_sync_fallback(self, query: str, k: int = 3) -> list[dict[str, Any]]:
        """Без эмбеддингов: лексический поиск по всем эпизодам."""
        tokens = tokenize(query)
        if not tokens:
            return []
        episodes = self.store.recent(limit=2000)
        if not episodes:
            return []
        scores = lexical_scores(tokens, [e.text for e in episodes])
        order = sorted(range(len(episodes)), key=lambda i: -scores[i])[:k]
        return [episodes[i].to_dict() for i in order if scores[i] > 0]

    async def flash(self, stimulus: str, emotion: Any = None, k: int = 4,
                    fresh_window: float = FLASH_FRESH_WINDOW) -> list[dict[str, Any]]:
        """Непроизвольное вспоминание — три пути, как у человека.

        1) слова собеседника цепляют сцены (семантика + лексика);
        2) настроение тянет воспоминания того же тона;
        3) ассоциативная цепочка ведёт от всплывшего дальше.
        """
        stamp = time.time()
        collected: list[dict[str, Any]] = []
        seen: set[str] = set()

        def push(item: dict[str, Any], via: str) -> None:
            episode_id = item.get("episode_id") or item.get("entry_id")
            if not episode_id or episode_id in seen or len(collected) >= k:
                return
            if fresh_window > 0 and stamp - float(item.get("ts", 0)) < fresh_window:
                return
            item["via"] = via
            seen.add(episode_id)
            collected.append(item)

        # 1) прямой триггер
        for node in await self._search(stimulus or "", top_k=max(2, k), now=stamp):
            push(node.to_dict(), node.via or "слово зацепило")

        # 2) настроение тянет своё
        intensity = float(getattr(emotion, "intensity", 0) or 0)
        emotion_word = str(getattr(emotion, "emotion", "") or "")
        if intensity >= 0.4 and emotion_word:
            word = emotion_word.split()[0]
            for episode in self.store.search_text(word, limit=4):
                push(episode.to_dict(), f"настроение потянуло ({word})")

        # 3) ассоциативная цепочка от уже всплывшего
        for seed in list(collected[:2]):
            seed_id = seed.get("episode_id")
            if not seed_id:
                continue
            for neighbour_id, kind, weight in self.store.neighbours(seed_id, limit=3):
                if kind == "prevents" or weight <= 0:
                    continue
                neighbour = self.store.get_episode(neighbour_id)
                if neighbour:
                    push(neighbour.to_dict(), "одно потянуло другое")

        # обращения усиливают след (practice effect)
        for item in collected:
            episode_id = item.get("episode_id")
            if episode_id:
                self.store.touch(episode_id, stamp)

        log.info("всплыло %d воспоминаний: %s", len(collected),
                 "; ".join(f"[{c['via']}] {c['text'][:36]}" for c in collected))
        return collected

    async def remember_fact(self, subject: str, predicate: str, obj: str,
                            confidence: float = 0.7,
                            origin_episode: str = "") -> Any:
        """Записать семантический факт с разрешением конфликтов по serial."""
        fact = self.store.upsert_fact(subject, predicate, obj,
                                      confidence=confidence,
                                      origin_episode=origin_episode)
        if self.bus:
            self.bus.emit("memory.fact", **fact.to_dict())
        return fact

    def stats(self) -> dict[str, Any]:
        return self.store.stats()
