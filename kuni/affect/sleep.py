"""Режим сна и учёт диалоговых обязательств.

Две вещи, которые нельзя решить промптом, потому что в моменты молчания
LLM вообще не вызывается:

  CommitmentTracker  — кому она должна ответ, и сколько ещё ждать
  SleepManager       — состояние сна с фазами и гейтом задержки

Главное разделение: «хочу спать» (давление) и «могу лечь» (гейт). Это
разные величины. Усталость может быть предельной, а уйти нельзя —
ждёшь ответа или в середине разговора.

Поправки к исходной архитектуре (см. 09-sleep-mental/REVIEW.md):
1. Экспонента затухания применяется ТОЛЬКО к социальным причинам.
   Физиологические (высокий N, незакрытая задача) от времени не слабеют:
   иначе через полтора часа она «заснёт», физически не имея возможности.
2. sleep_debt с явным потолком — сделан в mental.py.
"""
from __future__ import annotations

import logging
import math
import sqlite3
import time
from dataclasses import dataclass, field, replace
from typing import Any

from ..core import clock

log = logging.getLogger(__name__)

# --- фазы -------------------------------------------------------------

AWAKE = "awake"
WIND_DOWN = "wind_down"
NREM_SWS = "nrem_sws"
REM = "rem"
WAKE_INERTIA = "wake_inertia"
FRAGMENTED_WAKE = "fragmented_wake"

#: Фазы, в которых входящие не читаются и копятся в очереди.
SLEEPING_PHASES = (NREM_SWS, REM)

#: Компрессия: реальный сон 8 ч -> ядро 40 мин.
CORE_SECONDS = 2400.0

#: Доли фаз внутри ядра (архитектура биологического сна).
PHASE_SHARES = {
    NREM_SWS: 0.45,
    REM: 0.35,
    WAKE_INERTIA: 0.15,
    WIND_DOWN: 0.05,
}

#: Сонная инерция после ядра — «ещё в постели с телефоном».
INERTIA_EXTRA = 300.0

# --- давление сна -----------------------------------------------------

#: Порог желания спать.
SLEEP_THRESHOLD = 0.65
#: Минимальная усталость: без неё спать нельзя даже ночью.
MIN_FATIGUE = 0.72
#: Порог снятия гейта.
DELAY_RELEASE = 0.35
#: Жёсткий предел откладывания.
MAX_DELAY = 5400.0
#: Постоянная времени истощения воли.
TAU_EXHAUST = 3600.0
#: С какой усталости включается экстренный буст давления.
BOOST_FROM_FATIGUE = 0.92
#: Критическая усталость — срыв в сон безоговорочно.
CRITICAL_FATIGUE = 0.98
CRITICAL_PRESSURE = 0.92

# --- якоря задержки ---------------------------------------------------

#: Разговор считается живым, если последняя реплика была меньше 5 мин назад.
ACTIVE_DIALOGUE_SEC = 300.0
#: Порог W_expect, после которого чат ждёт ответа.
EXPECT_THRESHOLD = 0.45
#: Норадреналин выше этого не даёт уснуть.
HYPERAROUSAL_N = 0.60
#: Окно «мстительной прокрастинации».
REVENGE_WINDOW = 2700.0
#: Выгорание выше этого включает цинизм.
DEPLETION_CYNICAL = 0.60
#: Множитель усталости, пока тянет со сном.
POSTPONE_FATIGUE_X = 2.0
POSTPONE_DEBT_PER_MIN = 0.0015
POSTPONE_DEPLETION_PER_MIN = 0.002


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(v)))


def circadian(hour_value: float) -> float:
    """Циркадный модулятор: максимум в 03:30, минимум в 15:30."""
    return 0.5 * (1.0 + math.cos(2 * math.pi * (hour_value - 3.5) / 24.0))


# ======================================================================
# Учёт обязательств
# ======================================================================


