"""Нейромодуляторный движок: связанные ОДУ моноаминов, RK4.

Уравнение (спека NEURO-AFFECT, раздел 4.1):

    dc/dt = -K(c - c0) + W_int·tanh(c - c0) + B·u + ξ

где
    c    = [S, N, D]              мгновенные концентрации, [0,1]³
    c0   = c_base + A_circ·h     гомеостатическая точка притяжения
    K    = diag(κS, κN, κD)      клиренс, κN > κD > κS
    W_int                        перекрёстное торможение
    B·u                          вход стимула
    ξ                            шум

Ключевое свойство — **инерция**: после оскорбления состояние не
переключается в «прощение» за один тик. Именно этого не хватало
декларативной эмоции из промпта.

Модуль чистый: ни БД, ни сети. Время передаётся снаружи, шум
отключаем для детерминированных тестов.
"""
from __future__ import annotations

import logging
import math
import random
import time
from dataclasses import dataclass, field
from typing import Any

from .types import (AffectSnapshot, AppraisalImpulse, Homeostasis,
                    Monoamines, PAD, clamp)

log = logging.getLogger(__name__)

#: Коэффициенты клиренса и связи заданы в 1/мин (docs/research/01-affect/AFFECT.md §2.2), а шаг
#: интегрирования — в секундах. Пересчёт в одном месте, чтобы не разошёлся.
MINUTE = 1.0 / 60.0


@dataclass
class EngineConfig:
    """Параметры движка. Значения — из спеки, раздел 3.3."""

    # базовый темперамент
    baseline_s: float = 0.62
    baseline_n: float = 0.38
    baseline_d: float = 0.60

    #: Привыкание к повторным ударам: 5-й подряд слабее 1-го.
    habituation_enabled: bool = True

    # клиренс (1/мин). Обязательно κN > κD > κS.
    kappa_s: float = 0.035
    kappa_n: float = 0.320
    kappa_d: float = 0.140

    # перекрёстная модуляция
    gamma_sn: float = 0.12   # высокий N истощает S
    gamma_ns: float = 0.26   # высокий S тормозит N
    gamma_nd: float = 0.08   # D слегка поднимает N
    gamma_ds: float = 0.30   # высокий S тормозит D
    gamma_dn: float = 0.18   # хронический N притупляет D

    # шум
    noise_enabled: bool = True
    noise_s: float = 0.002
    noise_n: float = 0.006
    noise_d: float = 0.004

    # гомеостаз -> сдвиг базовой точки (A_circ)
    a_fatigue_s: float = -0.20
    a_load_s: float = -0.10
    a_satiety_s: float = 0.10
    a_fatigue_n: float = -0.15
    a_load_n: float = 0.25
    a_satiety_n: float = -0.05
    a_fatigue_d: float = -0.30
    a_load_d: float = -0.05
    a_satiety_d: float = 0.10

    # матрица B: как стимул проецируется в моноамины
    b_s_dv: float = 0.35
    b_s_threat: float = -0.45
    b_s_rpe: float = 0.10
    b_s_surprise: float = -0.05
    # N — возбуждение и угроза. RPE здесь почти не участвует:
    # в биологии ошибку предсказания кодирует дофамин, а не норадреналин.
    # Если нагрузить N ещё и RPE, негативный RPE гасит возбуждение —
    # то есть оскорбление от близкого человека «успокаивает». Это неверно.
    b_n_dv: float = -0.08
    b_n_threat: float = 0.70
    b_n_rpe: float = 0.02
    b_n_surprise: float = 0.55
    b_d_dv: float = 0.45
    b_d_threat: float = 0.05
    b_d_rpe: float = 0.75
    b_d_surprise: float = 0.25

    # скорость обновления гомеостаза
    # усталость: полный износ за ~8 часов бодрствования (1/28800)
    fatigue_awake_rate: float = 0.0000347
    fatigue_token_rate: float = 0.000004    # за токен
    social_satiety_gain: float = 0.05     # за сообщение
    social_decay_rate: float = 0.00003    # насыщение спадает за ~9 часов
    load_decay_rate: float = 0.02

    # масштаб фазического выброса: сколько единиц концентрации
    # добавляет импульс силы 1. Подобрано так, чтобы сильное оскорбление
    # давало заметный сдвиг PAD, но не сваливало систему в 0/1.
    burst_gain: float = 0.22

    # потолок одного выброса: одно событие не может высушить весь резервуар.
    # Без этого близкий человек (w_rel=2.5) одним словом обнуляет дофамин.
    burst_ceiling: float = 0.45


