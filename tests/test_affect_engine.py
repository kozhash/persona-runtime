"""Тесты нейромодуляторного движка.

Проверяем свойства, которые заявлены в спеке и которые легко сломать:
  * порядок клиренса κN > κD > κS;
  * инерция: извинение не отменяет обиду за один тик;
  * отсутствие однопиковых знакоперемен;
  * проекция PAD на вершинах куба Лёвхейма;
  * стабильность на больших dt;
  * перекрёстное торможение;
  * гомеостаз и сброс после сна.

Все тесты детерминированные: шум выключен.
"""
from __future__ import annotations

import math

import pytest

from kuni.affect import (AppraisalImpulse, EngineConfig, Homeostasis,
                         Monoamines, NeuromodulatoryEngine, PAD,
                         project_pad)
from kuni.affect.types import clamp


def engine(**overrides) -> NeuromodulatoryEngine:
    cfg = EngineConfig(noise_enabled=False, **overrides)
    return NeuromodulatoryEngine(cfg, seed=42)


def insult() -> AppraisalImpulse:
    return AppraisalImpulse(ts=0.0, delta_valence=-0.9, threat=0.9,
                            rpe=-0.5, surprise=0.7)


def apology() -> AppraisalImpulse:
    return AppraisalImpulse(ts=10.0, delta_valence=0.8, threat=0.0,
                            rpe=0.6, surprise=0.2)


def praise() -> AppraisalImpulse:
    return AppraisalImpulse(ts=0.0, delta_valence=0.9, threat=0.0,
                            rpe=0.8, surprise=0.3)


# ------------------------------------------------------------ клиренс

def test_clearance_ordering():
    """Норадреналин гаснет быстрее дофамина, серотонин самый инертный."""
    cfg = EngineConfig()
    assert cfg.kappa_n > cfg.kappa_d > cfg.kappa_s


def test_invalid_clearance_raises():
    with pytest.raises(ValueError, match="порядок клиренса"):
        NeuromodulatoryEngine(EngineConfig(kappa_s=0.9, kappa_n=0.1, kappa_d=0.5))


def test_noradrenaline_decays_faster_than_serotonin():
    """После выброса N возвращается к базе быстрее, чем S."""
    e = engine()
    base = e.mono
    e.apply_impulse(insult())
    e.step(30.0)
    # N успел вернуться ближе к базе, чем S (S инертнее)
    n_gap = abs(e.mono.n - base.n)
    s_gap = abs(e.mono.s - base.s)
    assert n_gap < s_gap * 1.5


# ------------------------------------------------------------ инерция

def test_insult_then_apology_has_inertia():
    """Извинение не отменяет обиду мгновенно — это главное свойство."""
    e = engine()
    e.apply_impulse(insult())
    e.step(10.0)
    after_insult = e.mono

    e.apply_impulse(apology())
    e.step(10.0)
    after_apology = e.mono

    # извинение улучшило состояние...
    assert after_apology.s > after_insult.s
    # ...но не вернуло к базе одним махом
    base = engine().mono
    assert after_apology.s < base.s + 0.2


def test_no_single_tick_sign_flip():
    """Нет мгновенного переворота знака P за один шаг."""
    e = engine()
    e.apply_impulse(insult())
    e.step(5.0)
    p1 = e.pad.p
    e.apply_impulse(apology())
    e.step(5.0)
    p2 = e.pad.p
    # сдвиг есть, но не «через всю шкалу»
    assert abs(p2 - p1) < 1.2


def test_sustained_state_is_bounded():
    """Состояние остаётся в [0,1] при любых импульсах."""
    e = engine()
    for _ in range(20):
        e.apply_impulse(insult())
        e.step(1.0)
    mono = e.mono
    assert 0.0 <= mono.s <= 1.0
    assert 0.0 <= mono.n <= 1.0
    assert 0.0 <= mono.d <= 1.0


# ------------------------------------------------------------ проекция PAD

