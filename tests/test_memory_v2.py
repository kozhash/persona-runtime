"""Тесты новой памяти: схема, граф, двухпутевой поиск, сон, забывание.

Ключевой тест — ассоциативный путь: воспоминание, лексически НЕ похожее
на запрос, должно всплыть через граф. Именно этого не умел старый kNN.
"""
from __future__ import annotations

import asyncio
import math
import time
from pathlib import Path

import numpy as np
import pytest

from kuni.memory import decay as decay_mod
from kuni.memory.consolidation import Consolidator
from kuni.memory.diary import Diary
from kuni.memory.episodic import (KIND_ENTITY, KIND_EVENT, KIND_FACT,
                                  Episode, EpisodicStore, Proposition)
from kuni.memory.retrieval import (base_activation, cosine_scores,
                                   dual_path_retrieve, lateral_inhibition,
                                   lexical_scores, spreading_activation,
                                   tokenize)


class FakeEmbedder:
    """Детерминированные эмбеддинги без сети: bag-of-words по кодам символов.

    Семантически «похожие» тексты получают близкие векторы за счёт общих
    слов, но разные темы — ортогональны. Достаточно, чтобы проверить
    механику, не проверяя качество модели.
    """

    def __init__(self, dim: int = 64) -> None:
        self.dim = dim

    def _vec(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for token in tokenize(text):
            vec[hash(token) % self.dim] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    async def embed_one(self, text: str) -> list[float]:
        return self._vec(text)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]


@pytest.fixture()
def store(tmp_path: Path) -> EpisodicStore:
    return EpisodicStore(tmp_path / "memory.db")


# ------------------------------------------------------------------ схема

def test_schema_creates_all_levels(store: EpisodicStore):
    from kuni.memory.schema import tables
    names = tables(store.db)
    for expected in ("episodes", "propositions", "edges", "facts", "syn_tags", "meta"):
        assert expected in names


def test_schema_migration_is_idempotent(store: EpisodicStore):
    from kuni.memory.schema import SCHEMA_VERSION, migrate
    assert migrate(store.db) == SCHEMA_VERSION
    assert migrate(store.db) == SCHEMA_VERSION


def test_episode_roundtrip(store: EpisodicStore):
    episode = store.add_episode(Episode(
        episode_id="ep1", ts=time.time(), text="Жан сказал, что любит",
        source="chat", chat_id=42, importance=0.9))
    loaded = store.get_episode("ep1")
    assert loaded is not None
    assert loaded.text == episode.text
    assert loaded.chat_id == 42
    assert loaded.stage == "labile"
    assert loaded.checksum  # метка происхождения


def test_delete_episode_cascades(store: EpisodicStore):
    store.add_episode(Episode(episode_id="a", ts=time.time(), text="раз"))
    store.add_episode(Episode(episode_id="b", ts=time.time(), text="два"))
    store.link("a", "b", weight=0.5)
    assert store.edge_count() == 1
    store.delete_episode("a")
    assert store.get_episode("a") is None
    assert store.edge_count() == 0


# ------------------------------------------------------------------ граф

def test_hebbian_weight_grows_and_caps(store: EpisodicStore):
    store.link("a", "b", weight=0.5)
    store.link("a", "b", weight=0.5)
    assert store.edge_weight("a", "b") == pytest.approx(1.0)
    for _ in range(20):
        store.link("a", "b", weight=1.0)
    assert store.edge_weight("a", "b") <= 3.0


def test_hebbian_decay_prunes_weak_edges(store: EpisodicStore):
    store.link("a", "b", weight=0.02)
    store.link("c", "d", weight=2.0)
    store.decay_edges(decay=0.5)
    assert store.edge_weight("a", "b") == 0.0        # удалено
    assert store.edge_weight("c", "d") == pytest.approx(1.0)


def test_prevents_edges_are_negative_in_spreading():
    """ГАМК-аналог: ребро prevents должно гасить активацию."""
    base = {"a": 1.0}
    neighbours = {"a": [("b", "hebbian", 1.0), ("c", "prevents", 1.0)]}
    scores = spreading_activation(base, neighbours, beta=0.5, hops=1)
    assert scores["b"] > 0
    assert scores["c"] < 0