def project_pad(mono: Monoamines) -> PAD:
    """Моноамины -> PAD. Гедонический и агонистический режимы дофамина.

    Раньше доминирование считалось как 1.2(S-0.5) + 0.2(N-0.5) + 0.6(D-0.5),
    то есть с перевесом серотонина. Из-за этого в гневе (S падает) персонаж
    чувствовал потерю контроля — а гнев это как раз высокая власть.
    Отсюда зона ярости и была недостижима на уровне ощущения.

    Теперь режим зависит от серотонина: при S >= 0.30 дофамин работает
    как награда (радость), при S < 0.30 — как агонистическая мобилизация
    (злость). Множитель sgn(S-0.30) переключает знак вклада D.
    Вес доминирования перенесён на D (1.10) и N (0.35): власть в гневе
    и беспомощность в остракизме получаются без искусственных правок.
    """
    ds = mono.s - 0.5
    dn = mono.n - 0.5
    dd = mono.d - 0.5
    # Переключаем знак вклада дофамина только когда дофамин ПОВЫШЕН.
    # При низком S и низком D (стыд, коллапс) дофамин должен давать
    # минус в удовольствие, а не плюс: иначе точка стыда (0,0,0)
    # получалась «приятной».
    p_d = 0.75 * dd * (-1.0 if (mono.s < 0.30 and dd > 0) else 1.0)
    p = 1.15 * ds - 0.65 * dn + p_d
    a = -0.35 * ds + 1.35 * dn + 0.55 * dd
    dom = 0.45 * ds + 0.35 * dn + 1.10 * dd
    return PAD(p, a, dom).clamped()


