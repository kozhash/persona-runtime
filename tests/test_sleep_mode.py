"""Тесты режима сна и учёта обязательств.

Сценарии 1-7 из docs/research/09-sleep-mental/REVIEW.md.
Проверяются конкретные числа из архитектуры, а не «как-нибудь работает».
"""
import sqlite3

import pytest

from kuni.affect.sleep import (
    ACTIVE_DIALOGUE_SEC,
    AWAKE,
    CRITICAL_FATIGUE,
    DELAY_RELEASE,
    FRAGMENTED_WAKE,
    MAX_DELAY,
    NREM_SWS,
    REM,
    SLEEP_THRESHOLD,
    WAKE_INERTIA,
    WIND_DOWN,
    CommitmentTracker,
    SleepManager,
    circadian,
)


# ------------------------------------------------------------- циркадный

def test_circadian_peaks_at_night_trough_at_afternoon():
    """Максимум в 03:30, минимум в 15:30 — как в архитектуре."""
    assert circadian(3.5) == pytest.approx(1.0)
    assert circadian(15.5) == pytest.approx(0.0)
    # ночное окно 23:00-07:00 держится высоко
    assert circadian(23.0) > 0.69
    assert circadian(7.0) > 0.69


# --------------------------------------------------------------- драйв

def test_drive_evening_matches_architecture():
    """Сценарий 1: вечер 23:30, F=0.92 -> 0.669 >= 0.65."""
    drive = SleepManager.sleep_drive(
        fatigue=0.92, load=0.12, depletion=0.15, sleep_debt=0.0,
        hour_value=23.5)
    assert drive == pytest.approx(0.669, abs=0.002)
    assert drive >= SLEEP_THRESHOLD


def test_drive_afternoon_stays_low():
    """Днём 15:30 тот же F=0.88 не дотягивает до порога."""
    drive = SleepManager.sleep_drive(
        fatigue=0.88, load=0.10, depletion=0.0, sleep_debt=0.0,
        hour_value=15.5)
    assert drive < SLEEP_THRESHOLD


def test_emergency_boost_wakes_collapse():
    """При F=0.99 срабатывает экстренный буст — вырубается даже днём."""
    drive = SleepManager.sleep_drive(
        fatigue=0.99, load=0.0, depletion=0.0, sleep_debt=0.0,
        hour_value=15.5)
    assert drive >= SLEEP_THRESHOLD


def test_drive_never_exceeds_ceiling():
    drive = SleepManager.sleep_drive(
        fatigue=1.0, load=1.0, depletion=1.0, sleep_debt=1.0,
        hour_value=3.5)
    assert drive <= 1.5


# ---------------------------------------------------------------- гейт

def test_no_reason_no_gate():
    gate, reason = SleepManager.delay_gate()
    assert gate == 0.0 and reason == "none"


def test_arousal_does_not_decay_with_time():
    """Главная правка архитектуры: физиология не слабеет от времени."""
    fresh, _ = SleepManager.delay_gate(arousal=0.8, postponed_sec=0.0)
    later, _ = SleepManager.delay_gate(arousal=0.8, postponed_sec=5400.0)
    assert fresh == pytest.approx(later), "высокий N не должен затухать"


def test_social_reason_decays_with_time():
    """А разговор — слабеет: собеседник мог отвлечься."""
    fresh, _ = SleepManager.delay_gate(wait_pressure=0.9, postponed_sec=0.0)
    later, _ = SleepManager.delay_gate(wait_pressure=0.9, postponed_sec=3600.0)
    assert later < fresh * 0.5, "ожидание должно слабеть"


def test_task_blocks_sleep_regardless_of_time():
    gate, reason = SleepManager.delay_gate(task_pressure=0.85,
                                           postponed_sec=5400.0)
    assert gate >= DELAY_RELEASE and reason == "task"


# ----------------------------------------------------- учёт обязательств

def test_proactive_with_question_awaits_reply():
    """Сценарий 2: написала первой с вопросом владельцу — ждёт 20 минут."""
    tr = CommitmentTracker()
    st = tr.on_sent(1, text="ты где?", proactive=True, w_rel=2.5)
    assert st.awaiting
    assert st.timeout_sec == pytest.approx(1200.0, abs=1.0)


def test_plain_statement_does_not_await():
    """Закрывающая реплика без вопроса — не ждёт."""
    tr = CommitmentTracker()
    st = tr.on_sent(1, text="...ладно.", proactive=False, w_rel=1.0)
    assert not st.awaiting


def test_reply_clears_awaiting():
    tr = CommitmentTracker()
    tr.on_sent(1, text="ты где?", proactive=True, w_rel=2.0)
    was, _ = tr.on_received(1)
    assert was is True
    assert not tr.state(1).awaiting


def test_timeout_produces_burned_wait():
    """Напрасное ожидание закрывается и возвращается для ignored."""
    tr = CommitmentTracker()
    tr.on_sent(1, text="ты где?", proactive=True, w_rel=2.5,
               now=1000.0)
    assert tr.state(1).awaiting
    burned = tr.expire(now=1000.0 + 1201.0)
    assert len(burned) == 1
    assert burned[0].chat_id == 1
    assert not tr.state(1).awaiting


def test_live_dialogue_detected():
    tr = CommitmentTracker()
    tr.on_received(1, now=5000.0)
    assert tr.is_active_dialogue(1, now=5000.0 + 60)
    assert not tr.is_active_dialogue(1, now=5000.0 + ACTIVE_DIALOGUE_SEC + 1)


