"""Сон-консолидация: NREM (переигровка), REM (дистилляция), забывание.

Три фазы, по ресерчу (SCM, SleepGate, HeLa-Mem):

  NREM   — «реплей»: помеченные синаптическими тегами эпизоды захватываются
           в долговременную память, рёбра между ними усиливаются.
  REM    — дистилляция: плотные кластеры (хабы) сжимаются в семантические
           пропозиции; изолированные узлы помечаются на забывание.
  FORGET — гомеостатическое забывание: слабые рёбра затухают, эпизоды
           с низкой доступностью уходят в архив, мёртвые теги распадаются.

Логика принятия решений о слиянии — в LLM (через переданный callable),
но вся механика (что считать хабом, что забывать, как обновлять веса)
детерминирована. LLM здесь не решает «что правда», только «как сказать
короче».
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Sequence

import numpy as np

from . import decay as decay_mod
from .episodic import Episode, EpisodicStore, KIND_FACT, KIND_OTHER, Proposition
from .retrieval import cosine_scores

log = logging.getLogger(__name__)

FACT_THRESHOLD = 0.6        # схожесть, при которой эпизоды считаются одним кластером
HUB_MIN_DEGREE = 2          # минимум связей, чтобы узел считался хабом
FORGET_THRESHOLD = 0.15     # доступность ниже этой — в архив
MAX_CLUSTER = 8

DistillFn = Callable[[list[Episode]], Awaitable[dict[str, Any]]]


@dataclass
class ConsolidationStats:
    started_at: float = 0.0
    finished_at: float = 0.0
    captured: int = 0
    replay_pairs: int = 0
    clusters: int = 0
    distilled: int = 0
    archived: int = 0
    edges_decayed: int = 0
    edges_pruned: int = 0
    tags_expired: int = 0
    facts_written: int = 0
    working_memory_dropped: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def seconds(self) -> float:
        return self.finished_at - self.started_at

    def to_dict(self) -> dict[str, Any]:
        data = dict(self.__dict__)
        data["seconds"] = round(self.seconds, 1)
        data["working_memory_dropped"] = self.working_memory_dropped
        data.update({
            "merged": self.distilled, "rewritten": 0, "discarded": self.archived,
        })
        return data


class Consolidator:
    """Ночной цикл: три фазы над EpisodicStore."""

    def __init__(self, store: EpisodicStore, *, working: Any = None,
                 embedder: Any = None, distill: DistillFn | None = None,
                 bus: Any = None) -> None:
        self.store = store
        self.working = working
        self.embedder = embedder          # объект с async embed_one(text)->vec
        self.distill = distill            # LLM-функция: кластер -> {text, kind}
        self.bus = bus
        self.last_stats: ConsolidationStats | None = None
        self.sleeping = False

    async def run(self, similarity_threshold: float = FACT_THRESHOLD,
                  lookback_days: float = 2.0,
                  now: float | None = None) -> ConsolidationStats:
        stamp = now or time.time()
        stats = ConsolidationStats(started_at=stamp)
        self.sleeping = True
        if self.bus:
            self.bus.emit("sleep.started")
        try:
            stats.captured = await self._nrem(stamp)
            stats.replay_pairs = self._replay(stamp)
            await self._rem(stats, similarity_threshold, lookback_days, stamp)
            self._forget(stats, stamp)
            if self.working is not None:
                stats.working_memory_dropped = self.working.cleanup()
        except Exception as e:
            log.exception("сон прошёл плохо")
            stats.errors.append(str(e))
        finally:
            stats.finished_at = time.time()
            self.sleeping = False
            self.last_stats = stats
            if self.bus:
                self.bus.emit("sleep.finished", **stats.to_dict())
        log.info("сон завершён: захвачено=%d кластеров=%d дистиллировано=%d "
                 "в архив=%d рёбер удалено=%d за %.1fс",
                 stats.captured, stats.clusters, stats.distilled,
                 stats.archived, stats.edges_pruned, stats.seconds)
        return stats

    # ------------------------------------------------------------ NREM

    async def _nrem(self, now: float) -> int:
        """Захват PRP: эпизоды с активными тегами -> долговременная память."""
        tags = self.store.live_tags(now)
        if not tags:
            return 0
        episode_ids = [t["episode_id"] for t in tags]
        captured = self.store.capture_tags(episode_ids, now)
        for episode_id in episode_ids:
            episode = self.store.get_episode(episode_id)
            if episode:
                self.store.touch(episode_id, now)
        log.info("NREM: захвачено %d эпизодов из лабильного буфера", captured)
        return captured

    def _replay(self, now: float) -> int:
        """Переигровка: захваченные вместе эпизоды связываются рёбрами (Хебб)."""
        consolidated = self.store.older_than(now, stage="consolidated")
        if len(consolidated) < 2:
            return 0
        recent = consolidated[-64:]
        ids = [e.episode_id for e in recent]
        _, matrix = self.store.all_embeddings(stage="consolidated")
        if matrix.size == 0 or matrix.shape[0] != len(ids):
            return 0
        pairs = 0
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                sim = float(np.dot(matrix[i], matrix[j]) /
                            (np.linalg.norm(matrix[i]) * np.linalg.norm(matrix[j]) + 1e-9))
                if sim >= 0.55:
                    self.store.link(ids[i], ids[j], kind="hebbian",
                                    weight=0.05 * sim, confidence=sim, now=now)
                    pairs += 1
        return pairs

    # -------------------------------------------------------------- REM

    async def _rem(self, stats: ConsolidationStats, threshold: float,
                   lookback_days: float, now: float) -> None:
        """Дистилляция хабов в семантические пропозиции + пометка забывания."""
        episodes = self.store.older_than(now, stage="consolidated")
        if not episodes:
            episodes = [e for e in self.store.older_than(now)
                        if now - e.ts <= lookback_days * 86400]
        if not episodes:
            return
        by_id = {e.episode_id: e for e in episodes}
        ids = [e.episode_id for e in episodes]
        _, matrix = self.store.all_embeddings()
        all_ids, _ = self.store.all_embeddings()
        index_of = {eid: i for i, eid in enumerate(all_ids)}
        rows = [index_of[eid] for eid in ids if eid in index_of]
        sub = matrix[rows] if rows else np.zeros((0, 1), dtype=np.float32)

        clusters = self._cluster(ids, sub, threshold, by_id=by_id)
        stats.clusters = len(clusters)
        for cluster_ids in clusters:
            cluster = [by_id[i] for i in cluster_ids if i in by_id]
            if not cluster:
                continue
            if len(cluster) == 1:
                self._maybe_archive_single(cluster[0], stats, now)
                continue
            await self._distill_cluster(cluster, stats, now)

    def _cluster(self, ids: Sequence[str], matrix: np.ndarray,
                 threshold: float,
                 by_id: dict[str, Any] | None = None) -> list[list[str]]:
        """Жадная кластеризация по близости: хаб + его соседи.

        27.10: добавлен by_id — без него кластеры строились ТОЛЬКО по
        cosine текста. собеседник «люблю кофе» и бот «люблю кофе» попадали
        в один кластер и сжимались в «кто-то любит кофе» БЕЗ автора.
        Утром она помнила факт, но не помнила чей.
        """
        if matrix.size == 0 or not ids:
            return [[i] for i in ids]
        by_id = by_id or {}
        used: set[str] = set()
        clusters: list[list[str]] = []
        for i, episode_id in enumerate(ids):
            if episode_id in used:
                continue
            cluster = [episode_id]
            used.add(episode_id)
            sims = cosine_scores(matrix[i], matrix)
            order = np.argsort(-sims)
            for idx in order:
                if len(cluster) >= MAX_CLUSTER:
                    break
                candidate = ids[int(idx)]
                if candidate in used or candidate == episode_id:
                    continue
                # разные люди не сливаются: иначе факты склеятся без автора
                a = by_id.get(episode_id)
                b = by_id.get(candidate)
                if (a is not None and b is not None
                        and a.speaker_id and b.speaker_id
                        and a.speaker_id != b.speaker_id):
                    continue
                if float(sims[int(idx)]) < threshold:
                    break
                cluster.append(candidate)
                used.add(candidate)
            clusters.append(cluster)
        return clusters

    async def _distill_cluster(self, cluster: list[Episode],
                               stats: ConsolidationStats, now: float) -> None:
        """Сжать кластер в одну пропозицию через LLM (или механически)."""
        text = ""
        kind = KIND_FACT
        if self.distill is not None:
            try:
                decision = await self.distill(cluster)
                text = str(decision.get("text") or "").strip()
                kind = str(decision.get("kind") or KIND_FACT)
                if str(decision.get("action", "merge")).lower() == "discard":
                    for episode in cluster:
                        self.store.set_stage(episode.episode_id, "archived")
                        stats.archived += 1
                    return
            except Exception as e:
                log.warning("дистилляция кластера не удалась: %s", str(e)[:120])
                stats.errors.append(f"distill[{len(cluster)}] {str(e)[:60]}")
        if not text:
            text = self._mechanical_merge(cluster)

        confidence = sum(e.confidence for e in cluster) / max(1, len(cluster))
        prop = Proposition(
            prop_id=f"prop_{int(now)}_{len(cluster)}",
            episode_id=cluster[0].episode_id,
            text=text, kind=kind,
            # 27.10: раньше здесь было «первое слово каждого текста» — это
            # не автор, а мусор. Теперь реальные имена участников.
            entities=sorted({e.speaker_name for e in cluster if e.speaker_name})[:8],
            confidence=max(-1.0, min(0.99, confidence)),
            ts=now,
        )
        if self.embedder is not None:
            try:
                prop.embedding = await self.embedder.embed_one(text)
            except Exception:
                prop.embedding = None
        self.store.add_proposition(prop)
        stats.distilled += len(cluster)

        # хаб-эпизод остаётся, остальные уходят в архив (не удаляются)
        for episode in cluster[1:]:
            self.store.set_stage(episode.episode_id, "archived")
        self.store.link(cluster[0].episode_id, prop.prop_id, kind="temporal",
                        weight=0.3, confidence=confidence, now=now)

    @staticmethod
    def _mechanical_merge(cluster: list[Episode]) -> str:
        """Без LLM: склеить самые информативные части, ничего не выдумывая."""
        ordered = sorted(cluster, key=lambda e: -e.importance)
        parts: list[str] = []
        for episode in ordered[:4]:
            snippet = " ".join(episode.text.split())[:220]
            if snippet and snippet not in parts:
                parts.append(snippet)
        return " / ".join(parts)

    def _maybe_archive_single(self, episode: Episode, stats: ConsolidationStats,
                              now: float) -> None:
        """Одиночный эпизод: архив, если след почти исчез."""
        access_times = [episode.last_access] if episode.last_access else []
        keep = decay_mod.retention(
            age_seconds=max(0.0, now - episode.ts),
            kind=KIND_OTHER,
            access_times=access_times,
            now=now,
        )
        if keep < FORGET_THRESHOLD and episode.importance < 0.5:
            self.store.set_stage(episode.episode_id, "archived")
            stats.archived += 1

    # ---------------------------------------------------------- FORGET

    def _forget(self, stats: ConsolidationStats, now: float) -> None:
        """Гомеостатическое забывание: слабые рёбра, мёртвые теги, старые эпизоды."""
        before = self.store.edge_count()
        self.store.decay_edges(now=now)
        stats.edges_decayed = before
        stats.edges_pruned = max(0, before - self.store.edge_count())
        stats.tags_expired = self.store.expire_tags(now)

        # эпизоды в лабильном буфере, не дождавшиеся захвата, гаснут
        stale = [e for e in self.store.older_than(now, stage="labile")
                 if now - e.ts > 86400]
        for episode in stale:
            self.store.set_stage(episode.episode_id, "archived")
            stats.archived += 1


async def estimate_importance(llm: Any, episode_text: str,
                              emotion_intensity: float = 0.0) -> dict[str, float]:
    """Быстрая оценка «стоит ли помнить» одной реплики.

    Возвращает importance / surprise / novelty-подобные компоненты.
    При недоступности LLM — эвристика по длине и эмоции, чтобы не терять
    записи совсем.
    """
    fallback = {
        "importance": min(1.0, 0.25 + 0.02 * len(episode_text.split())
                          + 0.3 * emotion_intensity),
        "surprise": 0.3,
        "emotion": min(1.0, emotion_intensity),
        "entities": [],
    }
    if llm is None:
        return fallback
    prompt = (
        "Оцени реплику для долговременной памяти персонажа. Ответь СТРОГО JSON: "
        '{"importance": 0..1, "surprise": 0..1, "emotion": 0..1, '
        '"entities": ["канонические имена людей/мест"]}. '
        "importance — пригодится ли это через неделю; surprise — насколько "
        "неожиданно; emotion — насколько эмоционально заряжено."
    )
    try:
        data = await llm.chat_json([
            {"role": "system", "content": prompt},
            {"role": "user", "content": episode_text[:1200]},
        ], function_label="memory_salience")
    except Exception:
        return fallback
    return {
        "importance": _clamp(data.get("importance"), 0.5),
        "surprise": _clamp(data.get("surprise"), 0.3),
        "emotion": _clamp(data.get("emotion"), emotion_intensity),
        "entities": [str(e)[:64] for e in (data.get("entities") or [])][:8],
    }


def _clamp(value: Any, default: float) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default
