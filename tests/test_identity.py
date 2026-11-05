"""Тесты идентичности собеседника: атрибуция памяти и две корзины.

27.10: появилось после разбора «она путает чаты». Проверяем, что
воспоминание знает, КТО сказал и ГДЕ, и что чужое не приписывается
текущему собеседнику.
"""
from kuni.memory.episodic import Episode, EpisodicStore
from kuni.memory.schema import migrate


def _store(tmp_path) -> EpisodicStore:
    """EpisodicStore принимает ПУТЬ, а не соединение — connect() внутри."""
    store = EpisodicStore(tmp_path / "memory.db")
    migrate(store.db)
    return store


def test_episode_keeps_attribution(tmp_path):
    """Атрибуция сохраняется и возвращается — иначе память не знает автора."""
    store = _store(tmp_path)
    ep = Episode(
        episode_id="ep1", ts=1000.0, text="люблю кофе",
        chat_id=111, speaker_id=555, speaker_name="собеседник",
        chat_title="личка",
    )
    store.add_episode(ep)
    back = store.get_episode("ep1")
    assert back is not None
    assert back.speaker_id == 555
    assert back.speaker_name == "собеседник"
    assert back.chat_title == "личка"


def test_different_speakers_survive_roundtrip(tmp_path):
    """Два разных человека с одинаковым текстом не сливаются в базе."""
    store = _store(tmp_path)
    store.add_episode(Episode(episode_id="a", ts=1.0, text="люблю кофе",
                              chat_id=1, speaker_id=10, speaker_name="собеседник"))
    store.add_episode(Episode(episode_id="b", ts=2.0, text="люблю кофе",
                              chat_id=2, speaker_id=20, speaker_name="Бот"))
    a = store.get_episode("a")
    b = store.get_episode("b")
    assert a.speaker_id == 10 and a.speaker_name == "собеседник"
    assert b.speaker_id == 20 and b.speaker_name == "Бот"


def test_slot_quota_puts_interlocutor_first():
    """Свои воспоминания не вытесняются чужими при малом числе слотов."""
    from kuni.app import KuniApp

    assocs = [
        {"text": "чужое 1", "speaker_id": 999, "speaker_name": "Бот"},
        {"text": "чужое 2", "speaker_id": 999, "speaker_name": "Бот"},
        {"text": "чужое 3", "speaker_id": 999, "speaker_name": "Бот"},
        {"text": "своё 1", "speaker_id": 111, "speaker_name": "собеседник"},
        {"text": "своё 2", "speaker_id": 111, "speaker_name": "собеседник"},
    ]
    out = KuniApp._apply_slot_quota(assocs, interlocutor_id=111, k=3)
    same = [a for a in out if a["speaker_id"] == 111]
    assert len(same) >= 2, "свои должны быть в начале выдачи"
    assert out[0]["speaker_id"] == 111


def test_slot_quota_keeps_all_when_enough_same():
    """Если своих уже достаточно — порядок не трогаем."""
    from kuni.app import KuniApp

    assocs = [
        {"text": "своё", "speaker_id": 111},
        {"text": "своё2", "speaker_id": 111},
        {"text": "чужое", "speaker_id": 999},
    ]
    out = KuniApp._apply_slot_quota(assocs, interlocutor_id=111, k=3)
    assert out == assocs


def test_slot_quota_noop_without_interlocutor():
    """Без известного собеседника квота не применяется."""
    from kuni.app import KuniApp

    assocs = [{"text": "x", "speaker_id": 1}]
    assert KuniApp._apply_slot_quota(assocs, interlocutor_id=0, k=3) == assocs


def test_cluster_does_not_merge_different_speakers():
    """Ночная кластеризация не сливает факты разных людей."""
    import numpy as np
    from kuni.memory.consolidation import Consolidator

    cons = Consolidator.__new__(Consolidator)
    eps = {
        "e1": Episode(episode_id="e1", ts=1.0, text="люблю кофе",
                      speaker_id=10, speaker_name="собеседник"),
        "e2": Episode(episode_id="e2", ts=2.0, text="люблю кофе",
                      speaker_id=20, speaker_name="Бот"),
    }
    ids = ["e1", "e2"]
    matrix = np.array([[1.0, 0.99], [0.99, 1.0]], dtype=np.float32)
    clusters = cons._cluster(ids, matrix, 0.5, by_id=eps)
    assert all(len(c) == 1 for c in clusters), (
        "разные люди не должны попасть в один кластер")


def test_cluster_merges_same_speaker():
    """Один и тот же человек — сливать можно."""
    import numpy as np
    from kuni.memory.consolidation import Consolidator

    cons = Consolidator.__new__(Consolidator)
    eps = {
        "e1": Episode(episode_id="e1", ts=1.0, text="люблю кофе",
                      speaker_id=10, speaker_name="собеседник"),
        "e2": Episode(episode_id="e2", ts=2.0, text="кофе вкусный",
                      speaker_id=10, speaker_name="собеседник"),
    }
    matrix = np.array([[1.0, 0.99], [0.99, 1.0]], dtype=np.float32)
    clusters = cons._cluster(["e1", "e2"], matrix, 0.5, by_id=eps)
    assert len(clusters) == 1 and len(clusters[0]) == 2


def test_message_text_extracts_text():
    """Помощник вытаскивает текст из TDLib-сообщения."""
    from kuni.app import KuniApp

    m = {"content": {"@type": "messageText", "text": {"text": "привет"}}}
    assert KuniApp._message_text(m) == "привет"

    photo = {"content": {"@type": "messagePhoto",
                         "caption": {"text": "смотри"}}}
    assert "смотри" in KuniApp._message_text(photo)

    sticker = {"content": {"@type": "messageSticker"}}
    assert KuniApp._message_text(sticker) == "[стикер]"

    empty = {"content": {"@type": "messageUnsupported"}}
    assert KuniApp._message_text(empty) == ""
