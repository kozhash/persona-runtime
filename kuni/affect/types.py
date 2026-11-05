"""Типы аффективной системы: моноамины, PAD, гомеостаз.

Чистые структуры без зависимостей — их можно тестировать без БД, сети
и LLM. Границы значений заданы так, как в спеке NEURO-AFFECT:

  * моноамины S/N/D  : [0, 1]
  * PAD (P/A/Dom)    : [-1, 1]
  * гомеостаз        : [0, 1]

Имена полей короткие намеренно: они попадают в формулы и в SQL.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# Классификация происхождения: что именно мы записали.
# Это защита от «ИИ-психоза» — собственная интерпретация не должна
# становиться фактом наравне с наблюдением.
ProvenanceClass = Literal["OBSERVATION", "TEACHING", "SELF_INFERENCE"]

OBSERVATION: ProvenanceClass = "OBSERVATION"
TEACHING: ProvenanceClass = "TEACHING"
SELF_INFERENCE: ProvenanceClass = "SELF_INFERENCE"


def clamp(value: float, lo: float, hi: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return lo
    return max(lo, min(hi, v))


@dataclass(frozen=True)
class Monoamines:
    """Три нейромодулятора: серотонин, норадреналин, дофамин."""

    s: float = 0.5
    n: float = 0.5
    d: float = 0.5

    def clamped(self) -> "Monoamines":
        return Monoamines(clamp(self.s, 0.0, 1.0),
                          clamp(self.n, 0.0, 1.0),
                          clamp(self.d, 0.0, 1.0))

    def as_tuple(self) -> tuple[float, float, float]:
        return (self.s, self.n, self.d)

    def to_dict(self) -> dict[str, float]:
        return {"s": round(self.s, 4), "n": round(self.n, 4), "d": round(self.d, 4)}


@dataclass(frozen=True)
class PAD:
    """Pleasure–Arousal–Dominance. Проекция моноаминов в переживание."""

    p: float = 0.0
    a: float = 0.0
    dom: float = 0.0

    def clamped(self) -> "PAD":
        return PAD(clamp(self.p, -1.0, 1.0),
                   clamp(self.a, -1.0, 1.0),
                   clamp(self.dom, -1.0, 1.0))

    def as_tuple(self) -> tuple[float, float, float]:
        return (self.p, self.a, self.dom)

    def distance(self, other: "PAD") -> float:
        """Евклидово расстояние — основа mood-congruent поиска."""
        return ((self.p - other.p) ** 2
                + (self.a - other.a) ** 2
                + (self.dom - other.dom) ** 2) ** 0.5

    def to_dict(self) -> dict[str, float]:
        return {"p": round(self.p, 4), "a": round(self.a, 4),
                "dom": round(self.dom, 4)}


@dataclass(frozen=True)
class Homeostasis:
    """Внутренние ресурсы: усталость, когнитивная нагрузка, насыщение общением."""

    fatigue: float = 0.0
    cognitive_load: float = 0.0
    social_satiety: float = 0.0

    def clamped(self) -> "Homeostasis":
        return Homeostasis(clamp(self.fatigue, 0.0, 1.0),
                           clamp(self.cognitive_load, 0.0, 1.0),
                           clamp(self.social_satiety, 0.0, 1.0))

    def as_tuple(self) -> tuple[float, float, float]:
        return (self.fatigue, self.cognitive_load, self.social_satiety)

    def to_dict(self) -> dict[str, float]:
        return {"fatigue": round(self.fatigue, 4),
                "cognitive_load": round(self.cognitive_load, 4),
                "social_satiety": round(self.social_satiety, 4)}


@dataclass(frozen=True)
class AffectSnapshot:
    """Полное состояние в один момент времени."""

    ts: float = 0.0
    mono: Monoamines = field(default_factory=Monoamines)
    pad: PAD = field(default_factory=PAD)
    homeostasis: Homeostasis = field(default_factory=Homeostasis)
    counterweight_active: bool = False
    circuit_breaker_active: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "mono": self.mono.to_dict(),
            "pad": self.pad.to_dict(),
            "homeostasis": self.homeostasis.to_dict(),
            "counterweight_active": self.counterweight_active,
            "circuit_breaker_active": self.circuit_breaker_active,
        }

    @staticmethod
    def neutral(ts: float = 0.0) -> "AffectSnapshot":
        """Нейтральное состояние — значение по умолчанию для старых вызовов."""
        return AffectSnapshot(ts=ts)


@dataclass(frozen=True)
class AppraisalImpulse:
    """Досознательная оценка входящего стимула.

    u(t) = [ΔV, ΔA_threat, RPE, Surprise] — вход для ODE.
    """

    ts: float
    chat_id: int | None = None
    user_id: int | None = None
    delta_valence: float = 0.0
    threat: float = 0.0
    rpe: float = 0.0
    surprise: float = 0.0
    w_rel: float = 1.0
    latency_ms: float = 0.0
    source: str = "message"

    def vector_u(self) -> tuple[float, float, float, float]:
        """Вектор входа с масштабированием привязанностью.

        Валентность и RPE умножаются на w_rel: похвала от близкого
        человека трогает сильнее, чем от случайного.
        """
        return (
            clamp(self.w_rel * self.delta_valence, -3.0, 3.0),
            clamp(self.threat, 0.0, 1.0),
            clamp(self.w_rel * self.rpe, -3.0, 3.0),
            clamp(self.surprise, 0.0, 1.0),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts, "chat_id": self.chat_id, "user_id": self.user_id,
            "delta_valence": round(self.delta_valence, 4),
            "threat": round(self.threat, 4),
            "rpe": round(self.rpe, 4),
            "surprise": round(self.surprise, 4),
            "w_rel": round(self.w_rel, 3),
            "latency_ms": round(self.latency_ms, 2),
            "source": self.source,
        }