class NeuromodulatoryEngine:
    """Решает ОДУ моноаминов шагами RK4."""

    def __init__(self, cfg: EngineConfig | None = None,
                 bus: Any = None, seed: int | None = None) -> None:
        self.cfg = cfg or EngineConfig()
        self.bus = bus
        self._rng = random.Random(seed)
        self._mono = Monoamines(self.cfg.baseline_s, self.cfg.baseline_n,
                                self.cfg.baseline_d)
        self.h = Homeostasis()
        self._pending: list[tuple[float, float, float, float]] = []
        self._last_burst: dict[str, float] = {}
        self._last_ts = time.time()
        self._counterweight = False
        self._breaker = False
        self._validate()

    def _validate(self) -> None:
        cfg = self.cfg
        if not (cfg.kappa_n > cfg.kappa_d > cfg.kappa_s):
            raise ValueError(
                "порядок клиренса нарушен: должно быть kappa_n > kappa_d > kappa_s "
                f"(сейчас {cfg.kappa_n}, {cfg.kappa_d}, {cfg.kappa_s})")

    # ------------------------------------------------------------ состояние

    @property
    def mono(self) -> Monoamines:
        return self._mono

    @property
    def pad(self) -> PAD:
        return project_pad(self._mono)

    def snapshot(self, ts: float | None = None) -> AffectSnapshot:
        return AffectSnapshot(
            ts=ts if ts is not None else time.time(),
            mono=self._mono, pad=self.pad, homeostasis=self.h,
            counterweight_active=self._counterweight,
            circuit_breaker_active=self._breaker,
        )

    def set_state(self, *, s: float | None = None, n: float | None = None,
                  d: float | None = None) -> None:
        """Прямая установка состояния — для тестов и восстановления из БД."""
        self._mono = Monoamines(
            self._mono.s if s is None else s,
            self._mono.n if n is None else n,
            self._mono.d if d is None else d,
        ).clamped()

    def set_homeostasis(self, *, fatigue: float | None = None,
                        cognitive_load: float | None = None,
                        social_satiety: float | None = None) -> None:
        current = self.h
        self.h = Homeostasis(
            current.fatigue if fatigue is None else fatigue,
            current.cognitive_load if cognitive_load is None else cognitive_load,
            current.social_satiety if social_satiety is None else social_satiety,
        ).clamped()

    def set_flags(self, *, counterweight: bool | None = None,
                  breaker: bool | None = None) -> None:
        if counterweight is not None:
            self._counterweight = counterweight
        if breaker is not None:
            self._breaker = breaker

    # -------------------------------------------------------------- импульс

    def apply_burst(self, ds: float, dn: float, dd: float) -> None:
        """Фазический выброс готовыми приращениями.

        Зачем отдельно от apply_impulse: матрица B* в appraisal.py умеет
        то, чего не умеет линейная B — агонистический вклад дофамина.
        Считать её здесь и передавать результат честнее, чем дублировать
        веса в двух местах.
        """
        cfg = self.cfg
        ceiling = float(getattr(cfg, "burst_ceiling", 0.45))
        mono = self._mono

        # Привыкание к повторным ударам. Пять оскорблений подряд не должны
        # пробивать дно: у человека каждый следующий удар бьёт слабее,
        # потому что ресурс уже сожжён. Считаем серию негативных вспышек
        # подряд и гасим её геометрически.
        import time as _t
        now = _t.time()
        habituation = getattr(cfg, "habituation_enabled", True)
        if ds < 0 and habituation:
            if now - getattr(self, "_last_neg_burst", 0.0) < 1800:
                self._neg_streak = getattr(self, "_neg_streak", 0) + 1
            else:
                self._neg_streak = 1
            self._last_neg_burst = now
            habit = 0.75 ** min(self._neg_streak - 1, 3)
            ds *= habit
            dd *= habit
            dn *= (0.85 + 0.15 * habit)      # возбуждение гаснет медленнее
        else:
            self._neg_streak = 0

        self._mono = Monoamines(
            mono.s + max(-ceiling, min(ceiling, ds)),
            mono.n + max(-ceiling, min(ceiling, dn)),
            mono.d + max(-ceiling, min(ceiling, dd)),
        ).clamped()
        self._last_burst = {"ds": round(ds, 4), "dn": round(dn, 4),
                            "dd": round(dd, 4),
                            "habit_streak": getattr(self, "_neg_streak", 0)}
        if self.bus is not None:
            self.bus.emit("affect.burst", **self._last_burst)

    def apply_impulse(self, impulse: AppraisalImpulse) -> None:
        """Фазический выброс: импульс сразу меняет концентрации.

        Почему не «вход в ОДУ»: если держать B·u постоянным, равновесие
        системы сдвигается на B·u/κ, что при κ≈0.035 даёт абсурдные
        значения (девять единиц вместо единицы). Биологически выброс
        моноаминов фазический: он мгновенно добавляется к концентрации,
        а затем клиренс его смывает. Отсюда и настоящая инерция.
        """
        cfg = self.cfg
        u_dv, u_threat, u_rpe, u_surprise = impulse.vector_u()

        # B·u — проекция стимула в моноамины, с общим масштабом
        ds = (cfg.b_s_dv * u_dv + cfg.b_s_threat * u_threat
              + cfg.b_s_rpe * u_rpe + cfg.b_s_surprise * u_surprise)
        dn = (cfg.b_n_dv * u_dv + cfg.b_n_threat * u_threat
              + cfg.b_n_rpe * u_rpe + cfg.b_n_surprise * u_surprise)
        dd = (cfg.b_d_dv * u_dv + cfg.b_d_threat * u_threat
              + cfg.b_d_rpe * u_rpe + cfg.b_d_surprise * u_surprise)

        gain = cfg.burst_gain
        ceiling = cfg.burst_ceiling
        self._mono = Monoamines(
            self._mono.s + clamp(ds * gain, -ceiling, ceiling),
            self._mono.n + clamp(dn * gain, -ceiling, ceiling),
            self._mono.d + clamp(dd * gain, -ceiling, ceiling),
        ).clamped()
        self._last_burst = {"ds": round(clamp(ds * gain, -ceiling, ceiling), 4),
                            "dn": round(clamp(dn * gain, -ceiling, ceiling), 4),
                            "dd": round(clamp(dd * gain, -ceiling, ceiling), 4)}
        if self.bus is not None:
            self.bus.emit("affect.impulse", **impulse.to_dict())

    def pending_impulses(self) -> int:
        return 0

    # ------------------------------------------------------------- гомеостаз

    def baseline_point(self) -> Monoamines:
        """c0 = c_base + A_circ·h — точка, к которой тянет клиренс."""
        cfg, h = self.cfg, self.h
        s0 = (cfg.baseline_s
              + cfg.a_fatigue_s * h.fatigue
              + cfg.a_load_s * h.cognitive_load
              + cfg.a_satiety_s * h.social_satiety)
        n0 = (cfg.baseline_n
              + cfg.a_fatigue_n * h.fatigue
              + cfg.a_load_n * h.cognitive_load
              + cfg.a_satiety_n * h.social_satiety)
        d0 = (cfg.baseline_d
              + cfg.a_fatigue_d * h.fatigue
              + cfg.a_load_d * h.cognitive_load
              + cfg.a_satiety_d * h.social_satiety)
        return Monoamines(s0, n0, d0).clamped()

    def _input_vector(self, dt: float) -> tuple[float, float, float, float]:
        """Суммарный стимул за шаг.

        Важно: импульс — не «мощность на всё время паузы». Если между
        сообщениями прошло 60 секунд, одно слово не должно взрывать
        состояние. Поэтому вклад ограничен по эффективному времени:
        длинный шаг не усиливает одиночный импульс.
        """
        if not self._pending or dt <= 0:
            return (0.0, 0.0, 0.0, 0.0)
        total = [0.0, 0.0, 0.0, 0.0]
        for vec in self._pending:
            for i, value in enumerate(vec):
                total[i] += value
        # средний импульс по пачке (не сумма): два сообщения не дают 2x
        count = len(self._pending)
        avg = [v / count for v in total]
        return tuple(clamp(v, -1.5, 1.5) for v in avg)  # type: ignore[return-value]

    def effective_impulse_dt(self, dt: float) -> float:
        """Сколько времени импульс реально действует (с потолком).

        Потолок нужен, чтобы одиночное сообщение после долгой паузы
        не переводило систему в крайнюю точку за один шаг.
        """
        return min(max(dt, 0.0), 0.5)

    def _derivative(self, c: Monoamines, c0: Monoamines,
                    u: tuple[float, float, float, float],
                    noise: tuple[float, float, float]) -> tuple[float, float, float]:
        cfg = self.cfg
        xs = c.s - c0.s
        xn = c.n - c0.n
        xd = c.d - c0.d

        # K·(c - c0) — клиренс тянет к базовой точке.
        #
        # ВАЖНО про единицы: kappa и gamma заданы в 1/мин (docs/research/01-affect/AFFECT.md
        # §2.2 — «клиренс (1/мин)», «норадреналин гаснет за минуты»), а шаг
        # интегрирования идёт в СЕКУНДАХ. Раньше коэффициенты применялись
        # как есть, и вся динамика была в 60 раз быстрее задуманного:
        # полураспад S — 28.6 с вместо 28.6 мин. Оскорбление забывалось за
        # две минуты, поэтому мут её не расстроил.
        #
        # Делить надо ОБА семейства: клиренс и связь — части одной матрицы,
        # и если замедлить только клиренс, связь начинает доминировать и
        # выбрасывает S в ноль навсегда.
        ds = -(cfg.kappa_s * MINUTE) * xs
        dn = -(cfg.kappa_n * MINUTE) * xn
        dd = -(cfg.kappa_d * MINUTE) * xd

        # W_int·φ(c - c0). ВАЖНО: φ — односторонняя (rectified) активация,
        # а не голый tanh. Только ИЗБЫТОК модулятора подавляет другой.
        #
        # Почему так: при голом tanh дефицит S (x_s < 0) даёт -γ_NS·tanh(x_s) > 0,
        # то есть повышает N; высокий N в свою очередь ещё сильнее давит S.
        # Получается положительная обратная связь, и S залипает на нуле.
        # Биологически же истощение серотонина само по себе не «включает»
        # норадреналин — работает только избыток.
        th_s = max(0.0, math.tanh(xs))
        th_n = max(0.0, math.tanh(xn))
        th_d = max(0.0, math.tanh(xd))

        ds += -(cfg.gamma_sn * MINUTE) * th_n          # избыток N истощает S
        dn += (-cfg.gamma_ns * th_s + cfg.gamma_nd * th_d) * MINUTE
        dd += (-cfg.gamma_ds * th_s - cfg.gamma_dn * th_n) * MINUTE

        # B·u
        u_dv, u_threat, u_rpe, u_surprise = u
        ds += (cfg.b_s_dv * u_dv + cfg.b_s_threat * u_threat
               + cfg.b_s_rpe * u_rpe + cfg.b_s_surprise * u_surprise)
        dn += (cfg.b_n_dv * u_dv + cfg.b_n_threat * u_threat
               + cfg.b_n_rpe * u_rpe + cfg.b_n_surprise * u_surprise)
        dd += (cfg.b_d_dv * u_dv + cfg.b_d_threat * u_threat
               + cfg.b_d_rpe * u_rpe + cfg.b_d_surprise * u_surprise)

        # шум
        ds += noise[0]
        dn += noise[1]
        dd += noise[2]
        return ds, dn, dd

    def step(self, dt_seconds: float, now: float | None = None) -> AffectSnapshot:
        """Один шаг RK4. Возвращает новое состояние.

        Большой dt разбивается на сублишаги: при dt > 1 с RK4 на связанной
        системе теряет устойчивость и улетает в потолок вместо возврата
        к базовой точке. Импульс применяется только на первом сублишаге.
        """
        dt = max(0.0, float(dt_seconds))
        if dt == 0.0:
            return self.snapshot(now)

        max_sub = 1.0
        remaining = dt
        first = True
        snapshot: AffectSnapshot | None = None
        while remaining > 1e-9:
            sub = min(remaining, max_sub)
            snapshot = self._step_once(sub, impulse_active=first)
            remaining -= sub
            first = False
        self._last_ts = now if now is not None else time.time()
        result = self.snapshot(self._last_ts)
        if self.bus is not None:
            self.bus.emit("affect.state", **result.to_dict())
        return result

    def _step_once(self, dt: float, impulse_active: bool = True) -> AffectSnapshot:
        """Один интеграционный шаг без дробления."""
        cfg = self.cfg
        c0 = self.baseline_point()
        u = self._input_vector(dt) if impulse_active else (0.0, 0.0, 0.0, 0.0)
        # импульс действует ограниченное время, клиренс — всё время шага
        u_dt = self.effective_impulse_dt(dt) if impulse_active else 0.0
        c = self._mono
        if cfg.noise_enabled:
            noise = (
                self._rng.gauss(0.0, cfg.noise_s),
                self._rng.gauss(0.0, cfg.noise_n),
                self._rng.gauss(0.0, cfg.noise_d),
            )
        else:
            noise = (0.0, 0.0, 0.0)

        def deriv(state: Monoamines) -> tuple[float, float, float]:
            """Производная: клиренс и связь — на dt, вход — на u_dt."""
            full = self._derivative(state, c0, (0.0, 0.0, 0.0, 0.0), noise)
            if not impulse_active or u_dt <= 0:
                return full
            stim = self._derivative(state, c0, u, (0.0, 0.0, 0.0))
            pure = self._derivative(state, c0, (0.0, 0.0, 0.0, 0.0),
                                    (0.0, 0.0, 0.0))
            # stim - pure = вклад только от входа
            return (
                full[0] + (stim[0] - pure[0]) * (u_dt / dt),
                full[1] + (stim[1] - pure[1]) * (u_dt / dt),
                full[2] + (stim[2] - pure[2]) * (u_dt / dt),
            )

        k1 = deriv(c)
        c2 = Monoamines(c.s + 0.5 * dt * k1[0], c.n + 0.5 * dt * k1[1],
                        c.d + 0.5 * dt * k1[2])
        k2 = deriv(c2)
        c3 = Monoamines(c.s + 0.5 * dt * k2[0], c.n + 0.5 * dt * k2[1],
                        c.d + 0.5 * dt * k2[2])
        k3 = deriv(c3)
        c4 = Monoamines(c.s + dt * k3[0], c.n + dt * k3[1], c.d + dt * k3[2])
        k4 = deriv(c4)

        new_s = c.s + (dt / 6.0) * (k1[0] + 2 * k2[0] + 2 * k3[0] + k4[0])
        new_n = c.n + (dt / 6.0) * (k1[1] + 2 * k2[1] + 2 * k3[1] + k4[1])
        new_d = c.d + (dt / 6.0) * (k1[2] + 2 * k2[2] + 2 * k3[2] + k4[2])
        self._mono = Monoamines(new_s, new_n, new_d).clamped()

        # гомеостаз живёт вместе с состоянием
        self._advance_homeostasis(dt)
        if impulse_active:
            self._pending.clear()
        return self.snapshot()

    def _advance_homeostasis(self, dt: float) -> None:
        cfg, h = self.cfg, self.h
        fatigue = clamp(h.fatigue + dt * cfg.fatigue_awake_rate, 0.0, 1.0)
        load = h.cognitive_load - dt * cfg.load_decay_rate * h.cognitive_load
        satiety = clamp(h.social_satiety - dt * cfg.social_decay_rate, 0.0, 1.0)
        self.h = Homeostasis(fatigue, clamp(load, 0.0, 1.0), satiety).clamped()

    # --------------------------------------------------------- вмешательства

    def register_message(self, valence: float = 0.0) -> None:
        """Общение насыщает социальную потребность — но не любое.

        Раньше +0.05 начислялось на каждое входящее сообщение, включая
        «вы сгниете заживо». Травля в чате парадоксально насыщала
        потребность в общении и стабилизировала настроение. Теперь
        негативная реплика (valence < -0.15) не кормит, а сушит: -половина
        обычного прироста.
        """
        cfg, h = self.cfg, self.h
        gain = cfg.social_satiety_gain
        if valence < -0.15:
            gain = -gain * 0.5      # неприятное общение не насыщает
        elif valence > 0.15:
            gain = gain * 1.2       # тёплое — насыщает чуть сильнее
        self.h = Homeostasis(
            h.fatigue,
            h.cognitive_load,
            clamp(h.social_satiety + gain, 0.0, 1.0),
        ).clamped()

    def register_tokens(self, count: int) -> None:
        """Генерация токенов утомляет — как реальная работа."""
        if count <= 0:
            return
        cfg, h = self.cfg, self.h
        self.h = Homeostasis(
            clamp(h.fatigue + cfg.fatigue_token_rate * count, 0.0, 1.0),
            h.cognitive_load,
            h.social_satiety,
        ).clamped()

    def register_task(self, count: int = 1) -> None:
        """Активные задачи/инструменты поднимают когнитивную нагрузку."""
        cfg, h = self.cfg, self.h
        self.h = Homeostasis(
            h.fatigue,
            clamp(h.cognitive_load + 0.08 * max(1, count), 0.0, 1.0),
            h.social_satiety,
        ).clamped()

    def force_reappraisal(self, delta_s: float, delta_n: float) -> None:
        """Нисходящий префронтальный импульс (от circuit breaker)."""
        self._mono = Monoamines(
            clamp(self._mono.s + delta_s, 0.0, 1.0),
            clamp(self._mono.n + delta_n, 0.0, 1.0),
            self._mono.d,
        ).clamped()
        log.info("переоценка: S%+.2f N%+.2f -> %s", delta_s, delta_n,
                 self._mono.to_dict())

    def reset_homeostasis_after_sleep(self, quality: float = 1.0) -> None:
        """Утро: усталость снята пропорционально качеству сна.

        28.10: раньше усталость всегда обнулялась. Но плохая ночь
        (руминация, гипервозбуждение, пробуждения) не восстанавливает
        полностью — при Q=0.52 остаётся почти половина, и это ровно то,
        что делает порочный круг из 07-sleep/REPORT.md.
        """
        q = clamp(quality, 0.0, 1.0)
        h = self.h
        # при полном сне — в ноль, иначе остаётся (1-q) от накопленного
        fatigue = 0.0 if q >= 0.95 else h.fatigue * (1.0 - q)
        self.h = Homeostasis(fatigue, 0.0, h.social_satiety * 0.3).clamped()

    def enter_rem_depression(self) -> None:
        """REM — состояние с дефицитом моноаминов.

        Chen et al. (2026): REM холинергически доминирующий и
        моноамин-истощённый. Наш прежний код во сне тянул S/N/D К базе —
        то есть делал ровно противоположное. Здесь моноамины падают в
        минимум, и именно на этом фоне идёт аффективное разъединение.
        """
        self._mono = Monoamines(0.05, 0.05, 0.05).clamped()

    def shift_toward_baseline(self, fraction: float = 0.5) -> None:
        """Сон мягко возвращает моноамины к базовой точке."""
        c0 = self.baseline_point()
        f = clamp(fraction, 0.0, 1.0)
        self._mono = Monoamines(
            self._mono.s + (c0.s - self._mono.s) * f,
            self._mono.n + (c0.n - self._mono.n) * f,
            self._mono.d + (c0.d - self._mono.d) * f,
        ).clamped()
