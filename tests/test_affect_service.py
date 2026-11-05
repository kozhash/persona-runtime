"""Тесты сервиса аффекта: связь состояния, памяти, безопасности.

Проверяет то, что не видно в юнит-тестах движка:
  * реплика меняет состояние и это сохраняется;
  * аффективная подпись закрепляется за эпизодом;
  * привязанность усиливает реакцию владельца;
  * предохранитель блокирует деструктив в критической зоне;
  * ecphory-поиск учитывает текущее настроение;
  * сон сбрасывает усталость.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from kuni.affect import AffectConfig, AffectService
from kuni.affect.types import AffectSnapshot, Monoamines
from kuni.memory.episodic import Episode, EpisodicStore

OWNER = 555


@pytest.fixture()
def svc(tmp_path: Path) -> AffectService:
    cfg = AffectConfig()
    cfg.engine.noise_enabled = False
    return AffectService(tmp_path / "affect.db", config=cfg,
                         owner_chat_id=OWNER)


@pytest.fixture()
def store(tmp_path: Path) -> EpisodicStore:
    return EpisodicStore(tmp_path / "memory.db")


# ------------------------------------------------------------ база

def test_initial_state_is_baseline(svc: AffectService):
    mono = svc.snapshot.mono
    assert mono.s == pytest.approx(0.62)
    assert mono.n == pytest.approx(0.38)


def test_state_persists_and_reloads(tmp_path: Path):
    cfg = AffectConfig()
    cfg.engine.noise_enabled = False
    first = AffectService(tmp_path / "a.db", config=cfg, owner_chat_id=OWNER)
    first.receive_message(text="ты дура и тупая", chat_id=OWNER)
    first.persist_state()
    before = first.snapshot.mono

    second = AffectService(tmp_path / "a.db", config=cfg, owner_chat_id=OWNER)
    after = second.snapshot.mono
    assert after.s == pytest.approx(before.s, abs=0.01)
    assert after.n == pytest.approx(before.n, abs=0.01)


# ------------------------------------------------------------ импульсы

def test_insult_lowers_serotonin(svc: AffectService):
    before = svc.snapshot.mono.s
    svc.receive_message(text="заткнись, ты тупая дура", chat_id=OWNER)
    assert svc.snapshot.mono.s < before


def test_insult_raises_noradrenaline(svc: AffectService):
    before = svc.snapshot.mono.n
    svc.receive_message(text="ненавижу тебя, отстань", chat_id=OWNER)
    assert svc.snapshot.mono.n > before


def test_praise_raises_dopamine(svc: AffectService):
    svc.receive_message(text="заткнись", chat_id=OWNER)
    low_d = svc.snapshot.mono.d
    svc.receive_message(text="ты лучшая, спасибо, люблю", chat_id=OWNER)
    assert svc.snapshot.mono.d > low_d


def test_neutral_text_does_not_swing_state(svc: AffectService):
    before = svc.snapshot.mono
    svc.receive_message(text="ок", chat_id=OWNER)
    after = svc.snapshot.mono
    assert abs(after.s - before.s) < 0.1


def test_caps_lock_counts_as_threat(svc: AffectService):
    _, threat_plain = svc._lexical_valence("отстань от меня")
    _, threat_caps = svc._lexical_valence("ОТСТАНЬ ОТ МЕНЯ")
    assert threat_caps > threat_plain


def test_impulse_recorded_in_db(svc: AffectService):
    svc.receive_message(text="спасибо, умница", chat_id=OWNER)
    rows = svc.db.execute("SELECT COUNT(*) FROM affect_impulses").fetchone()
    assert rows[0] >= 1


# ------------------------------------------------------------ привязанность

def test_owner_weight_higher_than_stranger(svc: AffectService):
    assert svc.relation_weight(OWNER) > svc.relation_weight(999)


def test_owner_insult_hits_harder(tmp_path: Path):
    """Оскорбление от близкого меняет состояние сильнее, чем от чужого."""
    cfg = AffectConfig()
    cfg.engine.noise_enabled = False

    owner_svc = AffectService(tmp_path / "o.db", config=cfg, owner_chat_id=OWNER)
    stranger_svc = AffectService(tmp_path / "s.db", config=cfg, owner_chat_id=0)

    owner_svc.receive_message(text="ты дура", chat_id=OWNER)
    stranger_svc.receive_message(text="ты дура", chat_id=999)

    owner_drop = 0.62 - owner_svc.snapshot.mono.s
    stranger_drop = 0.62 - stranger_svc.snapshot.mono.s
    assert owner_drop > stranger_drop


def test_positive_message_raises_relation(svc: AffectService):
    before = svc.relation_weight(777)
    svc.update_relation(777, delta=0.2, valence=0.5)
    assert svc.relation_weight(777) > before


def test_relation_bounded(svc: AffectService):
    for _ in range(100):
        svc.update_relation(888, delta=0.5, valence=0.9)
    assert svc.relation_weight(888) <= svc.cfg.w_rel_max


# ------------------------------------------------------------ подпись памяти

def test_bind_writes_affect_to_episode(svc: AffectService):
    svc.receive_message(text="ты дура", chat_id=OWNER)
    episode = Episode(text="он меня обозвал", importance=0.7)
    svc.bind_to_episode(episode)
    # настроение отрицательное -> подпись отрицательная
    assert episode.pad_p < 0
    assert episode.encoding_monoamines
    assert "s" in episode.encoding_monoamines


def test_bind_sets_valuation(svc: AffectService):
    episode = Episode(text="событие", importance=0.8)
    svc.bind_to_episode(episode)
    assert 0.0 <= episode.actr_valuation <= 1.0


def test_arousal_amplifies_salience(svc: AffectService):
    """Эмоционально заряженное помнится лучше (amygdala modulation)."""
    calm = svc.effective_salience(importance=0.4, surprise=0.0, text="обычное")
    svc.receive_message(text="ненавижу, заткнись!", chat_id=OWNER)
    charged = svc.effective_salience(importance=0.4, surprise=0.5, text="крик")
    assert charged >= calm


# ------------------------------------------------------------ безопасность

def test_breaker_allows_safe_actions(svc: AffectService):
    assert svc.check_action("remember").allowed is True


def test_breaker_blocks_in_rage(svc: AffectService):
    """В ярости деструктивные действия запрещены."""
    svc.engine.set_state(s=0.10, n=0.90, d=0.90)
    decision = svc.check_action("self_edit")
    assert decision.allowed is False
    assert decision.zone == "rage"
    assert decision.force_counterweight is True


def test_breaker_zones_detected(svc: AffectService):
    svc.engine.set_state(s=0.05, n=0.4, d=0.10)
    assert svc.stats()["zone"] == "collapse"
    svc.engine.set_state(s=0.10, n=0.95, d=0.5)
    assert svc.stats()["zone"] == "panic"
    svc.engine.set_state(s=0.6, n=0.4, d=0.6)
    assert svc.stats()["zone"] is None


def test_reappraisal_changes_state(svc: AffectService):
    svc.engine.set_state(s=0.10, n=0.90, d=0.90)
    svc.check_action("self_edit", now=time.time())
    # переоценка поднимает S и снижает N
    assert svc.snapshot.mono.s > 0.10


# ------------------------------------------------------------ генерация

def test_sampling_params_present(svc: AffectService):
    params = svc.sampling_params()
    assert "temperature" in params
    assert "max_tokens" in params
    assert params["temperature"] > 0


def test_rage_raises_temperature(svc: AffectService):
    calm = svc.sampling_params()["temperature"]
    svc.engine.set_state(s=0.10, n=0.95, d=0.90)
    hot = svc.sampling_params()["temperature"]
    assert hot > calm


def test_fatigue_shortens_output(svc: AffectService):
    base = svc.sampling_params()["max_tokens"]
    svc.engine.set_homeostasis(fatigue=0.95)
    assert svc.sampling_params()["max_tokens"] < base


def test_telemetry_is_numeric_not_instructional(svc: AffectService):
    """Телеметрия — числа, а не «ты злишься»."""
    header = svc.telemetry_header()
    assert "interoceptive_telemetry" in header
    for forbidden in ("злишься", "обидно", "агрессивно", "ярости"):
        assert forbidden not in header


# ------------------------------------------------------------ ecphory

def test_retrieve_prefers_mood_matching(svc: AffectService, store: EpisodicStore):
    """В негативном состоянии первой всплывает негативная сцена."""
    negative = Episode(text="он меня обозвал последним словом",
                       importance=0.7, stage="consolidated",
                       embedding=[1.0, 0.0])
    negative.pad_p, negative.pad_a, negative.pad_dom = -0.6, 0.4, -0.3
    positive = Episode(text="он меня похвалил за работу",
                       importance=0.7, stage="consolidated",
                       embedding=[1.0, 0.0])
    positive.pad_p, positive.pad_a, positive.pad_dom = 0.6, 0.2, 0.4
    store.add_episode(negative)
    store.add_episode(positive)

    svc.engine.set_state(s=0.10, n=0.60, d=0.40)
    hits = svc.retrieve(query="он сказал", query_embedding=[1.0, 0.0],
                        store=store)
    assert hits
    assert hits[0].node.episode_id == negative.episode_id


def test_retrieve_empty_store(svc: AffectService, store: EpisodicStore):
    assert svc.retrieve(query="что-то", query_embedding=[1.0], store=store) == []


# ------------------------------------------------------------ сон

def test_wake_resets_fatigue(svc: AffectService):
    svc.engine.set_homeostasis(fatigue=0.9, cognitive_load=0.5)
    svc.on_wake()
    assert svc.snapshot.homeostasis.fatigue < 0.01


def test_wake_shifts_toward_baseline(svc: AffectService):
    svc.engine.set_state(s=0.05, n=0.95, d=0.05)
    svc.on_wake()
    mono = svc.snapshot.mono
    assert mono.s > 0.05
    assert mono.n < 0.95


# ------------------------------------------------------------ статистика

def test_stats_shape(svc: AffectService):
    stats = svc.stats()
    for key in ("mono", "pad", "homeostasis", "counterweight", "zone", "breaker"):
        assert key in stats


def test_tick_advances_time(svc: AffectService):
    svc.engine.set_state(s=1.0, n=1.0, d=1.0)
    start = time.time()
    svc.tick(now=start + 3600)
    # за час клиренс заметно снизил состояние
    assert svc.snapshot.mono.n < 0.95