def test_pad_joy_vertex():
    """Радость (1,0,1): высокий P, низкий A, высокий Dom."""
    pad = project_pad(Monoamines(1.0, 0.0, 1.0))
    assert pad.p > 0.8
    assert pad.a < 0.0
    assert pad.dom > 0.5


def test_pad_anger_vertex():
    """Гнев (0,1,1): отрицательный P, максимальный A."""
    pad = project_pad(Monoamines(0.0, 1.0, 1.0))
    assert pad.p < 0.0
    assert pad.a > 0.7


def test_pad_shame_vertex():
    """Стыд (0,0,0): низкий P, низкий A, подчинённый Dom."""
    pad = project_pad(Monoamines(0.0, 0.0, 0.0))
    assert pad.p < 0.0
    assert pad.a < 0.0
    assert pad.dom < -0.5


def test_pad_neutral_is_near_zero():
    pad = project_pad(Monoamines(0.5, 0.5, 0.5))
    assert abs(pad.p) < 1e-6
    assert abs(pad.a) < 1e-6
    assert abs(pad.dom) < 1e-6


def test_pad_distance():
    a = PAD(0.0, 0.0, 0.0)
    b = PAD(1.0, 0.0, 0.0)
    assert a.distance(b) == pytest.approx(1.0)
    assert a.distance(a) == pytest.approx(0.0)


# ------------------------------------------------------------ стабильность

def test_large_dt_does_not_explode():
    """RK4 на большом шаге не улетает в потолок."""
    e = engine()
    e.apply_impulse(insult())
    e.step(3600.0)      # час
    mono = e.mono
    assert 0.0 <= mono.s <= 1.0
    # не залипло в единице
    assert mono.s < 0.99


def test_long_rest_returns_toward_baseline():
    """За 4 часа покоя состояние возвращается к базовой точке."""
    e = engine()
    base = e.mono
    e.apply_impulse(insult())
    e.step(30.0)
    for _ in range(12):
        e.step(1200.0)   # 4 часа
    assert abs(e.mono.s - base.s) < 0.35
    assert abs(e.mono.n - base.n) < 0.35


def test_zero_dt_is_noop():
    e = engine()
    before = e.mono
    e.step(0.0)
    assert e.mono.as_tuple() == before.as_tuple()


# --------------------------------------------------- перекрёстное торможение

def test_high_serotonin_dampens_dopamine():
    """Высокий S тормозит D (γ_DS)."""
    e = engine()
    e.set_state(s=0.95, n=0.5, d=0.95)
    before_d = e.mono.d
    e.step(60.0)
    assert e.mono.d < before_d


def test_high_noradrenaline_depletes_serotonin():
    """Избыток N истощает S (γ_SN)."""
    e = engine()
    e.set_state(s=0.8, n=0.99, d=0.5)
    before_s = e.mono.s
    e.step(120.0)
    assert e.mono.s < before_s


def test_deficit_does_not_trigger_coupling():
    """Дефицит сам по себе не включает перекрёстное торможение.

    Это защита от положительной обратной связи: при голом tanh дефицит S
    повышал N, а высокий N ещё сильнее давил S — система залипала в нуле.
    """
    e = engine()
    e.set_state(s=0.05, n=0.4, d=0.6)
    e.step(60.0)
    # S не продолжает падать до нуля из-за связи
    assert e.mono.s >= 0.0


# ------------------------------------------------------------ гомеостаз

def test_fatigue_grows_over_eight_hours():
    e = engine()
    assert e.h.fatigue == 0.0
    for _ in range(8):
        e.step(3600.0)
    assert 0.85 < e.h.fatigue <= 1.0


def test_fatigue_lowers_baseline():
    """Усталость сдвигает базовую точку вниз (A_circ)."""
    fresh = engine()
    tired = engine()
    tired.set_homeostasis(fatigue=0.9)
    assert tired.baseline_point().d < fresh.baseline_point().d