@dataclass
class ChatCommitment:
    """Что этот чат от неё ждёт."""

    chat_id: int
    awaiting: bool = False          # ждёт ответа на свою реплику
    sent_ts: float = 0.0            # когда отправила
    timeout_sec: float = 0.0        # сколько готова ждать
    proactive: bool = False         # это была её инициатива
    w_rel: float = 1.0              # близость
    last_msg_ts: float = 0.0        # последнее входящее
    last_sent_ts: float = 0.0       # последнее исходящее

    @property
    def expired(self) -> bool:
        return self.awaiting and self.sent_ts + self.timeout_sec <= time.time()

    def remaining(self, now: float) -> float:
        if not self.awaiting:
            return 0.0
        return max(0.0, self.sent_ts + self.timeout_sec - now)


class CommitmentTracker:
    """Детерминированный автомат: кто ждёт ответа и сколько.

    Работает на событиях, без LLM. Именно поэтому он и может управлять
    таймингом сна — промпт в эти моменты не существует.
    """

    def __init__(self) -> None:
        self._chats: dict[int, ChatCommitment] = {}

    def state(self, chat_id: int) -> ChatCommitment:
        return self._chats.setdefault(chat_id, ChatCommitment(chat_id=chat_id))

    def on_sent(self, chat_id: int, *, text: str = "", proactive: bool = False,
                w_rel: float = 1.0, affect_high: bool = False,
                now: float | None = None) -> ChatCommitment:
        """Учесть отправленное сообщение: ждёт ли она теперь ответа."""
        stamp = now if now is not None else time.time()
        st = self.state(chat_id)
        has_question = "?" in text or "？" in text

        # сила ожидания
        expect = clamp(
            0.50 * (1.0 if proactive else 0.0)
            + 0.35 * (1.0 if has_question else 0.0)
            + 0.25 * clamp(w_rel / 2.5, 0.0, 1.0)
            + 0.20 * (1.0 if affect_high else 0.0),
            0.0, 1.0)

        st.last_sent_ts = stamp
        st.w_rel = w_rel
        st.proactive = proactive

        if expect >= EXPECT_THRESHOLD:
            st.awaiting = True
            st.sent_ts = stamp
            # для владельца 20 минут, для рядового ~12
            st.timeout_sec = 480.0 + 720.0 * clamp((w_rel - 0.3) / 2.2, 0.0, 1.0)
            log.debug("ожидание в чате %s: W=%.2f, таймаут %.0fс",
                      chat_id, expect, st.timeout_sec)
        else:
            st.awaiting = False
            st.sent_ts = 0.0
            st.timeout_sec = 0.0
        return st

    def on_received(self, chat_id: int, *, now: float | None = None) -> tuple[bool, bool]:
        """Входящее сообщение. Возвращает (было_ожидание, истекло_напрасно)."""
        stamp = now if now is not None else time.time()
        st = self.state(chat_id)
        was_awaiting = st.awaiting
        expired_before = st.expired

        st.awaiting = False
        st.sent_ts = 0.0
        st.timeout_sec = 0.0
        st.last_msg_ts = stamp

        # ждала, но собеседник ответил уже после таймаута — это тоже обидно,
        # но меньше: он всё-таки вернулся
        return was_awaiting, expired_before

    def expire(self, now: float | None = None) -> list[ChatCommitment]:
        """Закрыть истёкшие ожидания. Возвращает те, что истекли НАПРАСНО."""
        stamp = now if now is not None else time.time()
        burned: list[ChatCommitment] = []
        for st in self._chats.values():
            if st.awaiting and st.sent_ts + st.timeout_sec <= stamp:
                burned.append(replace(st))
                st.awaiting = False
                st.sent_ts = 0.0
                st.timeout_sec = 0.0
        return burned

    def is_active_dialogue(self, chat_id: int, now: float | None = None) -> bool:
        """Живой разговор: последняя реплика была недавно."""
        stamp = now if now is not None else time.time()
        st = self._chats.get(chat_id)
        if st is None:
            return False
        return 0.0 < stamp - st.last_msg_ts < ACTIVE_DIALOGUE_SEC

    def waiting_pressure(self, now: float | None = None) -> tuple[float, int]:
        """Максимальное давление ожидания и в каком чате."""
        stamp = now if now is not None else time.time()
        best, best_chat = 0.0, 0
        for st in self._chats.values():
            if not st.awaiting:
                continue
            rem = st.remaining(stamp)
            if st.timeout_sec <= 0:
                continue
            # плавно убывает к таймауту
            pressure = clamp(rem / st.timeout_sec, 0.0, 1.0)
            if pressure > best:
                best, best_chat = pressure, st.chat_id
        return best, best_chat

    def dialogue_pressure(self, w_rel_of, now: float | None = None) -> float:
        """Давление живого разговора по всем чатам."""
        stamp = now if now is not None else time.time()
        best = 0.0
        for chat_id, st in self._chats.items():
            if st.last_msg_ts <= 0:
                continue
            delta = stamp - st.last_msg_ts
            if delta >= ACTIVE_DIALOGUE_SEC:
                continue
            decay = 1.0 - delta / ACTIVE_DIALOGUE_SEC
            close = clamp(0.6 + 0.2 * (w_rel_of(chat_id) if w_rel_of else 1.0),
                          0.6, 1.0)
            best = max(best, decay * close)
        return best

    def snapshot(self) -> dict[str, Any]:
        return {
            "awaiting": [
                {"chat_id": st.chat_id, "remaining": round(st.remaining(time.time()), 1),
                 "proactive": st.proactive, "w_rel": round(st.w_rel, 2)}
                for st in self._chats.values() if st.awaiting
            ],
        }