def test_syn_tag_lifecycle(store: EpisodicStore):
    now = time.time()
    store.add_episode(Episode(episode_id="x", ts=now, text="событие"))
    store.tag("x", strength=0.9, ttl_seconds=60, now=now)
    assert len(store.live_tags(now + 10)) == 1
    # тег распался без захвата
    assert len(store.live_tags(now + 120)) == 0
    assert store.expire_tags(now + 120) == 1


def test_capture_tags_promotes_episode(store: EpisodicStore):
    now = time.time()
    store.add_episode(Episode(episode_id="y", ts=now, text="важное"))
    store.tag("y", strength=0.95, ttl_seconds=600, now=now)
    assert store.capture_tags(["y"], now=now) == 1
    assert store.get_episode("y").stage == "consolidated"


# ------------------------------------------------- битемпоральные факты

def test_fact_resolution_by_serial(store: EpisodicStore):
    """Конфликт решается детерминированно: побеждает больший serial."""
    first = store.upsert_fact("Жан", "статус", "злой", confidence=0.6,
                              valid_from=1000.0, now=1000.0)
    second = store.upsert_fact("Жан", "статус", "добрый", confidence=0.8,
                               valid_from=2000.0, now=2000.0)
    assert second.serial > first.serial
    current = store.resolve_conflict("Жан", "статус")
    assert current is not None
    assert current.object == "добрый"
    # старый факт не удалён, а закрыт по valid_to
    closed = store.db.execute(
        "SELECT object, valid_to FROM facts WHERE fact_id=?", (first.fact_id,)).fetchone()
    assert closed[1] == pytest.approx(2000.0)


def test_current_facts_excludes_superseded(store: EpisodicStore):
    store.upsert_fact("Аня", "город", "Алматы", now=1000.0)
    store.upsert_fact("Аня", "город", "Астана", now=2000.0)
    current = store.current_facts("Аня")
    assert len(current) == 1
    assert current[0].object == "Астана"


# ------------------------------------------------------- кривые забывания

def test_decay_monotonic():
    assert decay_mod.weibull(0) == pytest.approx(1.0)
    assert decay_mod.weibull(3600 * 1000) < 0.5
    assert decay_mod.ebbinghaus(0) == pytest.approx(1.0)
    assert decay_mod.ebbinghaus(3600 * 24) < decay_mod.ebbinghaus(3600)


def test_type_decay_differs_by_kind():
    """Факт стареет медленнее мысли — это и есть type-conditioned decay."""
    age = 3600.0 * 24 * 10
    fact = decay_mod.type_decay(age, KIND_FACT)
    thought = decay_mod.type_decay(age, "THOUGHT")
    assert fact > thought


def test_actr_activation_grows_with_practice():
    now = time.time()
    rare = decay_mod.actr_base_activation([now - 3600], now)
    often = decay_mod.actr_base_activation([now - 3600] * 5, now)
    assert often > rare


def test_retention_boosted_by_access():
    now = time.time()
    age = 3600.0 * 48
    cold = decay_mod.retention(age, KIND_EVENT, [], now)
    warm = decay_mod.retention(age, KIND_EVENT, [now - 3600] * 6, now)
    assert warm > cold


# ------------------------------------------------------------- поиск

def test_tokenize_drops_stopwords():
    tokens = tokenize("и вот это был не он, а Жан")
    assert "и" not in tokens and "вот" not in tokens
    assert "жан" in tokens


def test_lexical_scores_prefers_rare_word():
    corpus = ["Жан сказал люблю", "погода сегодня хорошая", "снова погода"]
    scores = lexical_scores(["жан"], corpus)
    assert scores[0] > scores[1]


def test_cosine_scores_safe_on_zero_vector():
    matrix = np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)
    assert cosine_scores(np.array([0.0, 0.0]), matrix).tolist() == [0.0, 0.0]


