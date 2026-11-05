"""Осведомлённость о социальном отказе и затухание по семействам.

Проверяем именно то, что просил владелец: она должна ЗНАТЬ, что её
замутили или выгнали, и это должно быть видно в логах и в промпте.
"""
from __future__ import annotations

import math
import tempfile
from pathlib import Path

from kuni.affect.appraisal import FastAppraisalExtractor
from kuni.affect.service import AffectService
from kuni.character.emotions import (DECAY_TAU_SECONDS, EMOTION_DECAY_TAU,
                                     EmotionState)


# --------------------------------------------------- осведомлённость в промпте

def _persona():
    from kuni.character.persona import PersonaBuilder
    from kuni.character.prompts import PromptLibrary
    from kuni.core.config import load_config

    root = Path(__file__).resolve().parent.parent
    cfg = load_config(root / "config.toml")

    class FakeCfg:
        current = cfg

    return PersonaBuilder(PromptLibrary(root / "prompts"), None, FakeCfg())


def test_calm_state_has_no_awareness_block():
    assert _persona()._social_awareness({}) == ""


def test_mute_is_stated_explicitly():
    pb = _persona()
    text = pb._social_awareness({
        "stage": "reflexive", "pain_index": 0.5, "muted": True,
        "kicked": False, "cluster": "power", "chat_title": "NiVa Chat"})
    assert "запрещено писать" in text
    assert "NiVa Chat" in text


def test_kick_is_stated_explicitly():
    """Ключевое требование: она должна понимать, что её ВЫГНАЛИ."""
    pb = _persona()
    text = pb._social_awareness({
        "stage": "reflexive", "pain_index": 0.5, "muted": False,
        "kicked": True, "cluster": "power",
        "chat_title": "клуб любителей куни"})
    assert "выгнали" in text
    assert "клуб любителей куни" in text
    assert "больше не участник" in text
    assert "из «" in text          # падеж не сломан


def test_stage_changes_wording():
    pb = _persona()
    base = {"pain_index": 0.4, "muted": True, "cluster": "power",
            "chat_title": "чат"}
    reflexive = pb._social_awareness({**base, "stage": "reflexive"})
    reflective = pb._social_awareness({**base, "stage": "reflective"})
    resignation = pb._social_awareness({**base, "stage": "resignation"})
    assert "только что" in reflexive
    assert "осадок" in reflective
    assert "отстранилась" in resignation


def test_cluster_changes_wording():
    pb = _persona()
    power = pb._social_awareness({
        "stage": "reflexive", "pain_index": 0.5, "muted": True,
        "cluster": "power", "chat_title": "x"})
    belonging = pb._social_awareness({
        "stage": "reflexive", "pain_index": 0.5, "muted": True,
        "cluster": "belonging", "chat_title": "x"})
    assert "ничего не можешь изменить" in power
    assert "не хотят" in belonging


# ---------------------------------------------------- осведомлённость в сервисе

def test_payload_carries_kicked_flag():
    db = Path(tempfile.mkdtemp()) / "p.db"
    svc = AffectService(db, owner_chat_id=1)
    ex = FastAppraisalExtractor()
    vec = ex.extract_from_system_event("kicked", w_rel=1.5, witnesses=5)
    svc.apply_social_event(-100, "kicked", vec, witnesses=5)
    state = svc.social_state(-100)
    assert state.last_event == "kicked"
    assert state.is_muted is False


def test_mute_sets_muted_flag():
    db = Path(tempfile.mkdtemp()) / "p2.db"
    svc = AffectService(db, owner_chat_id=1)
    ex = FastAppraisalExtractor()
    vec = ex.extract_from_system_event("muted", w_rel=1.5)
    svc.apply_social_event(-101, "muted", vec)
    assert svc.social_state(-101).is_muted is True


# -------------------------------------------------- затухание по семействам

def test_emotion_families_have_different_tau():
    """Испуг должен гаснуть в разы быстрее обиды."""
    fear = EMOTION_DECAY_TAU["страх"]
    offence = EMOTION_DECAY_TAU["обида"]
    assert offence > fear * 10


def test_fear_half_life_under_hour():
    half = EMOTION_DECAY_TAU["страх"] * math.log(2) / 3600
    assert half < 1.0


def test_offence_half_life_hours():
    half = EMOTION_DECAY_TAU["обида"] * math.log(2) / 3600
    assert 8.0 < half < 20.0


def test_unknown_emotion_uses_default():
    state = EmotionState(emotion="неведомое", intensity=1.0, ts=0.0)
    decayed = state.decayed()
    assert decayed.intensity < 1.0


def test_rumination_stretches_decay():
    """Что пережёвывают — не гаснет."""
    import time
    now = time.time()
    state = EmotionState(emotion="обида", intensity=1.0, ts=now - 3600)
    without = state.decay(rumination_charge=0.0)
    with_rum = state.decay(rumination_charge=1.0)
    assert with_rum.intensity > without.intensity


def test_fear_decays_faster_than_offence_in_practice():
    import time
    age = 2 * 3600     # два часа
    now = time.time()
    fear = EmotionState(emotion="страх", intensity=1.0, ts=now - age).decayed()
    offence = EmotionState(emotion="обида", intensity=1.0,
                           ts=now - age).decayed()
    assert fear.intensity < 0.15, "испуг за два часа должен почти пройти"
    assert offence.intensity > 0.5, "обида за два часа должна остаться"
