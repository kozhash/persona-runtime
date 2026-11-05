"""Тесты памяти: хранилище, косинус, дневник, рабочая память, эмоции."""
from pathlib import Path

import numpy as np
import pytest

from kuni.character.emotions import Emotions, EmotionState
from kuni.memory.store import DiaryEntry, SqliteDiaryStore, cosine_top_k
from kuni.memory.working import WorkingMemory


def test_store_roundtrip(tmp_path: Path):
    store = SqliteDiaryStore(tmp_path / "d.db")
    entry = store.add(DiaryEntry(text="Встретила Влада в кафе", tags=["люди"], embedding=[1.0, 0.0]))
    assert store.count() == 1
    loaded = store.get(entry.entry_id)
    assert loaded is not None
    assert loaded.text == "Встретила Влада в кафе"
    assert loaded.embedding == [1.0, 0.0]
    store.update_text(entry.entry_id, "Встретила Влада")
    reloaded = store.get(entry.entry_id)
    assert reloaded.embedding is None   # вектор сброшен
    assert store.delete(entry.entry_id)
    assert store.count() == 0


def test_cosine_top_k():
    ids = ["a", "b", "c"]
    matrix = np.array([[1, 0], [0, 1], [1, 1]], dtype=np.float32)
    top = cosine_top_k(np.array([1, 0], dtype=np.float32), matrix, ids, k=2)
    assert top[0][0] == "a"
    assert top[0][1] == pytest.approx(1.0)
    # "c" тоже похож (0.707), но b — вообще нет
    assert [i for i, _ in top] == ["a", "c"]
    assert cosine_top_k(np.ones(2), np.zeros((0, 2)), [], k=3) == []


def test_search_text(tmp_path: Path):
    store = SqliteDiaryStore(tmp_path / "d.db")
    store.add(DiaryEntry(text="купила кофе"))
    store.add(DiaryEntry(text="прочитала книгу"))
    found = store.search_text("кофе")
    assert len(found) == 1 and "кофе" in found[0].text


class FakeEmbedder:
    """Детерминированные эмбеддинги без сети: bag-of-words по кодам символов."""

    async def embed_one(self, text: str) -> list[float]:
        vec = [0.0] * 32
        for ch in text.lower():
            vec[ord(ch) % 32] += 1.0
        return vec


class FakeEmotion:
    def __init__(self, emotion: str = "", intensity: float = 0.0) -> None:
        self.emotion = emotion
        self.intensity = intensity


@pytest.mark.asyncio
async def test_diary_graph_links_and_flash(tmp_path: Path):
    from kuni.memory.diary import Diary

    store = SqliteDiaryStore(tmp_path / "d.db")
    diary = Diary(store, FakeEmbedder())
    await diary.add("Катя рассказала про новый проект про кошек", tags=["Катя"])
    await diary.add("Катя прислала фото с кошками, было смешно", tags=["Катя"])
    await diary.add("Папик подарил мне наушники", tags=["Влад"])

    # рёбра графа появились: запись про фото-кошек связана с похожей про проект
    by_text = {e.text: e for e in store.recent(10)}
    links = store.linked(by_text["Катя прислала фото с кошками, было смешно"].entry_id, limit=5)
    _texts = [(store.get(eid).text if store.get(eid) else None) for eid, _ in links]
    assert any("кош" in (t or "") for t in _texts), f"links={links} texts={_texts}"

    # всплытие по словам собеседника
    flashed = await diary.flash("расскажи про кошек и Катю", FakeEmotion(), k=4, fresh_window=0)
    texts = " | ".join(f["text"] for f in flashed)
    assert "Катя" in texts and "кош" in texts
    assert all("via" in f for f in flashed)

    # настроение тянет воспоминания того же тона
    await diary.add("Меня бесит, когда опаздывают", tags=["обида"])
    moody = await diary.flash("привет", FakeEmotion(emotion="бесит", intensity=0.7),
                              k=4, fresh_window=0)
    assert any("бесит" in f["text"] for f in moody)

    # цепочка: от всплывшего — к связанному
    chained = await diary.flash("проект кошки Кати", FakeEmotion(), k=4, fresh_window=0)
    assert len(chained) >= 2