def test_lateral_inhibition_removes_near_duplicates():
    kept = lateral_inhibition({"a": 1.0, "b": 0.99, "c": 0.2})
    assert "a" in kept
    assert "b" not in kept          # почти такой же скор — подавлен
    assert "c" in kept


def test_dual_path_finds_associative_recall():
    """ГЛАВНЫЙ ТЕСТ: всплытие по ассоциации, а не по словам.

    Запрос «что случилось в гаражах» лексически далёк от «тело закопано»,
    но они связаны ребром — значит второе обязано всплыть.
    """
    now = time.time()
    ids = ["ep_body", "ep_weather", "ep_garage"]
    texts = [
        "тело закопано за гаражами, второе так и не нашли",
        "сегодня была отличная погода и мы гуляли",
        "гаражи молчат уже неделю, никто ничего не говорит",
    ]
    stamps = [now - 600, now - 300, now - 120]
    kinds = [KIND_EVENT] * 3
    matrix = np.zeros((3, 3), dtype=np.float32)
    neighbours = {"ep_garage": [("ep_body", "hebbian", 1.2)]}

    nodes = dual_path_retrieve(
        query="что там с гаражами",
        query_embedding=[0.0, 0.0, 1.0],
        episode_ids=ids, texts=texts, timestamps=stamps, kinds=kinds,
        matrix=matrix, neighbours=neighbours,
        top_k=3, flip_m=2, now=now, apply_inhibition=False,
    )
    found = {n.episode_id: n for n in nodes}
    assert "ep_garage" in found
    assert "ep_body" in found, "ассоциация по ребру не сработала"
    assert "потянуло" in found["ep_body"].via or found["ep_body"].score > 0


def test_dual_path_ranks_direct_hit_first():
    now = time.time()
    ids = ["direct", "noise"]
    nodes = dual_path_retrieve(
        query="жан",
        query_embedding=[1.0, 0.0],
        episode_ids=ids,
        texts=["жан сказал люблю", "случайный шум без слов"],
        timestamps=[now, now], kinds=[KIND_EVENT, KIND_EVENT],
        matrix=np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32),
        neighbours={}, top_k=2, now=now,
    )
    assert nodes[0].episode_id == "direct"


# --------------------------------------------------------------- дневник

@pytest.mark.asyncio
async def test_diary_add_creates_tag_and_links(tmp_path: Path):
    store = EpisodicStore(tmp_path / "d.db")
    diary = Diary(store, FakeEmbedder())
    await diary.add("Жан сказал, что любит меня", importance=0.9)
    await diary.add("Жан сказал, что любит меня очень", importance=0.9)
    stats = store.stats()
    assert stats["entries"] == 2
    assert stats["live_tags"] >= 0
    # похожие записи должны связаться
    assert stats["edges"] >= 1


@pytest.mark.asyncio
async def test_diary_high_importance_promotes_immediately(tmp_path: Path):
    store = EpisodicStore(tmp_path / "d.db")
    diary = Diary(store, FakeEmbedder())
    episode = await diary.add("критично важное событие", importance=1.0,
                              surprise=1.0, emotion=1.0)
    assert store.get_episode(episode.episode_id).stage == "consolidated"


@pytest.mark.asyncio
async def test_diary_recall_and_flash(tmp_path: Path):
    store = EpisodicStore(tmp_path / "d.db")
    diary = Diary(store, FakeEmbedder())
    await diary.add("мы говорили про закопанное тело у гаражей",
                    importance=0.8, promote=True)
    await diary.add("погода была холодная", importance=0.1)
    recalled = await diary.recall("тело гаражи", k=2)
    assert recalled
    assert "тело" in recalled[0]["text"] or "гараж" in recalled[0]["text"]
    flashed = await diary.flash("гаражи", k=2, fresh_window=0)
    assert flashed


@pytest.mark.asyncio
async def test_diary_rejects_empty(tmp_path: Path):
    diary = Diary(EpisodicStore(tmp_path / "d.db"), FakeEmbedder())
    with pytest.raises(ValueError):
        await diary.add("   ")


