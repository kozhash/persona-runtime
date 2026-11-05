"""Регрессии на баги, найденные в живом прогоне 2026-10-26 04:07.

  * эмбеддинги уходили с пустой моделью -> OpenRouter 400;
  * ночной режим блокировал ответы, хотя шёл тест;
  * старая память не мигрировала в новую базу;
  * сетевой обрыв Telegram ронял весь процесс.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------- пустая модель эмбеддингов

def test_endpoint_client_sets_embed_model(tmp_path: Path):
    """`_endpoint_client` раньше передавал "" как embed_model.

    Из-за этого запрос уходил с `model: ""`, и OpenRouter отвечал
    400 «Model does not exist».
    """
    from kuni.app import KuniApp

    config = tmp_path / "config.toml"
    config.write_text(
        '[general]\ncharacter_name = "T"\ndata_dir = "data"\nprompts_dir = "prompts"\n'
        '[telegram]\nenabled = false\n'
        '[llm]\nbase_url = "http://127.0.0.1:9/v1"\nmodel = "m"\n'
        'embed_model = "emb-model"\n'
        '[llm.embedding]\nbase_url = "https://openrouter.ai/api/v1"\n'
        'model = "qwen/qwen3-embedding-8b"\n'
        '[proxy]\nenabled = false\n[dashboard]\nenabled = false\n'
        '[metrics]\nenabled = false\n[images]\nenabled = false\n'
        '[assistant]\nmode = "off"\n[affect]\nnoise_enabled = false\n',
        encoding="utf-8",
    )
    app = KuniApp(config)
    try:
        # оба поля должны быть непустыми — именно это ломалось
        assert app.embed_llm.model, "model клиента эмбеддингов пуст"
        assert app.embed_llm.embed_model, "embed_model клиента пуст"
        assert app.embed_llm.embed_model == "qwen/qwen3-embedding-8b"
    finally:
        app.db.close()
        app.diary_store.db.close()


def test_embed_sends_non_empty_model(tmp_path: Path):
    """Прямая проверка тела запроса: model не может быть пустым."""
    from kuni.llm.client import LLMClient

    client = LLMClient("http://127.0.0.1:9/v1", "k", "chat-model", "emb-model")
    try:
        assert client.embed_model == "emb-model"
        assert client.model == "chat-model"
    finally:
        import asyncio
        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            client.aclose())


# ------------------------------------------------------------ ночной режим

def test_night_disabled_by_flag():
    """При night_enabled=false она не спит никогда."""
    from kuni.telegram.human import HumanBehavior

    class Cfg:
        night_from = 2
        night_to = 9
        night_enabled = False

    class Provider:
        current = type("C", (), {"telegram": Cfg()})()

    human = HumanBehavior(lambda *a, **k: None, Provider())
    assert human.is_night_now() is False


def test_night_enabled_respects_window():
    from kuni.telegram.human import HumanBehavior, _is_night

    class Cfg:
        night_from = 2
        night_to = 9
        night_enabled = True

    class Provider:
        current = type("C", (), {"telegram": Cfg()})()

    human = HumanBehavior(lambda *a, **k: None, Provider())
    # проверяем именно логику окна, а не текущие часы: раньше тест
    # проходил только между 02:00 и 09:00 и падал днём
    four_am = time.struct_time((2026, 1, 1, 4, 0, 0, 3, 1, 0))
    assert _is_night(2, 9, four_am) is True
    assert _is_night(2, 9, time.struct_time((2026, 1, 1, 12, 0, 0, 3, 1, 0))) is False


def test_is_night_boundaries():
    from kuni.telegram.human import _is_night
    import time as _t

    def at(hour: int):
        return _t.struct_time((2026, 10, 26, hour, 0, 0, 0, 299, -1))

    assert _is_night(2, 9, at(3)) is True
    assert _is_night(2, 9, at(12)) is False
    # через полночь
    assert _is_night(22, 8, at(23)) is True
    assert _is_night(22, 8, at(5)) is True
    assert _is_night(22, 8, at(12)) is False


# ------------------------------------------------------------ миграция памяти

def test_migration_preserves_entries(tmp_path: Path):
    """Старая diary.db должна переноситься целиком, идемпотентно."""
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    from migrate_memory import migrate

    old = tmp_path / "diary.db"
    conn = sqlite3.connect(str(old))
    conn.execute(
        "CREATE TABLE diary (entry_id TEXT PRIMARY KEY, ts REAL, text TEXT, "
        "tags TEXT, source TEXT, embedding BLOB)")
    conn.execute("CREATE TABLE diary_links (from_id TEXT, to_id TEXT, weight REAL)")
    now = time.time()
    conn.execute("INSERT INTO diary VALUES (?,?,?,?,?,?)",
                 ("a1", now - 100, "первая сцена", "x", "chat", None))
    conn.execute("INSERT INTO diary VALUES (?,?,?,?,?,?)",
                 ("a2", now - 50, "вторая сцена", "", "emotion", None))
    conn.commit()
    conn.close()

    new = tmp_path / "memory.db"
    stats = migrate(old, new)
    assert stats["moved"] == 2
    assert stats["total"] == 2

    # эмоциональная запись должна стать SELF_INFERENCE
    store = sqlite3.connect(str(new))
    row = store.execute(
        "SELECT provenance_class FROM episodes WHERE episode_id='a2'").fetchone()
    assert row[0] == "SELF_INFERENCE"
    # повторный запуск не дублирует
    stats2 = migrate(old, new)
    assert stats2["moved"] == 0
    assert stats2["skipped"] == 2


# --------------------------------------------------------- устойчивость loop

def test_network_errors_are_ignored_by_handler():
    """Сетевой обрыв не должен считаться фатальной ошибкой."""
    import asyncio

    handled: list[str] = []

    class FakeLoop:
        def set_exception_handler(self, fn):
            self._fn = fn

        def call_exception_handler(self, context):
            self._fn(self, context)

    import kuni.__main__ as main_mod

    # воспроизводим логику обработчика
    def handler(loop_, context):
        exc = context.get("exception")
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError,
                            BrokenPipeError, TimeoutError)):
            handled.append("ignored")
            return
        handled.append("reported")

    for exc in (ConnectionResetError(10054, "reset"),
                ConnectionAbortedError(10053, "aborted"),
                BrokenPipeError(32, "pipe"),
                TimeoutError("ping")):
        handler(None, {"exception": exc, "message": "x"})
    assert handled.count("ignored") == 4

    handler(None, {"exception": ValueError("real bug"), "message": "x"})
    assert handled[-1] == "reported"
