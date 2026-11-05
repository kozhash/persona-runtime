"""Социальная боль и остракизм: четыре потребности, три стадии.

Почему отдельный модуль. В engine.py живёт непрерывная биохимия — S/N/D
и клиренс. Социальная боль — не биохимия, а когнитивный автомат: она
переключает стадии, накапливает дефицит потребностей и решает, чего
персонаж хочет после отвержения. Смешивать это с RK4-решателем значит
потерять оба.

Теория: темпоральная модель угрозы потребностям Уильямса (TNTM, 2007).
Отвержение бьёт по четырём потребностям — принадлежность, самооценка,
контроль, значимость существования — и проходит три стадии:

    reflexive   (0–40 мин)    острая боль, самозащита, ещё не думает
    reflective  (40 мин–12 ч) выбор: вернуться или отдалиться
    resignation (18–24 ч+)    апатия, ресурс копинга исчерпан

Ключевая деталь, которой не было: мут и бан — это НЕ то же самое, что
оскорбление. При оскорблении канал открыт (coping высокий) и человек
злится. При муте канал закрыт (coping → 0) и человек чувствует
беспомощность. Отсюда разные профили удара по потребностям.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field, replace
from typing import Any

from .appraisal import AppraisalVector, amplify

#: Гомеостатическая норма каждой потребности.
NEED_NORM = 0.75

#: Веса потребностей в интегральном индексе боли.
OMEGA = {"belong": 0.30, "esteem": 0.30, "control": 0.25, "meaning": 0.15}

#: Пассивное затухание дефицита: 14 часов до половины.
TAU_SOCIAL_SEC = 14 * 3600.0

#: Ночной сон снимает 45% остаточного дефицита.
SLEEP_RELIEF = 0.45

#: Границы стадий (секунды).
REFLEXIVE_MAX_SEC = 40 * 60.0
REFLECTIVE_MAX_SEC = 12 * 3600.0
RESIGNATION_AFTER_SEC = 20 * 3600.0

#: Сколько повторов отвержения от одного лица считаем хроническим.
CHRONIC_REPEATS = 3


@dataclass(frozen=True)
class SocialPainSnapshot:
    """Состояние социальных потребностей в конкретном чате."""
    chat_id: int = 0
    need_belong: float = NEED_NORM
    need_esteem: float = NEED_NORM
    need_control: float = NEED_NORM
    need_meaning: float = NEED_NORM
    pain_index: float = 0.0
    stage: str = "none"          # none | reflexive | reflective | resignation
    stage_entered_ts: float = 0.0
    unanswered_streak: int = 0
    is_muted: bool = False
    last_event: str = ""
    repeats: int = 0             # сколько раз отвергли от одного лица
    updated_ts: float = 0.0

    # ------------------------------------------------------------ вычисления

    def deficits(self) -> dict[str, float]:
        return {
            "belong": max(0.0, NEED_NORM - self.need_belong),
            "esteem": max(0.0, NEED_NORM - self.need_esteem),
            "control": max(0.0, NEED_NORM - self.need_control),
            "meaning": max(0.0, NEED_NORM - self.need_meaning),
        }

    def recalc_pain(self, w_rel: float = 1.0) -> "SocialPainSnapshot":
        """Интегральный индекс острой боли (аналог сигнала dACC)."""
        w = amplify(w_rel)
        total = sum(OMEGA[k] * v for k, v in self.deficits().items())
        pain = max(0.0, min(1.0, total * (0.7 + 0.3 * w)))
        return replace(self, pain_index=round(pain, 4))

    def cluster(self) -> str:
        """Какой кластер потребностей поражён сильнее.

        По Уильямсу от этого зависит стратегия: если болит
        принадлежность и самооценка — человек ищет возвращения;
        если контроль и значимость — уходит в холод.
        """
        d = self.deficits()
        belonging_cluster = d["belong"] + d["esteem"]
        power_cluster = d["control"] + d["meaning"]
        return "belonging" if belonging_cluster > power_cluster else "power"

    def to_dict(self) -> dict[str, Any]:
        return {
            "chat_id": self.chat_id,
            "pain_index": round(self.pain_index, 3),
            "stage": self.stage,
            "cluster": self.cluster(),
            "needs": {
                "belong": round(self.need_belong, 3),
                "esteem": round(self.need_esteem, 3),
                "control": round(self.need_control, 3),
                "meaning": round(self.need_meaning, 3),
            },
            "deficits": {k: round(v, 3) for k, v in self.deficits().items()},
            "unanswered_streak": self.unanswered_streak,
            "is_muted": self.is_muted,
            "last_event": self.last_event,
            "repeats": self.repeats,
        }


#: Профили удара по потребностям. Порядок: belong, esteem, control, meaning.
_PROFILES: dict[str, tuple[float, float, float, float]] = {
    "muted":   (-0.35, -0.40, -0.65, -0.50),
    "kicked":  (-0.65, -0.50, -0.70, -0.60),
    "banned":  (-0.65, -0.50, -0.70, -0.60),
    "ignored": (-0.25, -0.20, -0.30, -0.45),
    "insult":  (-0.30, -0.45, -0.15, -0.05),
    "humiliation": (-0.40, -0.65, -0.35, -0.20),
    "unmuted": (0.25, 0.10, 0.45, 0.20),
    "apology": (0.30, 0.35, 0.15, 0.10),
}


class SocialPainTracker:
    """Автомат социальной боли: события, стадии, затухание, исцеление."""

    def apply_event(
        self,
        state: SocialPainSnapshot,
        vec: AppraisalVector,
        event_type: str,
        *,
        witnesses: int = 0,
        now: float | None = None,
    ) -> SocialPainSnapshot:
        stamp = now if now is not None else time.time()
        w_full = amplify(vec.w_rel)
        # Для системных событий (мут, бан, исключение) вес привязанности
        # уже заложен в сами числа профиля. Умножать их ещё и на w_rel
        # значит обнулять все четыре потребности одним событием, чего
        # по Уильямсу не происходит: урон накапливается, а не уничтожает.
        w = 1.0 + 0.25 * (w_full - 1.0)

        key = event_type if event_type in _PROFILES else "insult"
        profile = _PROFILES[key]

        # Публичность усиливает удар по самооценке.
        pub = 1.0
        if witnesses >= 2:
            pub = 1.0 + 0.25 * math.log(1.0 + witnesses)
        # Кумуляция игнора: второй раз больнее, потом потолок.
        eta = 1.0
        if key == "ignored":
            streak = state.unanswered_streak + 1
            eta = min(1.5, 0.45 * max(0, streak - 1)) or 1.0

        # Релевантность обязательна: оскорбление постороннего в группе,
        # которое её не касается, не должно бить так же, как прямое
        # обращение. Раньше профиль брался целиком, и свидетель чужого
        # скандала получал боль 0.585 — почти как пострадавший.
        rel = max(0.0, min(1.0, vec.relevance))
        belong, esteem, control, meaning = (v * rel for v in profile)

        # Насыщение: чем ниже потребность, тем слабее следующий удар.
        # Без этого три оскорбления подряд обнуляли принадлежность и
        # самооценку в ноль (0.45 x 3), и она проваливалась в пустоту,
        # хотя человек так не устроен: пятый удар подряд бьёт слабее
        # первого, потому что бить уже некуда.
        def saturate(need: float) -> float:
            # выключатель нужен для честного сравнения вариантов:
            # без него нельзя понять, что именно дало эффект
            if not getattr(self, "saturation_enabled", True):
                return 1.0
            return max(0.15, min(1.0, need / NEED_NORM))

        d_belong = belong * eta * saturate(state.need_belong) * (
            w if key not in ("unmuted", "apology") else 1.0)
        d_esteem = esteem * eta * pub * saturate(state.need_esteem) * (
            w if key not in ("unmuted", "apology") else 1.0)
        d_control = control * eta * saturate(state.need_control)
        d_meaning = meaning * eta * w * saturate(state.need_meaning)

        # Технический мут обнуляет контроль целиком.
        if key == "muted":
            d_control = min(d_control, -0.45)
        if key in ("kicked", "banned"):
            d_control = min(d_control, -0.50)

        # Потолок урона за одно событие: дальше только накопление.
        # Один бан делает больно, но не превращает человека в пустоту
        # мгновенно — иначе теряется вся динамика стадий.
        def cap(value: float) -> float:
            return max(-0.45, min(0.45, value))

        d_belong, d_esteem = cap(d_belong), cap(d_esteem)
        d_control, d_meaning = cap(d_control), cap(d_meaning)

        new = replace(
            state,
            need_belong=_clamp(state.need_belong + d_belong),
            need_esteem=_clamp(state.need_esteem + d_esteem),
            need_control=_clamp(state.need_control + d_control),
            need_meaning=_clamp(state.need_meaning + d_meaning),
            last_event=key,
            updated_ts=stamp,
            is_muted=(True if key == "muted" else
                      (False if key == "unmuted" else state.is_muted)),
            unanswered_streak=(0 if key in ("unmuted", "apology")
                              else (state.unanswered_streak + 1
                                    if key == "ignored" else 0)),
            repeats=(state.repeats + 1 if key in
                     ("muted", "kicked", "banned", "insult", "humiliation")
                     else state.repeats),
        ).recalc_pain(vec.w_rel)

        return self._advance_stage(new, stamp, vec)

    def _advance_stage(self, state: SocialPainSnapshot, stamp: float,
                       vec: AppraisalVector) -> SocialPainSnapshot:
        """Стадия по Уильямсу: reflexive -> reflective -> resignation."""
        if state.pain_index < 0.12:
            if state.stage != "none":
                return replace(state, stage="none", stage_entered_ts=stamp)
            return state

        # Хроническое отвержение: много повторов или долго без примирения.
        chronic = (state.repeats >= CHRONIC_REPEATS
                   or (state.stage_entered_ts
                       and stamp - state.stage_entered_ts > RESIGNATION_AFTER_SEC))
        if chronic and state.pain_index >= 0.2:
            if state.stage != "resignation":
                return replace(state, stage="resignation",
                               stage_entered_ts=stamp)
            return state

        if state.stage == "none":
            return replace(state, stage="reflexive", stage_entered_ts=stamp)
        return state

    def step_decay(self, state: SocialPainSnapshot, dt_sec: float,
                   *, now: float | None = None,
                   after_sleep: bool = False) -> SocialPainSnapshot:
        """Пассивное затухание и переходы стадий по времени."""
        if dt_sec <= 0:
            return state
        stamp = now if now is not None else time.time()

        # Экспоненциальное вытягивание к норме.
        factor = math.exp(-dt_sec / TAU_SOCIAL_SEC)
        if after_sleep:
            factor *= (1.0 - SLEEP_RELIEF)

        def pull(value: float) -> float:
            return NEED_NORM - (NEED_NORM - value) * factor

        new = replace(
            state,
            need_belong=_clamp(pull(state.need_belong)),
            need_esteem=_clamp(pull(state.need_esteem)),
            need_control=_clamp(pull(state.need_control)),
            need_meaning=_clamp(pull(state.need_meaning)),
            updated_ts=stamp,
        ).recalc_pain(1.0)

        # Стадия зависит от возраста, а не только от боли.
        age = stamp - (new.stage_entered_ts or stamp)
        stage = new.stage
        if new.pain_index < 0.12:
            stage = "none"
            # Кумуляция сбрасывается, когда боль ушла. Иначе три
            # случайных мута за месяц навсегда помечали чат как
            # «хроническое отвержение», и resignation включалась
            # от любого пустяка.
            new = replace(new, repeats=0)
        elif age < REFLEXIVE_MAX_SEC:
            stage = "reflexive"
        elif age < REFLECTIVE_MAX_SEC:
            stage = "reflective"
        elif age >= RESIGNATION_AFTER_SEC:
            stage = "resignation"
        else:
            stage = "reflective"

        if stage != new.stage:
            # stage_entered_ts ставим только при СМЕНЕ стадии. Раньше он
            # обновлялся на каждом тике, из-за чего стадия «омолаживалась»
            # каждые 5 минут и resignation не наступала никогда.
            return replace(new, stage=stage, stage_entered_ts=stamp)
        return new

    def repair(self, state: SocialPainSnapshot, kind: str,
               now: float | None = None) -> SocialPainSnapshot:
        """Исцеление: извинение или снятие мута."""
        stamp = now if now is not None else time.time()
        profile = _PROFILES.get("apology" if kind == "apology" else "unmuted",
                               _PROFILES["apology"])
        new = replace(
            state,
            need_belong=_clamp(state.need_belong + profile[0]),
            need_esteem=_clamp(state.need_esteem + profile[1]),
            need_control=_clamp(state.need_control + profile[2]),
            need_meaning=_clamp(state.need_meaning + profile[3]),
            is_muted=(False if kind == "unmuted" else state.is_muted),
            repeats=(0 if kind == "apology" else state.repeats),
            updated_ts=stamp,
        ).recalc_pain(1.0)
        if new.pain_index < 0.12:
            new = replace(new, stage="none", stage_entered_ts=stamp)
        return new


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


# ============================================================== руминация

#: Распад заряда: пережёвывание 5 ч до половины, рефлексия 1 ч.
LAMBDA_BROOD_PER_MIN = 0.0023
LAMBDA_REFLECT_PER_MIN = 0.0115

#: Тонический дрейф от руминации (в минуту).
RUM_DS_PER_MIN = -0.018
RUM_DN_PER_MIN = 0.024

#: Ниже этого заряда след неактивен.
RUM_MIN_CHARGE = 0.15

#: Дно, ниже которого руминация не опускает серотонин.
#: Человек не становится бесконечно несчастнее: наступает дно.
RUMMINATION_FLOOR = 0.15


@dataclass
class RuminationState:
    """След пережёвывания: заряд, режим, источник."""
    trace_id: str = ""
    chat_id: int = 0
    source_episode_id: str = ""
    trigger_summary: str = ""
    current_charge: float = 0.0
    mode_rho: float = 0.0        # <0.5 пережёвывание, >=0.5 рефлексия
    created_ts: float = 0.0
    updated_ts: float = 0.0
    resolved: str = ""           # "" | reflection | apology | archived
    author_id: int = 0
    message_id: int = 0

    @property
    def active(self) -> bool:
        return not self.resolved and self.current_charge >= RUM_MIN_CHARGE

    def mode_name(self) -> str:
        if not self.active:
            return "none"
        return "reflection" if self.mode_rho >= 0.5 else "brooding"

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "chat_id": self.chat_id,
            "trigger": self.trigger_summary[:160],
            "charge": round(self.current_charge, 3),
            "mode_rho": round(self.mode_rho, 3),
            "mode": self.mode_name(),
            "resolved": self.resolved,
            "created_ts": self.created_ts,
            "updated_ts": self.updated_ts,
        }


class RuminationEngine:
    """Заряд пережёвывания: рождение, режим, распад, дрейф в моноамины."""

    def initial_charge(self, vec: AppraisalVector, *,
                       breaker_blocked: bool = False) -> float:
        """Начальный заряд: значимость, удар по лицу, беспомощность."""
        w = amplify(vec.w_rel)
        value = (0.45 * (vec.relevance * w)
                 + 0.35 * vec.ego
                 + 0.20 * (1.0 - vec.coping)
                 + (0.25 if breaker_blocked else 0.0))
        return round(max(0.0, min(1.0, value)), 4)

    def should_spawn(self, vec: AppraisalVector, *,
                     social_pain: float = 0.0,
                     breaker_blocked: bool = False,
                     emotion_intensity: float = 0.0) -> bool:
        """Когда обида превращается в след, который живёт часами."""
        if breaker_blocked:
            return True
        if social_pain >= 0.45:
            return True
        if vec.relevance * vec.ego * max(0.0, -vec.congruence) >= 0.50:
            return True
        if emotion_intensity >= 0.65 and vec.norm <= -0.40:
            return True
        return False

    def mode_rho(self, s: float, n: float, coping: float) -> float:
        """Режим переработки: пережёвывание или рефлексия.

        Логистическая сигмоида префронтального контроля: высокий S и
        низкий N дают рефлексию, истощённый серотонин при высоком
        норадреналине — зацикливание.
        """
        z = 5.0 * (s - 0.42) - 3.5 * (n - 0.55) + 2.0 * (coping - 0.30)
        return round(1.0 / (1.0 + math.exp(-z)), 4)

    def step(self, state: RuminationState, dt_sec: float, *,
             s: float, n: float, coping: float,
             now: float | None = None) -> RuminationState:
        """Один шаг: обновить режим и снять заряд."""
        if not state.active or dt_sec <= 0:
            return state
        stamp = now if now is not None else time.time()
        rho = self.mode_rho(s, n, coping)
        rate = (LAMBDA_BROOD_PER_MIN * (1.0 - rho)
                + LAMBDA_REFLECT_PER_MIN * rho)
        charge = state.current_charge * math.exp(-rate * (dt_sec / 60.0))
        resolved = state.resolved
        if charge < RUM_MIN_CHARGE and not resolved:
            # распалось само — считаем рефлексией, если режим был высокий
            resolved = "reflection" if rho >= 0.5 else "archived"
        return replace(state, current_charge=round(charge, 4),
                       mode_rho=rho, updated_ts=stamp, resolved=resolved)

    def drift(self, state: RuminationState, dt_sec: float,
              s_current: float | None = None) -> tuple[float, float]:
        """Тонический вклад руминации в моноамины (dS, dN).

        Пережёвывание держит серотонин внизу и норадреналин наверху —
        ровно то, из-за чего обида не проходит сама.

        Но у этого есть предел. Без ограничителя дрейф сильнее клиренса
        и серотонин упирается в ноль и держится там часами: равновесие
        выходит 0.62 − 0.018/0.035 = 0.106, а с дискретными тиками и
        вовсе 0.000. Человек не становится бесконечно несчастнее —
        наступает дно, и дальше он просто живёт на дне. Поэтому ниже
        RUMMINATION_FLOOR дрейф гасится до нуля.
        """
        if not state.active or dt_sec <= 0:
            return 0.0, 0.0

        # гасим дрейф у пола: дальше опускать некуда
        suppress = 1.0
        if s_current is not None and s_current < RUMMINATION_FLOOR:
            suppress = 0.0
        elif s_current is not None and s_current < RUMMINATION_FLOOR + 0.10:
            suppress = (s_current - RUMMINATION_FLOOR) / 0.10

        if suppress <= 0:
            return 0.0, 0.0

        minutes = dt_sec / 60.0
        rho = state.mode_rho
        ds = (RUM_DS_PER_MIN * state.current_charge * (1.0 - rho)
              * minutes * suppress)
        dn = (RUM_DN_PER_MIN * state.current_charge
              * (1.0 - 0.5 * rho) * minutes * suppress)
        return ds, dn

    def apology_relief(self, state: RuminationState,
                       now: float | None = None) -> RuminationState:
        """Извинение снимает 60% заряда, но не всё: тяжёлая обида инерционна."""
        if not state.active:
            return state
        stamp = now if now is not None else time.time()
        charge = state.current_charge * 0.40
        return replace(state, current_charge=round(charge, 4),
                       updated_ts=stamp,
                       resolved=("apology" if charge < RUM_MIN_CHARGE
                                 else state.resolved))