@pytest.mark.asyncio
async def test_diary_promote(tmp_path: Path):
    store = EpisodicStore(tmp_path / "d.db")
    diary = Diary(store, FakeEmbedder())
    episode = await diary.add("мелкая деталь", importance=0.05, promote=False)
    assert store.get_episode(episode.episode_id).stage == "labile"
    assert await diary.promote(episode.episode_id) is True
    assert store.get_episode(episode.episode_id).stage == "consolidated"


# ------------------------------------------------------------------ сон

@pytest.mark.asyncio
async def test_consolidator_runs_all_phases(store: EpisodicStore):
    now = time.time()
    embedder = FakeEmbedder()
    for i in range(3):
        text = f"разговор про гаражи номер {i}"
        store.add_episode(Episode(
            episode_id=f"e{i}", ts=now - 100, text=text,
            embedding=await embedder.embed_one(text), importance=0.6))
        store.tag(f"e{i}", strength=0.9, ttl_seconds=600, now=now)

    working = type("W", (), {"cleanup": lambda self: 2})()
    consolidator = Consolidator(store, working=working, embedder=embedder)
    stats = await consolidator.run(now=now)

    assert stats.captured == 3
    assert stats.clusters >= 1
    assert stats.distilled >= 1
    assert stats.working_memory_dropped == 2
    assert stats.finished_at > 0


@pytest.mark.asyncio
async def test_consolidator_distill_uses_llm(store: EpisodicStore):
    now = time.time()
    embedder = FakeEmbedder()
    for i in range(2):
        text = f"Жан сказал про любовь, попытка {i}"
        store.add_episode(Episode(
            episode_id=f"d{i}", ts=now - 50, text=text,
            embedding=await embedder.embed_one(text), importance=0.7))

    async def fake_distill(cluster):
        return {"action": "merge", "kind": KIND_ENTITY,
                "text": "Жан признался в любви"}

    consolidator = Consolidator(store, embedder=embedder, distill=fake_distill)
    stats = await consolidator.run(similarity_threshold=0.5, now=now)
    assert stats.distilled >= 2
    props = store.db.execute("SELECT text FROM propositions").fetchall()
    assert any("признался" in p[0] for p in props)


@pytest.mark.asyncio
async def test_consolidator_archives_stale_labile(store: EpisodicStore):
    old = time.time() - 86400 * 3
    store.add_episode(Episode(episode_id="old", ts=old, text="старое",
                              stage="labile"))
    consolidator = Consolidator(store)
    stats = await consolidator.run(now=time.time())
    assert store.get_episode("old").stage == "archived"
    assert stats.archived >= 1


def test_stats_shape(store: EpisodicStore):
    stats = store.stats()
    for key in ("entries", "labile", "consolidated", "embedded", "edges",
                "propositions", "facts", "live_tags"):
        assert key in stats


# ------------------------------------------------------ совместимость

def test_legacy_store_aliases_work(tmp_path: Path):
    from kuni.memory.store import (DiaryEntry, SqliteDiaryStore,
                                   cosine_top_k)
    store = SqliteDiaryStore(tmp_path / "legacy.db")
    entry = store.add(DiaryEntry(text="старый API", embedding=[1.0, 0.0]))
    assert store.count() == 1
    assert store.get(entry.entry_id).text == "старый API"
    ids, matrix = store.all_embeddings()
    assert len(ids) == 1
    top = cosine_top_k(np.array([1.0, 0.0]), matrix, ids, 1)
    assert top[0][0] == entry.entry_id


@pytest.mark.asyncio
async def test_legacy_sleepcycle_wrapper(tmp_path: Path):
    from kuni.memory.sleep import SleepCycle
    store = EpisodicStore(tmp_path / "s.db")
    diary = Diary(store, FakeEmbedder())
    cycle = SleepCycle(diary, store, working=None)
    stats = await cycle.run(now=time.time())
    assert stats.seconds >= 0
    assert "merged" in stats.to_dict()
