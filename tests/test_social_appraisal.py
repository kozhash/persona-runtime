"""Тесты когнитивной оценки и социальной боли.

Покрывают именно те места, где критик нашёл блокеры и тихие провалы:
порог ложных обид, расчёт индексов, накопление урона, стадии по Уильямсу,
затухание, сохранение следов руминации после рестарта.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from kuni.affect.appraisal import (AppraisalVector, FastAppraisalExtractor,
                                  compute_monoamine_burst, merge_cortical_sec)
from kuni.affect.service import AffectService
from kuni.affect.social import (NEED_NORM, RuminationEngine, RuminationState,
                                SocialPainSnapshot, SocialPainTracker)


# ---------------------------------------------------------------- appraisal

def test_warm_message_is_positive():
    ex = FastAppraisalExtractor()
    vec = ex.extract_from_message("спасибо, ты лучшая", chat_type="private",
                                  w_rel=2.0)
    assert vec.congruence > 0.5
    assert vec.threat_index() == 0.0


def test_direct_insult_is_negative_and_intentional():
    ex = FastAppraisalExtractor()
    vec = ex.extract_from_message("ты тупая, пошла нахуй",
                                  chat_type="private", w_rel=2.0)
    assert vec.congruence < -0.8
    assert vec.agency > 0.8        # умышленно, а не случайно
    assert vec.norm < -0.5         # нарушение нормы


def test_cold_reply_alone_is_not_offence():
    """«ок» и «понятно» — не оскорбление. Критик поймал это как DRIFT."""
    ex = FastAppraisalExtractor()
    for text in ("ок", "понятно", "ясно", "как хочешь"):
        vec = ex.extract_from_message(text, chat_type="private", w_rel=2.0)
        assert vec.congruence > -0.35, f"{text} не должен считаться обидой"


def test_cold_with_context_is_offence():
    ex = FastAppraisalExtractor()
    vec = ex.extract_from_message("ок", chat_type="private", w_rel=2.0,
                                  is_cold_after_emotional=True)
    assert vec.congruence <= -0.35


def test_plain_criticism_is_neutral():
    """Критика работы — не удар по личности."""
    ex = FastAppraisalExtractor()
    for text in ("это плохо написано", "мне не нравится", "ты ошибаешься"):
        vec = ex.extract_from_message(text, chat_type="private", w_rel=2.0)
        assert vec.threat_index() < 0.2, f"{text} не должен быть угрозой"


def test_muted_kills_coping():
    """Мут отнимает возможность ответить — отсюда беспомощность, не злость."""
    ex = FastAppraisalExtractor()
    vec = ex.extract_from_system_event("muted", w_rel=1.5)
    assert vec.coping < 0.1
    assert vec.loss_index() > vec.agonistic_index()


def test_insult_keeps_coping():
    """Оскорбление в личке оставляет возможность ответить — отсюда гнев."""
    ex = FastAppraisalExtractor()
    vec = ex.extract_from_message("ты тупая", chat_type="private", w_rel=2.0)
    assert vec.coping > 0.5
    assert vec.agonistic_index() > 0.0


def test_agonistic_raises_dopamine():
    """Ключевое: без этого зона ярости недостижима."""
    ex = FastAppraisalExtractor()
    vec = ex.extract_from_message("ты тупая, пошла нахуй",
                                  chat_type="private", w_rel=2.0)
    ds, dn, dd = compute_monoamine_burst(vec)
    assert ds < 0          # серотонин падает
    assert dn > 0          # норадреналин растёт
    assert dd > 0          # дофамин РАСТЁТ, а не падает


def test_loss_lowers_dopamine():
    """Беспомощность, наоборот, роняет дофамин."""
    ex = FastAppraisalExtractor()
    vec = ex.extract_from_system_event("kicked", w_rel=1.5)
    ds, dn, dd = compute_monoamine_burst(vec)
    assert dd < 0


def test_cortical_correction_reverses_false_alarm():
    """Дружеская шутка, принятая за оскорбление, должна погаснуть."""
    ex = FastAppraisalExtractor()
    fast = ex.extract_from_message("дура, ну ты и умница",
                                   chat_type="private", w_rel=2.0)
    assert fast.congruence < 0
    merged, correction = merge_cortical_sec(
        fast, {"cong": 0.8, "agency": 0.1, "cope": 0.8, "norm": 0.5,
               "ego": 0.3, "rel": 1.0})
    assert merged.congruence > fast.congruence
    assert correction.congruence > 0     # поправка обратного знака


# ------------------------------------------------------------ социальная боль

def test_insult_damages_esteem_most():
    tracker = SocialPainTracker()
    ex = FastAppraisalExtractor()
    state = SocialPainSnapshot(chat_id=1)
    vec = ex.extract_from_message("ты тупая", chat_type="private", w_rel=2.0)
    state = tracker.apply_event(state, vec, "insult")
    deficits = state.deficits()
    assert deficits["esteem"] > deficits["control"]


def test_mute_damages_control_most():
    """Мут бьёт по контролю и значимости — кластер power."""
    tracker = SocialPainTracker()
    ex = FastAppraisalExtractor()
    state = SocialPainSnapshot(chat_id=1)
    vec = ex.extract_from_system_event("muted", w_rel=1.5)
    state = tracker.apply_event(state, vec, "muted", witnesses=5)
    assert state.cluster() == "power"
    assert state.is_muted is True


def test_single_event_does_not_zero_needs():
    """Один бан не превращает в пустоту мгновенно — урон накапливается."""
    tracker = SocialPainTracker()
    ex = FastAppraisalExtractor()
    state = SocialPainSnapshot(chat_id=1)
    vec = ex.extract_from_system_event("banned", w_rel=1.5)
    state = tracker.apply_event(state, vec, "banned")
    for need in (state.need_belong, state.need_esteem,
                 state.need_control, state.need_meaning):
        assert need > 0.2, "потребность не должна обнуляться одним событием"


def test_irrelevant_event_does_not_hurt():
    """Оскорбление постороннего в группе — не про неё."""
    tracker = SocialPainTracker()
    state = SocialPainSnapshot(chat_id=1)
    bystander = AppraisalVector(relevance=0.05, congruence=-0.9, agency=0.9,
                               coping=0.5, norm=-0.5, ego=0.3, w_rel=1.0)
    state = tracker.apply_event(state, bystander, "insult")
    assert state.pain_index < 0.1


def test_pain_decays_over_time():
    tracker = SocialPainTracker()
    ex = FastAppraisalExtractor()
    state = SocialPainSnapshot(chat_id=1)
    vec = ex.extract_from_system_event("muted", w_rel=1.5)
    state = tracker.apply_event(state, vec, "muted", now=1000.0)
    initial = state.pain_index
    later = tracker.step_decay(state, 14 * 3600, now=1000.0 + 14 * 3600)
    assert later.pain_index < initial * 0.5


def test_apology_repairs_pain():
    tracker = SocialPainTracker()
    ex = FastAppraisalExtractor()
    state = SocialPainSnapshot(chat_id=1)
    vec = ex.extract_from_message("ты меня достала", chat_type="private",
                                  w_rel=2.5)
    state = tracker.apply_event(state, vec, "insult")
    before = state.pain_index
    state = tracker.repair(state, "apology")
    assert state.pain_index < before


def test_stage_advances_by_time():
    tracker = SocialPainTracker()
    ex = FastAppraisalExtractor()
    state = SocialPainSnapshot(chat_id=1)
    vec = ex.extract_from_system_event("muted", w_rel=1.5)
    state = tracker.apply_event(state, vec, "muted", now=1000.0)
    assert state.stage == "reflexive"
    state = tracker.step_decay(state, 3600, now=1000.0 + 3600)
    assert state.stage == "reflective"


# ---------------------------------------------------------------- руминация

def test_rumination_spawns_on_insult():
    engine = RuminationEngine()
    ex = FastAppraisalExtractor()
    vec = ex.extract_from_message("ты тупая, пошла нахуй",
                                  chat_type="private", w_rel=2.5)
    assert engine.should_spawn(vec, social_pain=0.6)
    assert engine.initial_charge(vec) > 0.5


def test_rumination_does_not_spawn_on_praise():
    engine = RuminationEngine()
    ex = FastAppraisalExtractor()
    vec = ex.extract_from_message("спасибо, ты лучшая", chat_type="private",
                                  w_rel=2.5)
    assert not engine.should_spawn(vec)


def test_brooding_then_reflection():
    """Сразу после обиды — пережёвывание, через часы — рефлексия."""
    engine = RuminationEngine()
    assert engine.mode_rho(s=0.20, n=0.85, coping=0.2) < 0.5
    assert engine.mode_rho(s=0.55, n=0.40, coping=0.7) >= 0.5


def test_charge_decays():
    engine = RuminationEngine()
    state = RuminationState(trace_id="t", chat_id=1, current_charge=0.9,
                            created_ts=0.0, updated_ts=0.0)
    later = engine.step(state, 3600, s=0.3, n=0.7, coping=0.3, now=3600)
    assert later.current_charge < state.current_charge


def test_apology_relieves_but_not_fully():
    engine = RuminationEngine()
    state = RuminationState(trace_id="t", chat_id=1, current_charge=0.9)
    relieved = engine.apology_relief(state)
    assert 0.2 < relieved.current_charge < 0.9


# ------------------------------------------------------- интеграция сервиса

def test_mute_reaches_monoamines():
    """Блокер критика: путь мута был мёртв из-за frozen dataclass."""
    db = Path(tempfile.mkdtemp()) / "t.db"
    svc = AffectService(db, owner_chat_id=1)
    before = svc.engine.mono
    ex = FastAppraisalExtractor()
    vec = ex.extract_from_system_event("muted", w_rel=1.5)
    svc.apply_appraisal(vec, chat_id=-100, source="social.muted")
    after = svc.engine.mono
    assert abs(after.s - before.s) > 0.05, "мут должен двигать состояние"


def test_tick_does_not_block_on_long_idle():
    """Блокер: сутки простоя давали 1.9 с синхронной работы."""
    import time
    db = Path(tempfile.mkdtemp()) / "t2.db"
    svc = AffectService(db, owner_chat_id=1)
    start = time.time()
    svc.tick(time.time() + 7 * 86400)
    elapsed = time.time() - start
    assert elapsed < 0.5, f"тик после недели простоя занял {elapsed:.2f} с"


def test_rumination_accumulates_one_trace():
    """Три оскорбления — один след с накопленным зарядом, не три."""
    db = Path(tempfile.mkdtemp()) / "t3.db"
    svc = AffectService(db, owner_chat_id=1)
    ex = FastAppraisalExtractor()
    vec = ex.extract_from_message("ты тупая, пошла нахуй",
                                  chat_type="private", w_rel=2.0)
    first = svc.spawn_rumination(1, vec, summary="раз")
    second = svc.spawn_rumination(1, vec, summary="два")
    count = svc.db.execute(
        "SELECT COUNT(*) FROM rumination_traces").fetchone()[0]
    assert count == 1
    assert second.current_charge >= first.current_charge


def test_rumination_survives_restart():
    """Обида не должна исчезать при перезапуске процесса."""
    db = Path(tempfile.mkdtemp()) / "t4.db"
    svc = AffectService(db, owner_chat_id=1)
    ex = FastAppraisalExtractor()
    vec = ex.extract_from_message("ты тупая", chat_type="private", w_rel=2.0)
    svc.spawn_rumination(7, vec, summary="обида")
    svc.db.close()
    again = AffectService(db, owner_chat_id=1)
    assert again.rumination(7) is not None


def test_social_state_survives_restart():
    db = Path(tempfile.mkdtemp()) / "t5.db"
    svc = AffectService(db, owner_chat_id=1)
    ex = FastAppraisalExtractor()
    vec = ex.extract_from_system_event("muted", w_rel=1.5)
    svc.apply_social_event(-100, "muted", vec, witnesses=5)
    svc.db.close()
    again = AffectService(db, owner_chat_id=1)
    state = again.social_state(-100)
    assert state.pain_index > 0.1
    assert state.is_muted is True
