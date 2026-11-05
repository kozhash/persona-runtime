"""Кривые затухания памяти.

Разные типы знания стареют с разной скоростью (type-conditioned decay,
ScrubJay-MEM): «где лежат ключи» протухает за часы, «где живёт человек» —
за годы. Поэтому единой экспоненты быть не должно.

Реализованы:
  * степенной закон базовой активации ACT-R  B_i = ln(sum t_k^-d) + K_m
  * экспонента Эббингауза                    R = exp(-t/S)
  * распределение Вейбулла                    w = exp(-(t/lambda)^k)
  * ретеншн-приближение FSRS                 (retrievability по stability)

Все функции чистые: их легко тестировать без БД и сети.
"""
from __future__ import annotations

import math
from typing import Iterable

# Скорость забывания по типу знания: во сколько раз медленнее базового.
# EVENT — эпизод, живёт долго; FACT — семантический факт, живёт дольше всех;
# THOUGHT — рефлексия, быстро теряет актуальность; ENTITY — черты людей/мест.
TYPE_PERISHABILITY: dict[str, float] = {
    "EVENT": 1.0,
    "FACT": 3.0,
    "ENTITY_DESCRIPTION": 4.0,
    "THOUGHT": 0.5,
    "OTHER": 1.0,
}

# Базовая полужизнь в часах для типа
TYPE_HALF_LIFE_HOURS: dict[str, float] = {
    "EVENT": 72.0,
    "FACT": 24.0 * 30,
    "ENTITY_DESCRIPTION": 24.0 * 60,
    "THOUGHT": 12.0,
    "OTHER": 48.0,
}


def actr_base_activation(access_times: Iterable[float], now: float,
                         decay: float = 0.5, km: float = 0.0) -> float:
    """Базовая активация чанка по ACT-R: частота + недавность обращений.

    B_i = ln( sum_k (now - t_k)^-d ) + K_m

    Чем чаще и свежее обращались — тем выше активация. Пустой список
    обращений даёт -inf (чанк никогда не использовался).
    """
    total = 0.0
    for ts in access_times:
        age = max(1.0, now - float(ts))          # секунды, не ноль
        total += age ** (-decay)
    if total <= 0:
        return float("-inf")
    return math.log(total) + km


def ebbinghaus(age_seconds: float, stability_hours: float = 24.0) -> float:
    """Классическая экспонента: R = exp(-t / S). 0..1."""
    if stability_hours <= 0:
        return 0.0
    return math.exp(-max(0.0, age_seconds) / (stability_hours * 3600.0))


def weibull(age_seconds: float, scale_hours: float = 72.0, shape: float = 1.2) -> float:
    """Вейбулл: w = exp(-(t/lambda)^k). shape<1 — быстрый обвал, >1 — плато."""
    if scale_hours <= 0:
        return 0.0
    ratio = max(0.0, age_seconds) / (scale_hours * 3600.0)
    return math.exp(-(ratio ** max(0.05, shape)))


def fsrs_retrievability(age_days: float, stability_days: float) -> float:
    """Приближение FSRS: R = (1 + t / (9S))^-1. 0..1.

    Девятка — эмпирический коэффициент кривой забывания FSRS.
    """
    if stability_days <= 0:
        return 0.0
    return (1.0 + max(0.0, age_days) / (9.0 * stability_days)) ** -1.0


def type_decay(age_seconds: float, kind: str,
               half_life_hours: float | None = None) -> float:
    """Затухание с учётом типа знания. Возвращает вес 0..1."""
    base = half_life_hours or TYPE_HALF_LIFE_HOURS.get(kind, 48.0)
    scale = base * TYPE_PERISHABILITY.get(kind, 1.0)
    # Вейбулл: для фактов плато дольше (shape>1), для мыслей — обвал (shape<1)
    shape = 1.4 if kind in ("FACT", "ENTITY_DESCRIPTION") else 0.9
    return weibull(age_seconds, scale_hours=scale, shape=shape)


def retention(age_seconds: float, kind: str, access_times: Iterable[float],
              now: float) -> float:
    """Итоговая «доступность» следа: затухание, усиленное практикой.

    Практика (повторные обращения) поднимает стабильность: чем больше
    обращений — тем медленнее забывание. Это эффект распределённого
    повторения (spacing effect), которого нет у чистой экспоненты.
    """
    accesses = list(access_times)
    stability_boost = 1.0 + math.log1p(len(accesses))
    base = type_decay(age_seconds, kind)
    if not math.isfinite(base):
        return 0.0
    # усиление ограничено единицей: практика не делает след «свежим»
    return min(1.0, base * stability_boost ** 0.5)


def score_for_admission(importance: float, surprise: float, emotion: float,
                        novelty: float) -> float:
    """Композитный балл «стоит ли запоминать» (CraniMem-подобный).

    Importance + Surprise + Emotion — ядро; novelty добавляет вес новому,
    чтобы не цементировать повторы.
    """
    return (
        0.40 * _clamp01(importance)
        + 0.25 * _clamp01(surprise)
        + 0.20 * _clamp01(emotion)
        + 0.15 * _clamp01(novelty)
    )


def _clamp01(value: float) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0
