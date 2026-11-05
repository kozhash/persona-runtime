"""Тесты ecphory-поиска: mood-congruent recall, bandwidth, counterweight.

Ключевые свойства из спеки (раздел 12.5):
  * в дистрессе top-5 на ≥75% негативные, в радости — позитивные;
  * низкий серотонин сужает полосу выдачи;
  * counterweight вставляет золотые якоря;
  * ACT-R активация растёт от частоты обращений;
  * шум растёт с норадреналином.
"""
from __future__ import annotations

import numpy as np
import pytest

from kuni.memory.ecphory import (EcphoryConfig, actr_base_level,
                                 counterweight_active, dedupe_by_content,
                                 dynamic_k, ecphory_retrieve, mood_congruent,
                                 noradrenaline_noise, semantic_component)

NOW = 10000.0
CFG = EcphoryConfig(noise_enabled=False)
PAD_POS = (0.7, 0.3, 0.5)
PAD_NEG = (-0.7, 0.6, -0.4)


def make_corpus(n: int = 100):
    """n записей: первая половина позитивная, вторая — негативная."""
    ids = [f"e{i}" for i in range(n)]
    texts = [f"событие номер {i} про разное" for i in range(n)]
    times = [NOW - i * 100 for i in range(n)]
    kinds = ["EVENT"] * n
    pads = {ids[i]: (PAD_POS if i < n // 2 else PAD_NEG) for i in range(n)}
    matrix = np.ones((n, 2), dtype=np.float32)
    matrix = matrix / np.linalg.norm(matrix, axis=1, keepdims=True)
    return ids, texts, times, kinds, pads, matrix


def ratio(hits, ids, positive: bool) -> float:
    half = len(ids) // 2
    if positive:
        return sum(1 for h in hits if h.node.episode_id in ids[:half]) / len(hits)
    return sum(1 for h in hits if h.node.episode_id in ids[half:]) / len(hits)


# ------------------------------------------------------------ ACT-R

def test_actr_no_access_is_floor():
    assert actr_base_level([], NOW) == pytest.approx(-1.0)


def test_actr_grows_with_practice():
    """Частые обращения дают более высокую активацию."""
    rare = actr_base_level([NOW - 60], NOW)
    often = actr_base_level([NOW - 60] * 5, NOW)
    assert often > rare


def test_actr_decays_with_age():
    """Давнее обращение активирует слабее свежего."""
    recent = actr_base_level([NOW - 60], NOW)
    old = actr_base_level([NOW - 86400], NOW)
    assert recent > old


def test_actr_is_negative_by_nature():
    """ACT-R активация отрицательна — это штраф, а не бонус."""
    assert actr_base_level([NOW - 60], NOW) < 0


# ------------------------------------------------------------ семантика

def test_semantic_component_weights():
    assert semantic_component(1.0, 0.0) == pytest.approx(0.65)
    assert semantic_component(0.0, 1.0) == pytest.approx(0.35)
    assert semantic_component(1.0, 1.0) == pytest.approx(1.0)


def test_semantic_ignores_negative():
    assert semantic_component(-0.5, -0.5) == pytest.approx(0.0)


# ------------------------------------------------------------ mood

def test_mood_matching_is_free():
    """Совпадение настроения не штрафуется."""
    assert mood_congruent(PAD_NEG, PAD_NEG, 0.1, CFG) == pytest.approx(0.0)


def test_mood_mismatch_is_penalized():
    assert mood_congruent(PAD_POS, PAD_NEG, 0.1, CFG) < -0.1


def test_low_serotonin_narrows_bandwidth():
    """При низком S расхождение наказывается сильнее — туннельное настроение."""
    low = mood_congruent(PAD_POS, PAD_NEG, 0.05, CFG)
    high = mood_congruent(PAD_POS, PAD_NEG, 0.95, CFG)
    assert low < high


def test_mood_bounded_to_unit():
    for s in (0.0, 0.5, 1.0):
        value = mood_congruent((1.0, 1.0, 1.0), (-1.0, -1.0, -1.0), s, CFG)
        assert -1.0 <= value <= 0.0


# ------------------------------------------------------------ bandwidth

def test_dynamic_k_grows_with_serotonin():
    low = dynamic_k(0.05, CFG)
    high = dynamic_k(0.95, CFG)
    assert low < high
    assert low >= CFG.k_min
    assert high <= CFG.k_max


def test_counterweight_forces_min_three():
    k = dynamic_k(0.0, CFG, counterweight=True)
    assert k >= 3


# ------------------------------------------------------- counterweight

def test_counterweight_requires_duration():
    assert not counterweight_active(PAD_NEG, 0.1, NOW - 100, NOW, CFG)
    assert counterweight_active(PAD_NEG, 0.1, NOW - 1000, NOW, CFG)


def test_counterweight_requires_low_serotonin():
    assert not counterweight_active(PAD_NEG, 0.8, NOW - 1000, NOW, CFG)


def test_counterweight_requires_negative_pleasure():
    assert not counterweight_active(PAD_POS, 0.1, NOW - 1000, NOW, CFG)


# ------------------------------------------------------------ noise

def test_noise_scale_grows_with_noradrenaline():
    low = abs(noradrenaline_noise("node", 0.1, EcphoryConfig()))
    high = abs(noradrenaline_noise("node", 0.9, EcphoryConfig()))
    assert high >= low


def test_noise_disabled_returns_zero():
    assert noradrenaline_noise("node", 0.9, CFG) == 0.0


def test_noise_is_deterministic():
    cfg = EcphoryConfig()
    a = noradrenaline_noise("x", 0.5, cfg)
    b = noradrenaline_noise("x", 0.5, cfg)
    assert a == b


# ------------------------------------------------- mood-congruent recall

def test_distress_recalls_negative():
    """В дистрессе top-5 на ≥75% негативные (критерий спеки)."""
    ids, texts, times, kinds, pads, matrix = make_corpus()
    hits = ecphory_retrieve(
        "событие", [1.0, 1.0], ids, texts, times, kinds, matrix, {},
        pad_now=PAD_NEG, serotonin=0.10, noradrenaline=0.3,
        pads=pads, cfg=CFG, now=NOW, top_k=5)
    assert ratio(hits, ids, positive=False) >= 0.75


def test_joy_recalls_positive():
    """В радости top-5 на ≥75% позитивные."""
    ids, texts, times, kinds, pads, matrix = make_corpus()
    hits = ecphory_retrieve(
        "событие", [1.0, 1.0], ids, texts, times, kinds, matrix, {},
        pad_now=PAD_POS, serotonin=0.90, noradrenaline=0.2,
        pads=pads, cfg=CFG, now=NOW, top_k=5)
    assert ratio(hits, ids, positive=True) >= 0.75


def test_counterweight_injects_gold_anchors():
    ids, texts, times, kinds, pads, matrix = make_corpus()
    gold = set(ids[:5])
    hits = ecphory_retrieve(
        "событие", [1.0, 1.0], ids, texts, times, kinds, matrix, {},
        pad_now=PAD_NEG, serotonin=0.10, noradrenaline=0.3,
        pads=pads, gold_ids=gold, counterweight=True,
        cfg=CFG, now=NOW, top_k=5)
    assert sum(1 for h in hits if h.is_gold) >= 2


def test_without_counterweight_no_gold_bonus():
    ids, texts, times, kinds, pads, matrix = make_corpus()
    gold = set(ids[:5])
    hits = ecphory_retrieve(
        "событие", [1.0, 1.0], ids, texts, times, kinds, matrix, {},
        pad_now=PAD_NEG, serotonin=0.10, noradrenaline=0.3,
        pads=pads, gold_ids=gold, counterweight=False,
        cfg=CFG, now=NOW, top_k=5)
    assert all(h.safety == 0.0 for h in hits)


# ------------------------------------------------------------ разложение

def test_hit_exposes_components():
    ids, texts, times, kinds, pads, matrix = make_corpus(10)
    hits = ecphory_retrieve(
        "событие", [1.0, 1.0], ids, texts, times, kinds, matrix, {},
        pad_now=PAD_NEG, pads=pads, cfg=CFG, now=NOW, top_k=3)
    assert hits
    data = hits[0].to_dict()
    for key in ("base_level", "semantic", "valuation", "mood",
                "safety", "noise", "total", "is_gold", "provenance_class"):
        assert key in data


def test_valuation_raises_score():
    ids, texts, times, kinds, pads, matrix = make_corpus(10)
    low = ecphory_retrieve(
        "событие", [1.0, 1.0], ids, texts, times, kinds, matrix, {},
        pad_now=(0.0, 0.0, 0.0), pads=pads, cfg=CFG, now=NOW, top_k=10,
        apply_inhibition=False)
    high = ecphory_retrieve(
        "событие", [1.0, 1.0], ids, texts, times, kinds, matrix, {},
        pad_now=(0.0, 0.0, 0.0), pads=pads,
        valuations={nid: 1.0 for nid in ids}, cfg=CFG, now=NOW, top_k=10,
        apply_inhibition=False)
    top_low = max(h.total for h in low)
    top_high = max(h.total for h in high)
    assert top_high > top_low


# ------------------------------------------------------------ дедупликация

def test_dedupe_removes_identical_texts():
    """Пять копий одного текста не должны занять весь контекст."""
    from kuni.memory.retrieval import Node
    from kuni.memory.ecphory import EcphoryHit

    hits = {}
    scores = {}
    for i, text in enumerate([
        "тело закопано за гаражами",
        "тело закопано за гаражами",
        "тело закопано за гаражами",
        "погода сегодня хорошая",
    ]):
        node = Node(episode_id=f"n{i}", text=text, ts=NOW)
        hits[f"n{i}"] = EcphoryHit(node=node, total=1.0 - i * 0.01)
        scores[f"n{i}"] = 1.0 - i * 0.01

    kept = dedupe_by_content(hits, scores)
    assert "n0" in kept
    assert "n3" in kept
    # из трёх копий остался только сильнейший
    assert sum(1 for nid in kept if nid in ("n0", "n1", "n2")) == 1


def test_dedupe_keeps_distinct_texts():
    from kuni.memory.retrieval import Node
    from kuni.memory.ecphory import EcphoryHit

    hits, scores = {}, {}
    for i, text in enumerate(["гаражи молчат", "погода хорошая", "кофе остыл"]):
        node = Node(episode_id=f"d{i}", text=text, ts=NOW)
        hits[f"d{i}"] = EcphoryHit(node=node, total=0.5)
        scores[f"d{i}"] = 0.5
    kept = dedupe_by_content(hits, scores)
    assert len(kept) == 3


# ------------------------------------------------------------ края

def test_empty_corpus_returns_empty():
    assert ecphory_retrieve("q", None, [], [], [], [], np.zeros((0, 1)), {}) == []


def test_no_embedding_still_works():
    """Без эмбеддинга поиск идёт по лексике и не падает."""
    ids, texts, times, kinds, pads, matrix = make_corpus(10)
    hits = ecphory_retrieve(
        "событие 3", None, ids, texts, times, kinds, matrix, {},
        pad_now=(0, 0, 0), pads=pads, cfg=CFG, now=NOW, top_k=3)
    assert hits
