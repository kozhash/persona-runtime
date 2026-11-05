"""Тесты влияния состояния на генерацию (шаг 4).

Проверяем главный принцип из 06-mental/REPORT.md: состояние меняет
**способность**, а не самоописание. Пафос не включается, когда ресурса
нет; длина ответа режется; клинических слов в промпте нет.
"""
import pytest

from kuni.affect.mental import MentalSnapshot
from kuni.affect.modulation import InferenceModulator
from kuni.affect.sleep import SleepManager
from kuni.affect.types import AffectSnapshot, Homeostasis, Monoamines, PAD
from kuni.character.emotions import EmotionState


def _affect(mono: Monoamines | None = None,
            fatigue: float = 0.0) -> AffectSnapshot:
    return AffectSnapshot(
        mono=mono or Monoamines(0.62, 0.38, 0.60),
        pad=PAD(0.0, 0.0, 0.0),
        homeostasis=Homeostasis(fatigue=fatigue),
        ts=0.0,
    )


# ------------------------------------------------------- длина ответа

def test_episode_cuts_max_tokens():
    """В эпизоде ответы заметно короче."""
    mod = InferenceModulator()
    normal = mod.max_tokens(_affect(),
                            mental=MentalSnapshot(stage="stable"), )
    episode = mod.max_tokens(_affect(),
                             mental=MentalSnapshot(stage="episode"))
    assert episode < normal
    assert episode / normal < 0.75


def test_strain_cuts_less_than_episode():
    mod = InferenceModulator()
    strain = mod.max_tokens(_affect(), mental=MentalSnapshot(stage="strain"))
    episode = mod.max_tokens(_affect(), mental=MentalSnapshot(stage="episode"))
    assert strain > episode


def test_depletion_cuts_further():
    mod = InferenceModulator()
    tired = mod.max_tokens(_affect(), mental=MentalSnapshot(depletion=0.8))
    rested = mod.max_tokens(_affect(), mental=MentalSnapshot(depletion=0.0))
    assert tired < rested


def test_sleep_phase_cuts_length():
    mod = InferenceModulator()
    awake = mod.max_tokens(_affect(), sleep_phase="awake")
    dozing = mod.max_tokens(_affect(), sleep_phase="wind_down")
    assert dozing < awake


def test_max_tokens_never_below_floor():
    """Пол 400 не пробивается — иначе reasoning-модель вернёт пустоту."""
    mod = InferenceModulator()
    value = mod.max_tokens(
        _affect(fatigue=1.0),
        mental=MentalSnapshot(stage="episode", depletion=1.0),
        sleep_phase="wind_down")
    assert value >= mod.cfg.max_tokens_min


# ---------------------------------------------------------- температура

def test_episode_lowers_temperature():
    """Психомоторная заторможенность: генерация ровнее."""
    mod = InferenceModulator()
    normal = mod.temperature(_affect(), mental=MentalSnapshot(stage="stable"))
    flat = mod.temperature(_affect(), mental=MentalSnapshot(stage="episode"))
    assert flat < normal


def test_fragmented_wake_raises_temperature():
    """Спросонья — резче и менее предсказуемо."""
    mod = InferenceModulator()
    calm = mod.temperature(_affect(), sleep_phase="awake")
    groggy = mod.temperature(_affect(), sleep_phase="fragmented_wake")
    assert groggy > calm


def test_sampling_params_carry_state():
    mod = InferenceModulator()
    plain = mod.sampling_params(_affect())
    heavy = mod.sampling_params(
        _affect(), mental=MentalSnapshot(stage="episode"),
        sleep_phase="wind_down")
    assert heavy["max_tokens"] < plain["max_tokens"]
    assert heavy["temperature"] < plain["temperature"]


# ------------------------------------------------------------- пафос

def test_pathos_off_in_wind_down():
    st = EmotionState(emotion="гордость", intensity=0.9, valence=0.4,
                      arousal=0.6, dominance=0.8)
    text = st.style_directive(sleep_phase="wind_down")
    assert "пафос ВЫКЛЮЧЕН" in text or "ВЫКЛЮЧЕН" in text


def test_pathos_off_in_wake_inertia():
    st = EmotionState(emotion="гордость", intensity=0.9, valence=0.4,
                      arousal=0.6, dominance=0.8)
    text = st.style_directive(sleep_phase="wake_inertia")
    assert "сонная инерция" in text.lower() or "пафос ещё не проснулся" in text.lower()


def test_pathos_off_in_episode_even_when_proud():
    """Даже удар по гордости не даёт тираду — сил нет."""
    st = EmotionState(emotion="возмущение", intensity=0.9, valence=-0.5,
                      arousal=0.7, dominance=0.7)
    text = st.style_directive(mental=MentalSnapshot(stage="episode"))
    assert "не включается" in text.lower()
    assert "тирад" not in text.lower() or "нет сил" in text.lower()


def test_depletion_makes_her_formal():
    st = EmotionState(emotion="спокойствие", intensity=0.3)
    text = st.style_directive(mental=MentalSnapshot(depletion=0.8))
    assert "без встречных вопросов" in text.lower()


def test_normal_state_keeps_her_voice():
    st = EmotionState(emotion="гордость", intensity=0.6, valence=0.3,
                      arousal=0.5, dominance=0.7)
    text = st.style_directive(mental=MentalSnapshot(stage="stable"),
                              sleep_phase="awake")
    assert "величие" in text.lower() or "тирад" in text.lower()


# ------------------------------------------------------- весь промпт

def test_describe_includes_state():
    st = EmotionState(emotion="обида", intensity=0.6, valence=-0.4,
                      arousal=0.5, dominance=0.3)
    text = st.describe(mental=MentalSnapshot(stage="episode"),
                       sleep_phase="awake")
    assert "CURRENT EMOTION" in text
    assert "истощение" in text.lower() or "пафос" in text.lower()


def test_clinical_words_never_reach_prompt():
    """Главная защита от карикатуры — проверяем насквозь."""
    st = EmotionState(emotion="обида", intensity=0.9, valence=-0.6,
                      arousal=0.8, dominance=0.2)
    mental = MentalSnapshot(load=0.8, stage="episode", agency_erosion=0.7,
                            depletion=0.75, empathy_mod=0.5)
    text = st.describe(mental=mental, sleep_phase="wind_down").lower()
    for word in ("депрессия", "ангедония", "выгорание", "беспомощность",
                 "психическ", "расстройств"):
        assert word not in text, f"клиническое слово в промпте: {word}"
