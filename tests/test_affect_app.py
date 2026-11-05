"""Интеграция аффекта в приложение: приложение собирается и живёт.

Проверяет то, что не видно в юнит-тестах модулей:
  * KuniApp создаёт AffectService и PersonaBuilder с ним;
  * телеметрия попадает в системный промпт;
  * реплика меняет состояние и это видно в конфиге приложения;
  * предохранитель блокирует инструмент в критической зоне;
  * схема памяти поднята до версии 4.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def make_app(tmp_path: Path):
    from kuni.app import KuniApp

    config = tmp_path / "config.toml"
    config.write_text(
        '[general]\n'
        'character_name = "Тест"\n'
        'papik_chat_id = 555\n'
        'data_dir = "data"\n'
        'prompts_dir = "prompts"\n'
        '[telegram]\n'
        'enabled = false\n'
        '[llm]\n'
        'base_url = "http://127.0.0.1:9/v1"\n'
        'model = "stub"\n'
        '[proxy]\n'
        'enabled = false\n'
        '[dashboard]\n'
        'enabled = false\n'
        '[metrics]\n'
        'enabled = false\n'
        '[images]\n'
        'enabled = false\n'
        '[assistant]\n'
        'mode = "off"\n'
        '[memory]\n'
        'enabled = true\n'
        '[affect]\n'
        'enabled = true\n'
        'noise_enabled = false\n',
        encoding="utf-8",
    )
    app = KuniApp(config)
    app.prompts.directory = ROOT / "prompts"
    app.prompts.reload_all()
    return app


def test_app_creates_affect_service(tmp_path: Path):
    app = make_app(tmp_path)
    assert app.affect is not None
    assert app.persona.affect is app.affect
    assert app.affect.owner_chat_id == 555


def test_persona_contains_telemetry(tmp_path: Path):
    app = make_app(tmp_path)
    system = app.persona.build(chat_title="тест")
    assert "interoceptive_telemetry" in system
    assert "autonomic_arousal" in system


def test_message_changes_affect_state(tmp_path: Path):
    app = make_app(tmp_path)
    before = app.affect.snapshot.mono.s
    app.affect.receive_message(text="ты дура и тупая, заткнись", chat_id=555)
    after = app.affect.snapshot.mono.s
    assert after < before


def test_breaker_blocks_tool_in_rage(tmp_path: Path):
    app = make_app(tmp_path)
    app.affect.engine.set_state(s=0.10, n=0.90, d=0.90)
    decision = app.affect.check_action("self_edit")
    assert decision.allowed is False
    assert decision.zone == "rage"
    # безопасный инструмент проходит
    assert app.affect.check_action("remember").allowed is True


def test_memory_schema_v4(tmp_path: Path):
    app = make_app(tmp_path)
    from kuni.memory.schema import SCHEMA_VERSION
    assert SCHEMA_VERSION >= 4
    tables = {r[0] for r in app.diary_store.db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    for expected in ("neuro_state", "affect_impulses", "memory_access",
                     "relation_weights", "episodes", "edges", "facts"):
        assert expected in tables


def test_episode_gets_affect_signature(tmp_path: Path):
    """Запись в память должна нести эмоцию момента кодирования."""
    app = make_app(tmp_path)
    app.affect.receive_message(text="ты дура", chat_id=555)
    from kuni.memory.episodic import Episode
    episode = Episode(text="он меня обозвал", importance=0.7)
    app.affect.bind_to_episode(episode)
    assert episode.pad_p != 0.0
    assert episode.encoding_monoamines


def test_sampling_params_from_affect(tmp_path: Path):
    app = make_app(tmp_path)
    calm = app.affect.sampling_params()["temperature"]
    app.affect.engine.set_state(s=0.10, n=0.95, d=0.90)
    hot = app.affect.sampling_params()["temperature"]
    assert hot > calm


def test_night_queue_exists(tmp_path: Path):
    app = make_app(tmp_path)
    assert app.night_queue is not None
    assert app.night_queue.pending_count() == 0
