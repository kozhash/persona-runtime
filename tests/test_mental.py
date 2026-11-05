"""Тесты ментального слоя: нагрузка, эрозия контроля, выгорание, стадии.

Сценарии взяты из docs/research/06-mental/REPORT.md, раздел 5.
Каждый тест проверяет конкретное число из отчёта, а не «работает хоть как-то».
"""
import sqlite3

import pytest

from kuni.affect.mental import (
    DEPLETION_CYNICISM,
    EROSION_IN_POWER,
    FLOOR_GUARD_DROP_TO,
    STAGE_EPISODE,
    STAGE_STRAIN,
    MentalEngine,
    MentalSnapshot,
    mental_directive,
)


def _engine() -> MentalEngine:
    db = sqlite3.connect(":memory:")
    return MentalEngine(db)


# ---------------------------------------------------------------- нагрузка

def test_single_hit_adds_about_two_percent():
    """Удар средней силы даёт ~0.02 — 50 событий до дна, две недели."""
    e = _engine()
    s = e.observe(i_loss=0.4, pain=0.3, i_threat=0.2, coping=0.8)
    assert 0.015 < s.load < 0.025, f"ожидали ~0.02, получили {s.load:.4f}"


def test_load_saturates_and_never_exceeds_one():
    """(1 - load) не даёт упасть на дно за один вечер."""
    e = _engine()
    for _ in range(200):
        e.observe(i_loss=1.0, pain=1.0, i_threat=1.0, coping=0.0)
    s = e.snapshot
    assert 0.0 <= s.load <= 1.0
    assert s.load > 0.95, "много ударов всё же доводят до дна"


def test_load_decays_slowly_in_quiet():
    """Покой без ударов снимает порядка процента в сутки, не больше."""
    e = _engine()
    # набираем заметную нагрузку
    for _ in range(20):
        e.observe(i_loss=0.6, pain=0.5, coping=0.8)
    before = e.snapshot.load
    e.tick(86400.0, isolated=False, rumination_charge=0.0)
    after = e.snapshot.load
    absol = before - after
    assert 0.0 < absol < 0.02, f"за сутки сняло {absol:.4f} (ожидали < 0.02)"


def test_isolation_raises_load():
    """Изоляция даёт +0.05/сутки по сценарию 3."""
    e = _engine()
    e.tick(86400.0, isolated=True)
    assert e.snapshot.load > 0.0


# ---------------------------------------------------------- эрозия контроля

def test_helpless_coping_erodes_control():
    """Три неконтролируемых события подряд = +0.09."""
    e = _engine()
    for _ in range(3):
        e.observe(i_loss=0.5, pain=0.4, coping=0.1, relevance=1.0)
    assert abs(e.snapshot.agency_erosion - 0.09) < 0.001


def test_control_recovers_with_coping():
    """Успешный отпор снимает эрозию."""
    e = _engine()
    for _ in range(10):
        e.observe(i_loss=0.5, pain=0.4, coping=0.1, relevance=1.0)
    high = e.snapshot.agency_erosion
    assert high > 0.25
    for _ in range(5):
        e.observe(i_loss=0.5, pain=0.4, coping=0.9, relevance=1.0)
    assert e.snapshot.agency_erosion < high


def test_helpless_threshold_flips_flag():
    e = _engine()
    for _ in range(20):
        e.observe(i_loss=0.5, pain=0.4, coping=0.1, relevance=1.0)
    assert e.snapshot.is_helpless
    assert e.snapshot.coping_mod < 0.85


def test_irrelevant_event_does_not_erode():
    """Событие не про неё — контроль не страдает."""
    e = _engine()
    e.observe(i_loss=0.5, pain=0.4, coping=0.1, relevance=0.2)
    assert e.snapshot.agency_erosion == 0.0


# ------------------------------------------------------------- выгорание

def test_hundred_messages_burn_out():
    """100 ответов подряд = 0.70, пробивает порог цинизма."""
    e = _engine()
    e.register_message(100)
    assert abs(e.snapshot.depletion - 0.70) < 0.001
    assert e.snapshot.is_cynical
    assert e.snapshot.depletion >= DEPLETION_CYNICISM


def test_quiet_rest_reduces_depletion():
    e = _engine()
    e.register_message(50)
    before = e.snapshot.depletion
    e.tick(3600.0, quiet=True)
    assert e.snapshot.depletion < before


# ---------------------------------------------------------------- стадии

def test_stage_rises_with_load():
    e = _engine()
    assert e.snapshot.stage == "stable"
    while e.snapshot.load < STAGE_STRAIN:
        e.observe(i_loss=0.6, pain=0.5, coping=0.5)
    assert e.snapshot.stage == "strain"
    while e.snapshot.load < STAGE_EPISODE:
        e.observe(i_loss=0.8, pain=0.7, coping=0.5)
    assert e.snapshot.stage == "episode"