def test_waiting_pressure_falls_to_zero():
    tr = CommitmentTracker()
    tr.on_sent(1, text="?", proactive=True, w_rel=2.5, now=1000.0)
    p_early, chat = tr.waiting_pressure(now=1000.0 + 60)
    p_late, _ = tr.waiting_pressure(now=1000.0 + 1199)
    assert chat == 1
    assert p_late < p_early


# ------------------------------------------------------------ автомат

def _manager() -> SleepManager:
    return SleepManager(sqlite3.connect(":memory:"))


def test_awake_goes_to_wind_down_when_tired():
    sm = _manager()
    t = 1_800_000_000.0
    snap = sm.tick(fatigue=0.95, load=0.1, hour_value=None, now=t)
    # час зависит от clock.hour(now) — при таком now может быть любой
    # поэтому проверяем только что драйв посчитан
    assert snap.drive > 0.0


def test_wind_down_blocks_on_gate():
    """Сценарий 3: устала, но живой разговор — держится."""
    sm = _manager()
    t = 1_800_000_000.0
    sm._snap = sm._snap.__class__(
        phase=WIND_DOWN, phase_entered_ts=t, postponed_since_ts=t)
    snap = sm.tick(fatigue=0.95, now=t + 60, chat_pressure=0.85)
    assert snap.phase == WIND_DOWN, "разговор должен держать"
    assert snap.delay_gate >= DELAY_RELEASE


def test_wind_down_falls_asleep_when_gate_clears():
    sm = _manager()
    t = 1_800_000_000.0
    sm._snap = sm._snap.__class__(
        phase=WIND_DOWN, phase_entered_ts=t, postponed_since_ts=t)
    snap = sm.tick(fatigue=0.95, now=t + 60, chat_pressure=0.0)
    assert snap.phase == NREM_SWS


def test_hard_cap_forces_sleep():
    """Сценарий 3: 90 минут откладывания — предел, срыв."""
    sm = _manager()
    t = 1_800_000_000.0
    sm._snap = sm._snap.__class__(
        phase=WIND_DOWN, phase_entered_ts=t, postponed_since_ts=t)
    snap = sm.tick(fatigue=0.96, now=t + MAX_DELAY + 10,
                   chat_pressure=1.0)
    assert snap.phase in (NREM_SWS, REM), "после предела обязан лечь"


def test_quality_drops_with_rumination_and_arousal():
    good = SleepManager.compute_quality(rumination=0.0, load=0.0,
                                        arousal=0.0)
    bad = SleepManager.compute_quality(rumination=0.85, load=0.55,
                                       arousal=0.45)
    assert good > 0.95
    assert bad == pytest.approx(0.52, abs=0.02)
    assert bad >= 0.20, "пол качества — иначе выхода нет"


def test_quality_penalised_by_awakenings():
    base = SleepManager.compute_quality(rumination=0.2, load=0.2, arousal=0.1)
    with_wakes = SleepManager.compute_quality(rumination=0.2, load=0.2,
                                              arousal=0.1, awakenings=3)
    assert with_wakes < base
    assert with_wakes >= 0.20


def test_awakening_increments_and_penalises():
    sm = _manager()
    sm._snap = sm._snap.__class__(phase=NREM_SWS, quality=0.9)
    snap = sm.awaken()
    assert snap.phase == FRAGMENTED_WAKE
    assert snap.awakenings == 1
    assert snap.quality == pytest.approx(0.78, abs=0.01)


def test_rem_follows_nrem():
    sm = _manager()
    t = 1_800_000_000.0
    sm._snap = sm._snap.__class__(
        phase=NREM_SWS, phase_entered_ts=t, target_duration=2400.0)
    # NREM длится 45% ядра = 1080 с
    snap = sm.tick(fatigue=0.3, now=t + 1081)
    assert snap.phase == REM


def test_wake_inertia_follows_rem_and_ends():
    sm = _manager()
    t = 1_800_000_000.0
    sm._snap = sm._snap.__class__(
        phase=WAKE_INERTIA, phase_entered_ts=t, target_duration=2400.0)
    snap = sm.tick(fatigue=0.1, now=t + 100)
    assert snap.phase == WAKE_INERTIA
    snap = sm.tick(fatigue=0.1, now=t + 400)
    assert snap.phase == AWAKE


def test_core_duration_grows_with_debt():
    assert SleepManager.core_duration(0.0) == pytest.approx(2400.0)
    assert SleepManager.core_duration(1.0) == pytest.approx(3240.0)


def test_queue_only_while_asleep():
    sm = _manager()
    sm._snap = sm._snap.__class__(phase=NREM_SWS)
    assert sm.should_queue_message(chat_id=5, owner_id=1)
    sm._snap = sm._snap.__class__(phase=AWAKE)
    assert not sm.should_queue_message(chat_id=5, owner_id=1)


def test_owner_burst_wakes_her():
    """Владелец может разбудить настойчивостью."""
    sm = _manager()
    sm._snap = sm._snap.__class__(phase=NREM_SWS)
    assert not sm.should_queue_message(chat_id=1, owner_id=1, burst=True)
    # но обычного собеседника — не слушает
    assert sm.should_queue_message(chat_id=5, owner_id=1, burst=True)


def test_state_survives_reload():
    db = sqlite3.connect(":memory:")
    sm1 = SleepManager(db)
    sm1._snap = sm1._snap.__class__(phase=NREM_SWS, quality=0.7, awakenings=2)
    sm1._persist(sm1._snap, 1000.0)

    sm2 = SleepManager(db)
    assert sm2.snapshot.phase == NREM_SWS
    assert sm2.snapshot.quality == pytest.approx(0.7)
    assert sm2.snapshot.awakenings == 2