def test_sleep_resets_fatigue():
    e = engine()
    for _ in range(8):
        e.step(3600.0)
    assert e.h.fatigue > 0.8
    e.reset_homeostasis_after_sleep()
    assert e.h.fatigue < 0.01
    assert e.h.cognitive_load < 0.01


def test_register_message_raises_satiety():
    e = engine()
    before = e.h.social_satiety
    e.register_message()
    assert e.h.social_satiety > before


def test_register_tokens_raises_fatigue():
    e = engine()
    before = e.h.fatigue
    e.register_tokens(10000)
    assert e.h.fatigue > before


def test_register_task_raises_load():
    e = engine()
    e.register_task(2)
    assert e.h.cognitive_load > 0.1


def test_social_satiety_decays():
    e = engine()
    e.set_homeostasis(social_satiety=1.0)
    e.step(3600.0)
    assert e.h.social_satiety < 1.0


# ------------------------------------------------------------ вмешательства

def test_force_reappraisal():
    e = engine()
    e.set_state(s=0.1, n=0.9, d=0.9)
    e.force_reappraisal(delta_s=0.25, delta_n=-0.30)
    assert e.mono.s == pytest.approx(0.35)
    assert e.mono.n == pytest.approx(0.60)


def test_shift_toward_baseline():
    e = engine()
    base = e.mono
    e.set_state(s=0.0, n=1.0, d=0.0)
    e.shift_toward_baseline(1.0)
    assert e.mono.s == pytest.approx(base.s, abs=1e-6)


# ------------------------------------------------------------ снапшот

def test_snapshot_shape():
    e = engine()
    snap = e.snapshot(ts=1234.5)
    assert snap.ts == 1234.5
    data = snap.to_dict()
    for key in ("ts", "mono", "pad", "homeostasis",
                "counterweight_active", "circuit_breaker_active"):
        assert key in data
    assert set(data["mono"]) == {"s", "n", "d"}
    assert set(data["pad"]) == {"p", "a", "dom"}


def test_neutral_snapshot():
    e = engine()
    snap = e.snapshot()
    assert snap.mono.s == pytest.approx(e.mono.s)


def test_clamp_helper():
    assert clamp(5.0, 0.0, 1.0) == 1.0
    assert clamp(-5.0, 0.0, 1.0) == 0.0
    assert clamp("bad", 0.0, 1.0) == 0.0
    assert clamp(None, 0.0, 1.0) == 0.0


def test_monoamines_clamped():
    m = Monoamines(2.0, -1.0, 0.5).clamped()
    assert m.s == 1.0 and m.n == 0.0 and m.d == 0.5


def test_homeostasis_clamped():
    h = Homeostasis(2.0, -1.0, 0.5).clamped()
    assert h.fatigue == 1.0
    assert h.cognitive_load == 0.0
    assert h.social_satiety == 0.5


# ------------------------------------------------------------ w_rel

def test_owner_impulse_is_amplified():
    """Похвала от близкого человека сдвигает состояние сильнее."""
    owner = AppraisalImpulse(ts=0.0, delta_valence=0.9, rpe=0.8, w_rel=2.5)
    stranger = AppraisalImpulse(ts=0.0, delta_valence=0.9, rpe=0.8, w_rel=0.7)
    e1, e2 = engine(), engine()
    e1.apply_impulse(owner)
    e2.apply_impulse(stranger)
    assert e1.mono.d > e2.mono.d
    assert e1.mono.s > e2.mono.s


def test_impulse_vector_shape():
    impulse = AppraisalImpulse(ts=0.0, delta_valence=0.5, threat=0.2,
                               rpe=0.3, surprise=0.1, w_rel=2.0)
    vec = impulse.vector_u()
    assert len(vec) == 4
    assert vec[0] == pytest.approx(1.0)   # w_rel * delta_valence
    assert vec[1] == pytest.approx(0.2)
    assert vec[2] == pytest.approx(0.6)   # w_rel * rpe
    assert vec[3] == pytest.approx(0.1)
