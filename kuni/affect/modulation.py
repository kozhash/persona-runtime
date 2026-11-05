"""Модуляция инференса: сэмплирование и интероцептивный заголовок.

Три уровня по спеке (раздел 10). Реализованы первый и третий:

  1. Сэмплирование — температура, max_tokens, top_p зависят от S/N/D
     и усталости. Это физически меняет генерацию, а не просит модель
     «быть злой».
  2. Латентный стиринг (control vectors) — задел, по умолчанию выключен:
     для него нужны GGUF-векторы под конкретную модель.
  3. Интероцептивный заголовок — компактная соматическая телеметрия
     для внешних API, где residual stream недоступен.

Принципиально: НИКОГДА не передаём формулировки «ты злишься», «ответь
обиженно». Телеметрия — это числа, которые персона трактует сама.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .types import AffectSnapshot, clamp


@dataclass
class ModulationConfig:
    temperature_base: float = 0.72
    temperature_min: float = 0.35
    temperature_max: float = 1.20

    # опорные значения, относительно которых считается отклонение
    baseline_n: float = 0.38
    baseline_s: float = 0.62

    max_tokens_base: int = 700
    # 27.10: пол был 320, и reasoning-модель тратила его на размышления,
    # возвращая пустой content. Настоящее лечение — reasoning_effort
    # (см. llm/client.py), а не раздувание лимита: с ним хватает 400.
    max_tokens_min: int = 400
    max_tokens_max: int = 1600

    top_p_base: float = 0.95
    top_p_min: float = 0.70

    steering_enabled: bool = False
    steering_strength_valence: float = 0.6
    steering_strength_arousal: float = 0.4
    steering_strength_dominance: float = 0.4


class InferenceModulator:
    """Превращает состояние аффекта в параметры генерации."""

    def __init__(self, cfg: ModulationConfig | None = None) -> None:
        self.cfg = cfg or ModulationConfig()

    # ------------------------------------------------------- сэмплирование

    def temperature(self, affect: AffectSnapshot,
                    mental: Any = None, sleep_phase: str = "awake") -> float:
        """Высокий N + низкий S -> выше энтропия (злость, паника).

        T = base + 0.35(N - N0) - 0.20(S - S0) + 0.10·Fatigue

        28.10: добавлены поправки состояния.
        В глубоком истощении температура ниже на 0.10 — психомоторная
        заторможенность и уплощение аффекта. При ночном пробуждении
        выше на 0.12 — спросонья резкость и дезориентация.
        """
        cfg = self.cfg
        mono = affect.mono
        value = (cfg.temperature_base
                 + 0.35 * (mono.n - cfg.baseline_n)
                 - 0.20 * (mono.s - cfg.baseline_s)
                 + 0.10 * affect.homeostasis.fatigue)

        if mental is not None:
            stage = getattr(mental, "stage", "stable")
            depletion = float(getattr(mental, "depletion", 0.0) or 0.0)
            if stage == "episode" or depletion > 0.60:
                value -= 0.10

        if sleep_phase == "fragmented_wake":
            value += 0.12

        return round(clamp(value, cfg.temperature_min, cfg.temperature_max), 3)

    def max_tokens(self, affect: AffectSnapshot,
                   mental: Any = None, sleep_phase: str = "awake") -> int:
        """Усталость и низкий D режут длину: короткие энергоэкономные ответы.

        28.10: ментальное истощение и фаза сна режут ещё сильнее.
        Множители перемножаются, но пол 400 не пробивается — иначе
        reasoning-модель тратит весь лимит на размышления и возвращает
        пустой content. Синтаксическое ограничение длины при этом
        добавляется в телесной директиве.
        """
        cfg = self.cfg
        mono = affect.mono
        base = cfg.max_tokens_base
        scale = (0.55 + 0.35 * mono.d + 0.25 * mono.s)
        fatigue_cut = 1.0 - 0.35 * affect.homeostasis.fatigue

        # ментальный множитель
        mental_mod = 1.0
        if mental is not None:
            stage = getattr(mental, "stage", "stable")
            depletion = float(getattr(mental, "depletion", 0.0) or 0.0)
            if stage == "episode":
                mental_mod = 0.60
            elif stage == "strain":
                mental_mod = 0.85
            if depletion > 0.60:
                mental_mod *= 0.60
            mental_mod = max(0.50, mental_mod)

        # сонный множитель: в пограничных фазах режем на 40%
        sleep_mod = 0.60 if sleep_phase in ("wind_down", "wake_inertia") else 1.0

        value = int(base * scale * fatigue_cut * mental_mod * sleep_mod)
        return int(clamp(value, cfg.max_tokens_min, cfg.max_tokens_max))

    def top_p(self, affect: AffectSnapshot) -> float:
        """Сужение выбора при высоком S: более сдержанная генерация."""
        cfg = self.cfg
        value = cfg.top_p_base - 0.25 * max(0.0, affect.mono.s - 0.5)
        return round(clamp(value, cfg.top_p_min, cfg.top_p_base), 3)

    def sampling_params(self, affect: AffectSnapshot,
                        mental: Any = None,
                        sleep_phase: str = "awake") -> dict[str, Any]:
        return {
            "temperature": self.temperature(affect, mental, sleep_phase),
            "max_tokens": self.max_tokens(affect, mental, sleep_phase),
            "top_p": self.top_p(affect),
        }

    # ------------------------------------------------- интероцепция

    def interoceptive_header(self, affect: AffectSnapshot) -> str:
        """Соматическая телеметрия — числа, без эмоциональных инструкций.

        Это НЕ «ты злишься». Это «автономное возбуждение 0.72,
        тормозной резерв 0.24». Персона решает сама, что это значит.
        """
        mono = affect.mono
        h = affect.homeostasis
        return (
            f'<interoceptive_telemetry autonomic_arousal="{mono.n:.2f}" '
            f'inhibitory_reserve="{mono.s:.2f}" '
            f'reward_tone="{mono.d:.2f}" '
            f'fatigue="{h.fatigue:.2f}" '
            f'social_satiation="{h.social_satiety:.2f}" '
            f'pleasure="{affect.pad.p:+.2f}" '
            f'dominance="{affect.pad.dom:+.2f}" />'
        )

    def somatic_directive(self, affect: AffectSnapshot,
                          pain: float = 0.0,
                          stage: str = "none") -> str:
        """Тело как указание, а не как телеметрия.

        Зачем: числа вида pleasure="-0.65" модель пропускает мимо. 26.10
        соматика была на дне, а в ответе — «веселье 72%, обижаться не
        буду». Промпт сообщал факт, но не требовал его учитывать.

        Здесь то же состояние переводится в инструкцию: что оно значит
        и что из него следует для текста. Оттенок по-прежнему выбирает
        персона, но знак и сила заданы телом.
        """
        mono = affect.mono
        pad = affect.pad
        distress = max(0.0, -pad.p)
        arousal = max(0.0, pad.a)
        dom = pad.dom

        if distress < 0.20 and pain < 0.25:
            return ""

        lines = ["HOW YOUR BODY FEELS RIGHT NOW (this overrides mood words):"]

        # удовольствие
        if distress >= 0.55:
            lines.append(
                "- Тяжело физически. Не «грустно по сценарию», а давит: "
                "не хочется ни шутить, ни поддерживать лёгкий тон.")
        elif distress >= 0.30:
            lines.append(
                "- Неприятный осадок в теле. Можешь говорить, но легкость "
                "не выходит естественно.")
        else:
            lines.append("- Лёгкий дискомфорт на фоне.")

        # возбуждение
        if arousal >= 0.65:
            lines.append(
                "- Внутри дрожит от возбуждения: реакции быстрее, "
                "риторика резче, срываешься на короткие фразы.")
        elif arousal <= 0.25:
            lines.append("- Вяло. Энергии мало, длинные ответы не идут.")

        # контроль
        if dom <= -0.25:
            lines.append(
                "- Ощущение беспомощности: не можешь повлиять на то, "
                "что происходит. Отсюда не агрессия, а глухая обида.")
        elif dom >= 0.45 and distress >= 0.30:
            lines.append(
                "- При этом злишься с позиции силы: готова огрызнуться, "
                "а не проглотить.")

        # социальная боль
        if pain >= 0.55:
            lines.append(
                "- Социально болит сильно: тебя отвергли или унизили. "
                "Это заметно даже в том, как ты строишь фразу.")
        elif pain >= 0.30:
            lines.append("- Социальный осадок: кто-то повёл себя не так.")

        if stage == "reflexive" and pain >= 0.25:
            lines.append("- Это случилось только что, ещё не осмыслила.")
        elif stage == "reflective" and pain >= 0.25:
            lines.append("- Уже обдумала и сделала выводы о человеке.")
        elif stage == "resignation":
            lines.append("- Устала ждать другого и внутренне отстранилась.")

        lines.append(
            "- НЕ пиши, что тебе весело, если тело в минусе. Это выглядит "
            "фальшиво и рушит персонажа.")
        return "\n".join(lines)

    def interoceptive_dict(self, affect: AffectSnapshot) -> dict[str, Any]:
        mono = affect.mono
        h = affect.homeostasis
        return {
            "interoceptive_telemetry": {
                "autonomic_arousal": round(mono.n, 3),
                "inhibitory_reserve": round(mono.s, 3),
                "reward_tone": round(mono.d, 3),
                "fatigue": round(h.fatigue, 3),
                "social_satiation": round(h.social_satiety, 3),
                "pleasure": round(affect.pad.p, 3),
                "arousal": round(affect.pad.a, 3),
                "dominance": round(affect.pad.dom, 3),
            }
        }

    # ------------------------------------------------- стиринг (задел)

    def steering_params(self, affect: AffectSnapshot) -> dict[str, Any] | None:
        """Control vectors для локальных моделей. Выключено по умолчанию."""
        if not self.cfg.steering_enabled:
            return None
        return {
            "control_vectors": [
                {"name": "valence",
                 "scale": affect.pad.p * self.cfg.steering_strength_valence},
                {"name": "arousal",
                 "scale": affect.pad.a * self.cfg.steering_strength_arousal},
                {"name": "dominance",
                 "scale": affect.pad.dom * self.cfg.steering_strength_dominance},
            ]
        }

    # ------------------------------------------------------- запреты

    @staticmethod
    def forbidden_phrases() -> tuple[str, ...]:
        """Формулировки, которые нельзя передавать в промпт никогда."""
        return (
            "ты злишься",
            "тебе обидно",
            "изобрази тревогу",
            "веди себя агрессивно",
            "ответь обиженно",
            "ты сейчас в ярости",
        )