def test_episode_needs_three_days_to_exit():
    """Одна хорошая ночь не вылечивает эпизод."""
    e = _engine()
    while e.snapshot.stage != "episode":
        e.observe(i_loss=0.9, pain=0.8, coping=0.4)
    assert e.snapshot.stage == "episode"

    # симулируем время явно: окна стадий двигаются по now
    t = 1_800_000_000.0

    # одна ночь — всё ещё эпизод
    t += 3600
    e.after_sleep(quality=1.0, now=t)
    assert e.snapshot.stage == "episode", "одной ночи быть не должно хватить"

    # держим нагрузку ниже половины двое суток — всё ещё эпизод
    for _ in range(2):
        t += 86400.0
        e.after_sleep(quality=1.0, now=t)
        e.tick(86400.0, now=t, rumination_charge=0.0)
    assert e.snapshot.stage == "episode", (
        f"двух суток быть не должно хватить (load={e.snapshot.load:.3f})")

    # третьи сутки — выходим
    t += 86400.0
    e.tick(86400.0, now=t, rumination_charge=0.0)
    assert e.snapshot.stage != "episode", (
        f"за трое суток должны выйти (load={e.snapshot.load:.3f})")


def test_floor_guard_releases_after_week():
    """Держать load выше 0.85 неделю нельзя — принудительный сброс."""
    e = _engine()
    for _ in range(400):
        e.observe(i_loss=1.0, pain=1.0, coping=0.0)
    assert e.snapshot.load > 0.85

    # проматываем неделю явным временем: предохранитель смотрит на now
    t = 1_800_000_000.0
    e.tick(8 * 86400.0, now=t, isolated=True, rumination_charge=0.9)
    assert e.snapshot.load <= FLOOR_GUARD_DROP_TO + 0.01, (
        f"предохранитель дна не сработал, load={e.snapshot.load:.3f}")


# ------------------------------------------------------------- множители

def test_anhedonia_drops_in_episode():
    snap = MentalSnapshot(load=0.8, stage="episode")
    assert snap.anhedonia_mod == pytest.approx(0.40)


def test_anhedonia_partial_in_strain():
    snap = MentalSnapshot(load=0.5, stage="strain")
    assert 0.5 <= snap.anhedonia_mod < 1.0


def test_coping_mod_falls_with_erosion():
    snap = MentalSnapshot(agency_erosion=1.0)
    assert snap.coping_mod == pytest.approx(0.5)


# ------------------------------------------------------------------- сон

def test_good_sleep_releases_35_percent_of_load():
    snap = MentalSnapshot(load=0.60)
    e = _engine()
    e._state = snap
    after = e.after_sleep(quality=1.0)
    assert abs(after.load - 0.39) < 0.01


def test_sleep_almost_does_not_fix_agency():
    """Контроль восстанавливается опытом, не отдыхом — только 10%."""
    e = _engine()
    e._state = MentalSnapshot(load=0.5, agency_erosion=0.60)
    after = e.after_sleep(quality=1.0)
    assert after.agency_erosion > 0.50, "сон не должен лечить беспомощность"


def test_bad_sleep_helps_little():
    e = _engine()
    e._state = MentalSnapshot(load=0.60)
    good = e.after_sleep(quality=1.0).load
    e._state = MentalSnapshot(load=0.60)
    bad = e.after_sleep(quality=0.35).load
    assert bad > good, "плохой сон снимает меньше"


def test_sleep_debt_has_ceiling():
    """Долг сна не уходит в бесконечность — исправлена дыра архитектуры."""
    e = _engine()
    e.tick(30 * 86400.0, now=3_000_000.0, deprived=True)
    assert e.snapshot.sleep_debt <= 1.0


def test_deprivation_kills_empathy_over_three_days():
    e = _engine()
    t = 1_800_000_000.0
    for _ in range(3):
        t += 86400.0
        e.tick(86400.0, now=t, deprived=True)
    assert 0.45 < e.snapshot.empathy_mod < 0.60, "≈0.51 за трое суток"


def test_awakening_lowers_empathy():
    e = _engine()
    before = e.snapshot.empathy_mod
    e.register_awakening()
    assert e.snapshot.empathy_mod < before


# --------------------------------------------------------------- директива

def test_directive_empty_when_stable():
    assert mental_directive(MentalSnapshot()) == ""


def test_directive_has_no_diagnosis_words():
    """Главный риск — карикатура. Клинических слов быть не должно."""
    snap = MentalSnapshot(load=0.8, stage="episode", agency_erosion=0.7,
                          depletion=0.7, empathy_mod=0.5)
    text = mental_directive(snap).lower()
    for word in ("депрессия", "ангедония", "выгорание", "беспомощность",
                 "психическ", "расстройств", "клиническ"):
        assert word not in text, f"в промпт попало диагностическое слово: {word}"


def test_directive_limits_capability_not_self_report():
    """Состояние ограничивает способность, а не описывает себя."""
    snap = MentalSnapshot(load=0.8, stage="episode")
    text = mental_directive(snap)
    assert "не задавай встречных вопросов" in text.lower()
    assert "короткие фразы" in text.lower()


# ------------------------------------------------------------ персистентность

def test_state_survives_reload():
    db = sqlite3.connect(":memory:")
    e1 = MentalEngine(db)
    e1.observe(i_loss=0.6, pain=0.5, coping=0.2)
    load = e1.snapshot.load

    e2 = MentalEngine(db)
    assert abs(e2.snapshot.load - load) < 0.0001


def test_snapshot_to_dict_is_plain():
    d = MentalSnapshot(load=0.5, stage="strain").to_dict()
    assert d["stage"] == "strain"
    assert 0.0 <= d["load"] <= 1.0
    assert "anhedonia_mod" in d and "coping_mod" in d