# ======================================================================
# Режим сна
# ======================================================================


@dataclass
class SleepSnapshot:
    phase: str = AWAKE
    phase_entered_ts: float = 0.0
    postponed_since_ts: float = 0.0
    drive: float = 0.0
    delay_gate: float = 0.0
    delay_reason: str = ""
    quality: float = 1.0
    awakenings: int = 0
    target_duration: float = CORE_SECONDS
    updated_ts: float = 0.0

    @property
    def is_asleep(self) -> bool:
        return self.phase in SLEEPING_PHASES

    @property
    def is_awake(self) -> bool:
        return self.phase == AWAKE

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "drive": round(self.drive, 4),
            "delay_gate": round(self.delay_gate, 4),
            "delay_reason": self.delay_reason,
            "quality": round(self.quality, 3),
            "awakenings": self.awakenings,
            "postponed_sec": round(max(0.0, time.time() - self.postponed_since_ts)
                                   if self.postponed_since_ts else 0.0, 1),
            "is_asleep": self.is_asleep,
        }


class SleepManager:
    """Автомат режима сна. Опрашивается раз в 30 секунд.

    Не знает про Telegram и LLM: получает числа, возвращает решение.
    """

    def __init__(self, db: sqlite3.Connection | None = None) -> None:
        self.db = db
        self._snap = SleepSnapshot(phase_entered_ts=time.time())
        self._ensure_table()

    def _ensure_table(self) -> None:
        if self.db is None:
            return
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS sleep_state (
                   id INTEGER PRIMARY KEY CHECK (id = 1),
                   phase TEXT NOT NULL DEFAULT 'awake',
                   phase_entered_ts REAL NOT NULL DEFAULT 0.0,
                   postponed_since_ts REAL NOT NULL DEFAULT 0.0,
                   target_duration_sec REAL NOT NULL DEFAULT 2400.0,
                   computed_quality REAL NOT NULL DEFAULT 1.0,
                   awakenings INTEGER NOT NULL DEFAULT 0,
                   pre_sleep_s REAL NOT NULL DEFAULT 0.62,
                   pre_sleep_n REAL NOT NULL DEFAULT 0.38,
                   pre_sleep_d REAL NOT NULL DEFAULT 0.60,
                   updated_ts REAL NOT NULL DEFAULT 0.0
               )"""
        )
        self.db.commit()
        row = self.db.execute(
            "SELECT phase, phase_entered_ts, postponed_since_ts,"
            " target_duration_sec, computed_quality, awakenings, updated_ts"
            " FROM sleep_state WHERE id=1").fetchone()
        if row:
            self._snap = SleepSnapshot(
                phase=row[0], phase_entered_ts=row[1],
                postponed_since_ts=row[2], target_duration=row[3],
                quality=row[4], awakenings=row[5], updated_ts=row[6])

    @property
    def snapshot(self) -> SleepSnapshot:
        return self._snap

    # -------------------------------------------------------- давление

    @staticmethod
    def sleep_drive(*, fatigue: float, load: float = 0.0,
                    depletion: float = 0.0, sleep_debt: float = 0.0,
                    hour_value: float | None = None) -> float:
        """P_sleep: хочет ли спать. Числа — из 09-sleep-mental/REVIEW.md."""
        h = clock.hour() if hour_value is None else hour_value
        c = circadian(h)
        mental = 0.6 * clamp(load, 0.0, 1.0) + 0.4 * clamp(depletion, 0.0, 1.0)
        # буст стартует с 0.92, а не с порога критической усталости:
        # при F=0.99 он даёт +0.13 и вырубает даже днём
        emergency = 0.15 * max(0.0, (fatigue - BOOST_FROM_FATIGUE)
                               / max(1e-6, 1.0 - BOOST_FROM_FATIGUE))
        return clamp(
            0.55 * clamp(fatigue, 0.0, 1.0)
            + 0.20 * c
            + 0.15 * clamp(sleep_debt, 0.0, 1.0)
            + 0.10 * mental
            + emergency,
            0.0, 1.5)

    # ----------------------------------------------------------- гейт

    @staticmethod
    def delay_gate(*, chat_pressure: float = 0.0, wait_pressure: float = 0.0,
                   task_pressure: float = 0.0, arousal: float = 0.0,
                   revenge: float = 0.0,
                   postponed_sec: float = 0.0) -> tuple[float, str]:
        """I_delay: почему нельзя лечь прямо сейчас.

        Отличие от исходной архитектуры: экспонента затухания касается
        ТОЛЬКО социальных причин (разговор, ожидание, прокрастинация).
        Физиологические — высокий норадреналин и незакрытая задача —
        от времени не слабеют: если N высок, она не уснёт, сколько ни
        жди.
        """
        social = max(chat_pressure, wait_pressure, revenge)
        decayed = social * math.exp(-max(0.0, postponed_sec) / TAU_EXHAUST)
        physio = max(task_pressure, arousal)
        gate = max(decayed, physio)

        if gate <= 0.0:
            # ничего не держит — и не важно, какая причина «могла бы» быть
            return 0.0, "none"

        if physio >= decayed and physio > 0:
            reason = "arousal" if arousal >= task_pressure else "task"
        elif wait_pressure >= max(chat_pressure, revenge):
            reason = "awaiting_reply"
        elif chat_pressure >= revenge:
            reason = "dialogue"
        elif revenge > 0:
            reason = "revenge"
        else:
            reason = "none"
        return clamp(gate, 0.0, 1.0), reason

    # ----------------------------------------------------------- тик

    def tick(self, *, fatigue: float, load: float = 0.0,
             depletion: float = 0.0, sleep_debt: float = 0.0,
             rumination: float = 0.0, noradrenaline: float = 0.38,
             chat_pressure: float = 0.0, wait_pressure: float = 0.0,
             task_pressure: float = 0.0, revenge: float = 0.0,
             hour_value: float | None = None,
             now: float | None = None) -> SleepSnapshot:
        """Один шаг автомата. Возвращает новое состояние.

        Вызывается раз в 30 секунд из планировщика. Побочных эффектов
        нет — решение принимает вызывающий код по возвращённой фазе.
        """
        stamp = now if now is not None else time.time()
        s = self._snap
        if hour_value is None:
            hour_value = clock.hour(stamp)

        drive = self.sleep_drive(
            fatigue=fatigue, load=load, depletion=depletion,
            sleep_debt=sleep_debt, hour_value=hour_value)

        arousal = clamp((noradrenaline - HYPERAROUSAL_N)
                        / max(1e-6, 1.0 - HYPERAROUSAL_N), 0.0, 1.0)

        # мстительная прокрастинация: возврат контроля над своим временем
        revenge_val = 0.0
        postponed = (max(0.0, stamp - s.postponed_since_ts)
                     if s.postponed_since_ts else 0.0)
        if postponed < REVENGE_WINDOW:
            revenge_val = clamp(0.55 * (load * depletion), 0.0, 1.0)
        revenge_total = max(revenge, revenge_val)

        gate, reason = self.delay_gate(
            chat_pressure=chat_pressure, wait_pressure=wait_pressure,
            task_pressure=task_pressure, arousal=arousal,
            revenge=revenge_total, postponed_sec=postponed)

        phase = s.phase
        phase_entered = s.phase_entered_ts
        postponed_since = s.postponed_since_ts

        if phase == AWAKE:
            wants = drive >= SLEEP_THRESHOLD and fatigue >= MIN_FATIGUE
            if wants:
                phase = WIND_DOWN
                phase_entered = stamp
                postponed_since = stamp
                log.info("хочет спать: drive=%.2f, гейт=%.2f (%s)",
                         drive, gate, reason)

        elif phase == WIND_DOWN:
            elapsed = stamp - phase_entered
            hard_cap = postponed >= MAX_DELAY
            critical = fatigue >= CRITICAL_FATIGUE and drive >= CRITICAL_PRESSURE
            if gate < DELAY_RELEASE or hard_cap or critical:
                phase = NREM_SWS
                phase_entered = stamp
                if critical:
                    log.warning("критическая усталость — срыв в сон")
                elif hard_cap:
                    log.warning("откладывала %.0f мин — предел", postponed / 60)

        elif phase in SLEEPING_PHASES:
            phase = self._advance_sleep_phase(s, stamp, sleep_debt)

        elif phase == WAKE_INERTIA:
            if stamp - phase_entered >= INERTIA_EXTRA:
                phase = AWAKE
                phase_entered = stamp
                postponed_since = 0.0
                log.info("проснулась")

        # ----------------------------------------------------- качество
        quality = s.quality
        if phase == NREM_SWS and s.phase != NREM_SWS:
            quality = self.compute_quality(
                rumination=rumination, load=load, arousal=arousal,
                awakenings=s.awakenings)

        updated = replace(
            s, phase=phase, phase_entered_ts=phase_entered,
            postponed_since_ts=postponed_since, drive=drive,
            delay_gate=gate, delay_reason=reason, quality=quality,
            updated_ts=stamp)

        if phase == NREM_SWS and s.phase == WIND_DOWN:
            updated = replace(updated, target_duration=self.core_duration(
                sleep_debt))

        self._snap = updated
        self._persist(updated, stamp)
        return updated

    def _advance_sleep_phase(self, s: SleepSnapshot, now: float,
                             sleep_debt: float) -> str:
        """NREM -> REM -> WAKE_INERTIA по долям ядра."""
        core = s.target_duration or self.core_duration(sleep_debt)
        elapsed = now - s.phase_entered_ts
        nrem_len = core * PHASE_SHARES[NREM_SWS]
        rem_len = core * PHASE_SHARES[REM]

        if s.phase == NREM_SWS and elapsed >= nrem_len:
            log.info("сон: NREM -> REM")
            return REM
        if s.phase == REM and elapsed >= rem_len:
            log.info("сон: REM -> пробуждение")
            return WAKE_INERTIA
        return s.phase

    @staticmethod
    def core_duration(sleep_debt: float) -> float:
        """Ядро сна: 40 минут, дольше при долге сна (до 54)."""
        return CORE_SECONDS * (1.0 + 0.35 * clamp(sleep_debt, 0.0, 1.0))

    @staticmethod
    def compute_quality(*, rumination: float, load: float, arousal: float,
                        awakenings: int = 0,
                        deprived_days: int = 0) -> float:
        """Качество ночи. Ниже 0.2 не опускается — иначе выхода нет."""
        q0 = clamp(1.0 - 0.30 * clamp(rumination, 0.0, 1.0)
                   - 0.20 * clamp(load, 0.0, 1.0)
                   - 0.25 * clamp(arousal, 0.0, 1.0), 0.20, 1.0)
        q = q0 * (0.85 ** max(0, deprived_days)) - 0.12 * max(0, awakenings)
        return clamp(q, 0.20, 1.0)

    def awaken(self, *, now: float | None = None) -> SleepSnapshot:
        """Разбудили среди сна. Штраф к качеству и счётчик."""
        stamp = now if now is not None else time.time()
        s = self._snap
        q = clamp(s.quality - 0.12, 0.20, 1.0)
        updated = replace(s, phase=FRAGMENTED_WAKE, phase_entered_ts=stamp,
                          awakenings=s.awakenings + 1, quality=q,
                          updated_ts=stamp)
        self._snap = updated
        self._persist(updated, stamp)
        log.info("пробуждение #%d, качество сна -> %.2f", updated.awakenings, q)
        return updated

    def resume_sleep(self, *, now: float | None = None) -> SleepSnapshot:
        """После прерванного пробуждения — обратно досыпать."""
        stamp = now if now is not None else time.time()
        s = self._snap
        updated = replace(s, phase=NREM_SWS, phase_entered_ts=stamp,
                          updated_ts=stamp)
        self._snap = updated
        self._persist(updated, stamp)
        return updated

    def wake_up(self, *, now: float | None = None) -> SleepSnapshot:
        """Окончательное пробуждение (после инерции)."""
        stamp = now if now is not None else time.time()
        s = self._snap
        updated = replace(s, phase=AWAKE, phase_entered_ts=stamp,
                          postponed_since_ts=0.0, updated_ts=stamp)
        self._snap = updated
        self._persist(updated, stamp)
        return updated

    # ----------------------------------------------------- обслуживание

    def should_queue_message(self, *, chat_id: int, owner_id: int = 0,
                             burst: bool = False) -> bool:
        """Копить ли сообщение до утра.

        Во сне — да. Исключение: владелец, который зовёт настойчиво.
        """
        s = self._snap
        if s.phase in SLEEPING_PHASES:
            if owner_id and chat_id == owner_id and burst:
                return False        # разбудил — пропускаем
            return True
        if s.phase == WAKE_INERTIA:
            return True
        return False

    def _persist(self, snap: SleepSnapshot, now: float) -> None:
        if self.db is None:
            return
        self.db.execute(
            """INSERT INTO sleep_state
                   (id, phase, phase_entered_ts, postponed_since_ts,
                    target_duration_sec, computed_quality, awakenings,
                    updated_ts)
               VALUES (1,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                    phase=excluded.phase,
                    phase_entered_ts=excluded.phase_entered_ts,
                    postponed_since_ts=excluded.postponed_since_ts,
                    target_duration_sec=excluded.target_duration_sec,
                    computed_quality=excluded.computed_quality,
                    awakenings=excluded.awakenings,
                    updated_ts=excluded.updated_ts""",
            (snap.phase, snap.phase_entered_ts, snap.postponed_since_ts,
             snap.target_duration, snap.quality, snap.awakenings, now))
        self.db.commit()


def sleep_directive(snap: SleepSnapshot, *, fatigue: float = 0.0) -> str:
    """Телесная директива для фазы сна. Без клинических слов."""
    if snap.phase == WIND_DOWN:
        return ("- засыпает на ходу (мана на нуле): пафос ВЫКЛЮЧЕН. "
                "Глаза слипаются, фразы обрывочные, одна-две короткие "
                "строчки. Никаких тирад и титулов.")
    if snap.phase == WAKE_INERTIA:
        return ("- только проснулась (сонная инерция): ворчливая, сонная, "
                "соображает туго. Пафос ещё не проснулся — отвечает "
                "коротко и сухо, хочет обратно под одеяло или есть.")
    if snap.phase == FRAGMENTED_WAKE:
        return ("- разбудили среди ночи: дезориентация и раздражение. "
                "Отвечает резко и коротко, без развёрнутых фраз.")
    return ""