@pytest.mark.asyncio
async def test_diary_add_and_recall(tmp_path: Path):
    from kuni.memory.diary import Diary

    store = SqliteDiaryStore(tmp_path / "d.db")
    diary = Diary(store, FakeEmbedder())
    await diary.add("Катя рассказала про новый проект про кошек")
    await diary.add("Папик подарил мне наушники")
    found = await diary.recall("кошки проект Катя", k=1)
    assert found and "Катя" in found[0]["text"]
    assert found[0]["similarity"] > 0.3


def test_working_memory_lifecycle(tmp_path: Path):
    import time as _t
    wm = WorkingMemory(tmp_path / "wm.md")
    wm.add_item("скилить ссылку на плейлист")
    wm.add_item("напомнить Владу про собес")
    assert "плейлист" in wm.content()

    old_date = _t.strftime("%Y-%m-%d", _t.localtime(_t.time() - 10 * 86400))
    wm.replace(f"{old_date} - просроченное дело\nсвежая строка без даты")
    removed = wm.cleanup(max_age_days=3)
    assert removed == 1
    assert "просроченное" not in wm.content()
    assert "свежая строка" in wm.content()


def test_emotions_v3_pad_mood_habituation(tmp_path: Path):
    import sqlite3
    db = sqlite3.connect(":memory:")
    em = Emotions(db)

    # первая оценка: злорадство 70%, высокий контроль, примесь веселья
    em.apply_assessment(
        {"emotion": "Злорадство", "secondary_emotion": "веселье", "intensity": 70,
         "valence": 0.6, "arousal": 0.7, "dominance": 0.8,
         "reason": "собеседник снова лоханулся"}, chat_id=42)
    assert em.state.emotion.lower() == "злорадство"
    assert em.state.intensity_pct >= 70
    assert em.state.secondary == "веселье"
    assert em.state.dominance > 0.5
    assert em.state.emoji() in ("😏", "😌", "🤣")
    first = em.state.intensity

    # тот же триггер снова — копится
    em.apply_assessment(
        {"emotion": "злорадство", "intensity": 70, "valence": 0.6,
         "arousal": 0.7, "dominance": 0.8, "reason": "он же"}, chat_id=42)
    assert em.state.intensity >= first

    # mood подтянулся в позитив после двух радостных вспышек
    assert em.mood["valence"] > 0.1
    assert 0.3 <= em.mood["dominance"] <= 1.0

    # диклампинг мусорного ответа
    parsed = Emotions.parse_llm_json(
        {"emotion": "злость", "intensity": "-5", "valence": "-9",
         "arousal": "oops", "dominance": "12"}, 1)
    assert parsed.intensity == 0.0 and parsed.valence == -1.0
    assert parsed.arousal == 0.3 and parsed.dominance == 1.0

    # затухание со временем: 30 часов -> спокойствие
    import time as _t
    old = EmotionState(emotion="грусть", intensity=0.3, valence=-0.3,
                       arousal=0.2, ts=_t.time() - 30 * 3600)
    faded = old.decayed()
    assert faded.emotion == "спокойствие" or faded.intensity < 0.1

    # describe: два слоя + директива с процентами и dominance
    d = em.state.describe()
    assert "%" in d and "MUST" in d and "dominance" in d

    hist = em.history()
    assert len(hist) == 2 and "dominance" in hist[0] and "secondary" in hist[0]


def test_emotion_emoji_tiers():
    low = EmotionState(emotion="злость", intensity=0.2)
    mid = EmotionState(emotion="злость", intensity=0.5)
    high = EmotionState(emotion="злость", intensity=0.9)
    assert (low.emoji(), mid.emoji(), high.emoji()) == ("😒", "😠", "🤬")
    assert EmotionState(emotion="что-то-новое", intensity=0.8).emoji() == "‼️"
    assert EmotionState(emotion="злорадство", intensity=0.9).emoji() == "🤣"
