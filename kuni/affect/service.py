"""Сервис аффекта: связывает движок, память и безопасность.

Центральное место интеграции. Задачи:

  * хранить состояние между перезапусками (таблица neuro_state);
  * превращать входящую реплику в AppraisalImpulse;
  * закреплять аффективную подпись при записи в память;
  * отдавать ecphory-поиск с mood-контентом;
  * проверять действия предохранителем;
  * вести привязанность (w_rel) по чатам.

Ничего не решает про LLM — только числа и вызовы памяти.
"""
from __future__ import annotations

import json
import logging
import math
import sqlite3
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ..affect.appraisal import AppraisalVector
from ..affect.social import (RuminationEngine, RuminationState,
                             SocialPainSnapshot, SocialPainTracker)
from ..affect.engine import (EngineConfig, NeuromodulatoryEngine, project_pad)
from ..affect.mental import MentalEngine, MentalSnapshot, mental_directive
from ..affect.modulation import InferenceModulator, ModulationConfig
from ..affect.sleep import (CommitmentTracker, SleepManager, SleepSnapshot,
                            sleep_directive)
from ..affect.safety import (BreakerConfig, BreakerDecision,
                             MetacognitiveCircuitBreaker)
from ..affect.types import (OBSERVATION, SELF_INFERENCE, TEACHING,
                            AffectSnapshot, AppraisalImpulse, Homeostasis,
                            Monoamines, PAD, clamp)
from ..memory.ecphory import EcphoryConfig, EcphoryHit, ecphory_retrieve

log = logging.getLogger(__name__)


@dataclass
class AffectConfig:
    """Настройки сервиса — читаются из [affect] в config.toml."""

    enabled: bool = True
    engine: EngineConfig = None  # type: ignore[assignment]
    modulation: ModulationConfig = None  # type: ignore[assignment]
    breaker: BreakerConfig = None  # type: ignore[assignment]
    ecphory: EcphoryConfig = None  # type: ignore[assignment]

    # закрепление подписи: насколько сильно эмоция пишется в память
    pad_bind_weight: float = 0.6

    # привязанность
    w_rel_base: float = 1.0
    w_rel_owner: float = 2.5
    w_rel_gain: float = 0.05
    w_rel_max: float = 3.0
    w_rel_min: float = 0.3

    # counterweight
    counterweight_tau_sec: float = 900.0

    def __post_init__(self) -> None:
        if self.engine is None:
            self.engine = EngineConfig()
        if self.modulation is None:
            self.modulation = ModulationConfig()
        if self.breaker is None:
            self.breaker = BreakerConfig()
        if self.ecphory is None:
            self.ecphory = EcphoryConfig()


class AffectService:
    """Живое состояние аффекта + его влияние на память и генерацию."""

    def __init__(self, db_path: Path, *, config: AffectConfig | None = None,
                 bus: Any = None, owner_chat_id: int = 0) -> None:
        self.cfg = config or AffectConfig()
        self.bus = bus
        self.owner_chat_id = owner_chat_id
        # схема должна существовать: сервис может открыть и чистую базу
        from ..memory.schema import connect as _connect
        self.db: sqlite3.Connection = _connect(Path(db_path))
        self.engine = NeuromodulatoryEngine(self.cfg.engine, bus=bus)
        self.modulator = InferenceModulator(self.cfg.modulation)
        self.breaker = MetacognitiveCircuitBreaker(self.cfg.breaker,
                                                  engine=self.engine, bus=bus)
        self._negative_since: float | None = None
        self._last_tick = time.time()

        # Социальная боль (Уильямс) и руминация — отдельные автоматы.
        # В engine живёт непрерывная биохимия; здесь — когнитивные
        # состояния со своими правилами перехода. Смешивать нельзя.
        self.social = SocialPainTracker()
        self.rumination_engine = RuminationEngine()
        self._social_states: dict[int, SocialPainSnapshot] = {}
        self._rumination: dict[int, RuminationState] = {}
        self._last_appraisal: AppraisalVector | None = None

        # Ментальный слой и режим сна. Оба автономны: считают по числам,
        # про Telegram и LLM не знают. Живут в той же базе, что и аффект.
        self.mental = MentalEngine(self.db)
        self.sleep = SleepManager(self.db)
        self.commitments = CommitmentTracker()

        self._load_state()
        self._ensure_social_tables()
        self._load_rumination()

    # -------------------------------------------------------- соц. боль

    def _ensure_social_tables(self) -> None:
        """Таблицы социальной боли и следов руминации."""
        self.db.executescript("""
CREATE TABLE IF NOT EXISTS social_pain_states (
    chat_id INTEGER PRIMARY KEY,
    need_belong REAL NOT NULL DEFAULT 0.75,
    need_esteem REAL NOT NULL DEFAULT 0.75,
    need_control REAL NOT NULL DEFAULT 0.75,
    need_meaning REAL NOT NULL DEFAULT 0.75,
    pain_index REAL NOT NULL DEFAULT 0.0,
    stage TEXT NOT NULL DEFAULT 'none',
    stage_entered_ts REAL NOT NULL DEFAULT 0.0,
    unanswered_streak INTEGER NOT NULL DEFAULT 0,
    is_muted INTEGER NOT NULL DEFAULT 0,
    last_event TEXT NOT NULL DEFAULT '',
    repeats INTEGER NOT NULL DEFAULT 0,
    updated_ts REAL NOT NULL DEFAULT 0.0
);
CREATE TABLE IF NOT EXISTS rumination_traces (
    trace_id TEXT PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    source_episode_id TEXT NOT NULL DEFAULT '',
    trigger_summary TEXT NOT NULL DEFAULT '',
    current_charge REAL NOT NULL,
    mode_rho REAL NOT NULL DEFAULT 0.0,
    created_ts REAL NOT NULL,
    updated_ts REAL NOT NULL,
    resolved TEXT NOT NULL DEFAULT '',
    author_id INTEGER NOT NULL DEFAULT 0,
    message_id INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_rum_active
    ON rumination_traces(resolved, current_charge DESC);
""")
        self.db.commit()
        self._load_social_states()

    def _load_social_states(self) -> None:
        try:
            rows = self.db.execute(
                "SELECT chat_id, need_belong, need_esteem, need_control, "
                "need_meaning, pain_index, stage, stage_entered_ts, "
                "unanswered_streak, is_muted, last_event, repeats, updated_ts "
                "FROM social_pain_states").fetchall()
        except sqlite3.Error:
            return
        for row in rows:
            self._social_states[int(row[0])] = SocialPainSnapshot(
                chat_id=int(row[0]), need_belong=row[1], need_esteem=row[2],
                need_control=row[3], need_meaning=row[4], pain_index=row[5],
                stage=row[6] or "none", stage_entered_ts=row[7],
                unanswered_streak=int(row[8] or 0), is_muted=bool(row[9]),
                last_event=row[10] or "", repeats=int(row[11] or 0),
                updated_ts=row[12] or 0.0,
            )

    def social_state(self, chat_id: int) -> SocialPainSnapshot:
        state = self._social_states.get(chat_id)
        if state is None:
            state = SocialPainSnapshot(chat_id=chat_id)
            self._social_states[chat_id] = state
        return state

    def _persist_social(self, state: SocialPainSnapshot) -> None:
        self._social_states[state.chat_id] = state
        self.db.execute(
            """INSERT INTO social_pain_states
               (chat_id, need_belong, need_esteem, need_control, need_meaning,
                pain_index, stage, stage_entered_ts, unanswered_streak,
                is_muted, last_event, repeats, updated_ts)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(chat_id) DO UPDATE SET
                 need_belong=excluded.need_belong,
                 need_esteem=excluded.need_esteem,
                 need_control=excluded.need_control,
                 need_meaning=excluded.need_meaning,
                 pain_index=excluded.pain_index,
                 stage=excluded.stage,
                 stage_entered_ts=excluded.stage_entered_ts,
                 unanswered_streak=excluded.unanswered_streak,
                 is_muted=excluded.is_muted,
                 last_event=excluded.last_event,
                 repeats=excluded.repeats,
                 updated_ts=excluded.updated_ts""",
            (state.chat_id, state.need_belong, state.need_esteem,
             state.need_control, state.need_meaning, state.pain_index,
             state.stage, state.stage_entered_ts, state.unanswered_streak,
             int(state.is_muted), state.last_event, state.repeats,
             state.updated_ts),
        )
        self.db.commit()

    def apply_social_event(self, chat_id: int, kind: str, vec: AppraisalVector,
                           *, witnesses: int = 0,
                           now: float | None = None) -> SocialPainSnapshot:
        """Событие отвержения: мут, исключение, игнор, извинение."""
        state = self.social_state(chat_id)
        state = self.social.apply_event(state, vec, kind,
                                        witnesses=witnesses, now=now)
        self._persist_social(state)
        if self.bus is not None:
            self.bus.emit("social.pain", **state.to_dict())
        return state

    def register_silence(self, chat_id: int, now: float | None = None) -> None:
        """Она решила промолчать (#wait). Это ход, а не ошибка.

        28.10: молчание — валидная часть переписки. Но оно должно
        чувствоваться: если молчишь, когда к тебе обратились, остаётся
        лёгкий след — она «сдержалась». Влияет слабо, чтобы молчание
        не превращалось в наказание себя.
        """
        try:
            state = self.emotions.state if hasattr(self, "emotions") else None
            if state is not None and hasattr(state, "valence"):
                log.debug("тишина в чате %s (v=%.2f)", chat_id, state.valence)
        except Exception:
            log.debug("тишина не отразилась в эмоциях", exc_info=True)

    def repair_social(self, chat_id: int, kind: str = "unmuted",
                      now: float | None = None) -> SocialPainSnapshot | None:
        """Права вернулись. Снять залипший флаг мута и подлечить потребности.

        28.10: флаг is_muted ставился при ошибке отправки и НЕ снимался,
        когда владелец возвращал права. Она продолжала считать себя
        замьюченной и жаловалась на это в каждом ответе, хотя писала
        уже свободно. Успешная отправка — достаточное доказательство,
        что права есть.
        """
        if not chat_id:
            return None
        stamp = now if now is not None else time.time()
        state = self.social_state(chat_id)
        if not state.is_muted and state.stage == "none":
            return state
        state = self.social.repair(state, kind, now=stamp)
        self._persist_social(state)
        log.info("соц. состояние чата %s восстановлено (%s): muted=%s stage=%s",
                 chat_id, kind, state.is_muted, state.stage)
        if self.bus is not None:
            self.bus.emit("social.repaired", chat_id=chat_id, kind=kind,
                          **state.to_dict())
        return state

    def tick_social(self, dt_sec: float, *, after_sleep: bool = False,
                    now: float | None = None) -> None:
        """Затухание социальной боли и сдвиг стадий по времени."""
        for chat_id, state in list(self._social_states.items()):
            new = self.social.step_decay(state, dt_sec, now=now,
                                         after_sleep=after_sleep)
            if new != state:
                self._persist_social(new)

    # --------------------------------------------------------- руминация

    def spawn_rumination(self, chat_id: int, vec: AppraisalVector, *,
                         social_pain: float = 0.0, breaker_blocked: bool = False,
                         emotion_intensity: float = 0.0, summary: str = "",
                         author_id: int = 0, message_id: int = 0,
                         now: float | None = None) -> RuminationState | None:
        """Родить след пережёвывания, если событие того стоит."""
        if not self.rumination_engine.should_spawn(
                vec, social_pain=social_pain,
                breaker_blocked=breaker_blocked,
                emotion_intensity=emotion_intensity):
            return None
        stamp = now if now is not None else time.time()
        charge = self.rumination_engine.initial_charge(
            vec, breaker_blocked=breaker_blocked)
        if charge < 0.15:
            return None
        trace_id = f"rum_{int(stamp)}_{chat_id}"
        state = RuminationState(
            trace_id=trace_id, chat_id=chat_id,
            trigger_summary=summary[:300], current_charge=charge,
            created_ts=stamp, updated_ts=stamp,
            author_id=author_id, message_id=message_id,
        )
        # Три оскорбления подряд не должны порождать три независимых
        # следа, из которых выживает последний: новое событие усиливает
        # существующую обиду. Иначе счётчик идёт в никуда, а заряд
        # сбрасывается к начальному.
        existing = self._rumination.get(chat_id)
        if existing is not None and existing.active:
            merged = max(existing.current_charge, charge)
            merged = min(1.0, merged + 0.25 * min(existing.current_charge, charge))
            state = RuminationState(
                trace_id=existing.trace_id, chat_id=chat_id,
                source_episode_id=existing.source_episode_id,
                trigger_summary=existing.trigger_summary or summary[:300],
                current_charge=round(merged, 4),
                mode_rho=existing.mode_rho,
                created_ts=existing.created_ts, updated_ts=stamp,
                author_id=author_id or existing.author_id,
                message_id=message_id or existing.message_id,
            )
        self._rumination[chat_id] = state
        self._persist_rumination(state)
        if self.bus is not None:
            self.bus.emit("rumination.spawned", **state.to_dict())
        log.info("руминация: заряд %.2f в чате %s — %s",
                 charge, chat_id, summary[:80])
        return state

    def _load_rumination(self) -> None:
        """Поднять активные следы из базы после рестарта.

        Без этого обида жила только в памяти процесса: перезапуск стирал
        её полностью, и «пережёвывает три часа» превращалось в «забыла
        при первом же рестарте».
        """
        try:
            rows = self.db.execute(
                "SELECT trace_id, chat_id, source_episode_id, trigger_summary, "
                "current_charge, mode_rho, created_ts, updated_ts, resolved, "
                "author_id, message_id FROM rumination_traces "
                "WHERE resolved='' AND current_charge >= ? ORDER BY updated_ts DESC",
                (0.15,)).fetchall()
        except sqlite3.Error:
            return
        for row in rows:
            state = RuminationState(
                trace_id=row[0], chat_id=int(row[1]),
                source_episode_id=row[2] or "", trigger_summary=row[3] or "",
                current_charge=row[4], mode_rho=row[5] or 0.0,
                created_ts=row[6] or 0.0, updated_ts=row[7] or 0.0,
                resolved=row[8] or "", author_id=int(row[9] or 0),
                message_id=int(row[10] or 0),
            )
            # один активный след на чат: берём самый свежий
            self._rumination.setdefault(state.chat_id, state)

    def rumination(self, chat_id: int) -> RuminationState | None:
        state = self._rumination.get(chat_id)
        return state if state is not None and state.active else None

    def step_rumination(self, dt_sec: float,
                        now: float | None = None) -> tuple[float, float]:
        """Шаг руминации: распад заряда + тонический дрейф в моноамины."""
        snap = self.engine.snapshot()
        ds_total = dn_total = 0.0
        coping = 0.5
        if self._last_appraisal is not None:
            coping = self._last_appraisal.coping
        for chat_id, state in list(self._rumination.items()):
            if not state.active:
                continue
            state = self.rumination_engine.step(
                state, dt_sec, s=snap.mono.s, n=snap.mono.n,
                coping=coping, now=now)
            self._rumination[chat_id] = state
            self._persist_rumination(state)
            ds, dn = self.rumination_engine.drift(
                state, dt_sec, s_current=snap.mono.s)
            ds_total += ds
            dn_total += dn
        return ds_total, dn_total

    # ------------------------------------------------- симуляция травмы

    #: Готовые сценарии: (тип события, текст-триггер, число свидетелей).
    SIM_SCENARIOS: dict[str, tuple[str, str, int]] = {
        "fuck_off_dm": (
            "insult", "ты меня достала, пошла нахуй отсюда, не пиши мне больше", 1),
        "fuck_off_public": (
            "humiliation", "какая же ты жалкая, всем смешно с тебя", 5),
        "group_mute": ("muted", "тебя лишили права писать", 4),
        "group_ban": ("kicked", "тебя выгнали из группы", 6),
        "ghosting_streak": ("ignored", "она написала трижды и никто не ответил", 0),
        "apology_repair": ("apology", "прости меня, я был не прав, ты важна", 0),
    }

    def simulate_social_event(
        self,
        scenario: str,
        *,
        chat_id: int,
        actor_id: int = 0,
        actor_name: str = "Собеседник",
        custom_text: str | None = None,
        witnesses: int | None = None,
        now: float | None = None,
        apply_to_engine: bool = True,
    ) -> dict[str, Any]:
        """Подбросить социальное событие по полному боевому контуру.

        Зачем: проверить, как она переживёт отвержение, не устраивая
        реальную травлю в настоящих чатах. Событие идёт тем же путём,
        что и живое: appraisal -> моноамины -> социальная боль ->
        руминация -> запись в базу. Отличается только источник.

        Сценарии:
            fuck_off_dm        грубый посыл в личке от близкого
            fuck_off_public    публичное унижение при свидетелях
            group_mute         лишение права писать
            group_ban          исключение из группы
            ghosting_streak    три сообщения без ответа
            apology_repair     искреннее извинение (проверка исцеления)

        Возвращает снимок: состояние до/после, боль, стадия, руминация.
        """
        from .appraisal import FastAppraisalExtractor

        if scenario not in self.SIM_SCENARIOS:
            raise ValueError(
                f"неизвестный сценарий: {scenario} "
                f"(есть: {', '.join(sorted(self.SIM_SCENARIOS))})")

        kind, default_text, default_witnesses = self.SIM_SCENARIOS[scenario]
        text = custom_text or default_text
        witness_count = default_witnesses if witnesses is None else witnesses
        stamp = now if now is not None else time.time()

        before_mono = self.engine.mono
        before_social = self.social_state(chat_id)

        extractor = FastAppraisalExtractor()
        try:
            w_rel = self.relation_weight(chat_id)
        except Exception:
            w_rel = 1.0
        # у владельца привязанность максимальная: предательство близкого
        # переживается тяжелее, чем хамство случайного человека
        if actor_id and actor_id == self.owner_chat_id:
            w_rel = max(w_rel, 2.5)

        if kind in ("muted", "kicked", "ignored", "apology"):
            vec = extractor.extract_from_system_event(
                kind, w_rel=w_rel, witnesses=witness_count)
        else:
            vec = extractor.extract_from_message(
                text, w_rel=w_rel, chat_type="private",
                witnesses=witness_count)

        # 1. соматика: тот же путь, что у живого сообщения
        if apply_to_engine:
            self.apply_appraisal(vec, chat_id=chat_id, user_id=actor_id,
                                 source=f"sim.{scenario}")

        # 2. социальные потребности
        social_kind = "muted" if kind == "muted" else (
            "kicked" if kind == "kicked" else (
                "ignored" if kind == "ignored" else (
                    "apology" if kind == "apology" else kind)))
        if social_kind == "humiliation":
            social_kind = "humiliation"
        pain = self.apply_social_event(
            chat_id, social_kind, vec, witnesses=witness_count, now=stamp)

        # 3. руминация: извинение лечит, остальное оставляет след
        trace = None
        if kind == "apology":
            existing = self._rumination.get(chat_id)
            if existing is not None and existing.active:
                relieved = self.rumination_engine.apology_relief(
                    existing, now=stamp)
                self._rumination[chat_id] = relieved
                self._persist_rumination(relieved)
                trace = relieved
        else:
            trace = self.spawn_rumination(
                chat_id, vec, social_pain=pain.pain_index,
                summary=f"[симуляция] {text[:200]}",
                author_id=actor_id, message_id=0, now=stamp)

        after_mono = self.engine.mono
        result = {
            "scenario": scenario,
            "event": kind,
            "text": text,
            "witnesses": witness_count,
            "chat_id": chat_id,
            "actor_id": actor_id,
            "actor_name": actor_name,
            "mono_before": {"s": round(before_mono.s, 3),
                            "n": round(before_mono.n, 3),
                            "d": round(before_mono.d, 3)},
            "mono_after": {"s": round(after_mono.s, 3),
                           "n": round(after_mono.n, 3),
                           "d": round(after_mono.d, 3)},
            "mono_delta": {
                "ds": round(after_mono.s - before_mono.s, 3),
                "dn": round(after_mono.n - before_mono.n, 3),
                "dd": round(after_mono.d - before_mono.d, 3),
            },
            "pad": {"p": round(self.engine.pad.p, 3),
                    "a": round(self.engine.pad.a, 3),
                    "dom": round(self.engine.pad.dom, 3)},
            "pain_before": round(before_social.pain_index, 3),
            "pain_after": round(pain.pain_index, 3),
            "stage": pain.stage,
            "cluster": pain.cluster(),
            "needs": pain.to_dict()["needs"],
            "deficits": pain.to_dict()["deficits"],
            "appraisal": vec.to_dict(),
            "rumination": trace.to_dict() if trace is not None else None,
            "simulated": True,
            "ts": stamp,
        }
        if self.bus is not None:
            self.bus.emit("social.simulated", **{
                k: result[k] for k in ("scenario", "event", "chat_id",
                                       "pain_after", "stage", "cluster")})
        log.warning(
            "СИМУЛЯЦИЯ | %s | чат %s | боль %.2f->%.2f | стадия %s | "
            "кластер %s | dS=%+.2f dN=%+.2f dD=%+.2f | руминация %s",
            scenario, chat_id, before_social.pain_index, pain.pain_index,
            pain.stage, pain.cluster(),
            after_mono.s - before_mono.s, after_mono.n - before_mono.n,
            after_mono.d - before_mono.d,
            f"{trace.current_charge:.2f}" if trace else "нет")
        return result

    def _persist_rumination(self, state: RuminationState) -> None:
        self.db.execute(
            """INSERT INTO rumination_traces
               (trace_id, chat_id, source_episode_id, trigger_summary,
                current_charge, mode_rho, created_ts, updated_ts, resolved,
                author_id, message_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(trace_id) DO UPDATE SET
                 current_charge=excluded.current_charge,
                 mode_rho=excluded.mode_rho,
                 updated_ts=excluded.updated_ts,
                 resolved=excluded.resolved""",
            (state.trace_id, state.chat_id, state.source_episode_id,
             state.trigger_summary, state.current_charge, state.mode_rho,
             state.created_ts, state.updated_ts, state.resolved,
             state.author_id, state.message_id),
        )
        self.db.commit()

    # ------------------------------------------------------------ состояние

    def _load_state(self) -> None:
        row = self.db.execute(
            "SELECT ts, s, n, d, pad_p, pad_a, pad_dom, fatigue, "
            "cognitive_load, social_satiety, rumination_since, "
            "counterweight_active FROM neuro_state WHERE id=1").fetchone()
        if not row:
            return
        ts, s, n, d, p, a, dom, fatigue, load, satiety, neg_since, cw = row
        self.engine.set_state(s=s, n=n, d=d)
        self.engine.set_homeostasis(fatigue=fatigue, cognitive_load=load,
                                    social_satiety=satiety)
        self._negative_since = neg_since
        self._last_tick = ts or time.time()
        log.info("аффект восстановлен: S=%.2f N=%.2f D=%.2f fatigue=%.2f",
                 s, n, d, fatigue)

    def persist_state(self) -> None:
        snap = self.engine.snapshot()
        mono, h, pad = snap.mono, snap.homeostasis, snap.pad
        self.db.execute(
            """INSERT INTO neuro_state
               (id, ts, s, n, d, pad_p, pad_a, pad_dom, fatigue,
                cognitive_load, social_satiety, rumination_since,
                counterweight_active)
               VALUES (1,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                   ts=excluded.ts, s=excluded.s, n=excluded.n, d=excluded.d,
                   pad_p=excluded.pad_p, pad_a=excluded.pad_a,
                   pad_dom=excluded.pad_dom, fatigue=excluded.fatigue,
                   cognitive_load=excluded.cognitive_load,
                   social_satiety=excluded.social_satiety,
                   rumination_since=excluded.rumination_since,
                   counterweight_active=excluded.counterweight_active""",
            (snap.ts, mono.s, mono.n, mono.d, pad.p, pad.a, pad.dom,
             h.fatigue, h.cognitive_load, h.social_satiety,
             self._negative_since, int(snap.counterweight_active)),
        )
        self.db.commit()

    @property
    def snapshot(self) -> AffectSnapshot:
        return self.engine.snapshot()

    def tick(self, now: float | None = None) -> AffectSnapshot:
        """Продвинуть время: клиренс моноаминов и гомеостаз.

        Потолок dt обязателен. Раньше после суток простоя сюда приходило
        dt = 86400, и RK4 с сублишагом 1 с прокручивал 86 400 итераций
        синхронно в обработчике входящего сообщения — 1.9 с на сутки,
        23.6 с на неделю. На недельном простое это ещё и смывало
        накопленный дрейф руминации в ноль, и обида исчезала ровно в
        момент генерации ответа. За один вызов не больше 10 минут:
        остальное догоняется фоновым тиком каждые 5 минут.
        """
        stamp = now if now is not None else time.time()
        dt = max(0.0, stamp - self._last_tick)
        self._last_tick = stamp
        dt = min(dt, 600.0)
        if dt > 0:
            self.engine.step(dt, now=stamp)
        self._update_negative_since(stamp)
        self.persist_state()
        return self.engine.snapshot(stamp)

    def _update_negative_since(self, now: float) -> None:
        """Отслеживать длительность отрицательного состояния."""
        pleasure = self.engine.pad.p
        if pleasure < -self.cfg.breaker.rage_s_lt - 0.2:
            if self._negative_since is None:
                self._negative_since = now
        else:
            self._negative_since = None

    def counterweight_active(self, now: float | None = None) -> bool:
        stamp = now if now is not None else time.time()
        pad = self.engine.pad
        s = self.engine.mono.s
        from ..memory.ecphory import counterweight_active
        return counterweight_active(
            (pad.p, pad.a, pad.dom), s, self._negative_since, stamp,
            self.cfg.ecphory, self.cfg.counterweight_tau_sec)

    # ------------------------------------------------------------ привязанность

    def relation_weight(self, chat_id: int) -> float:
        """w_rel: близость человека. Владелец по умолчанию близок."""
        if chat_id and chat_id == self.owner_chat_id:
            return self.cfg.w_rel_owner
        row = self.db.execute(
            "SELECT w_rel FROM relation_weights WHERE chat_id=?",
            (chat_id,)).fetchone()
        if row:
            return float(row[0])
        return self.cfg.w_rel_base

    def update_relation(self, chat_id: int, *, delta: float,
                        valence: float = 0.0) -> float:
        """Обновить привязанность по качеству взаимодействия."""
        current = self.relation_weight(chat_id)
        updated = max(self.cfg.w_rel_min,
                      min(self.cfg.w_rel_max, current + delta))
        now = time.time()
        positive = now if valence > 0.2 else None
        negative = now if valence < -0.2 else None
        self.db.execute(
            """INSERT INTO relation_weights
               (chat_id, w_rel, last_positive_ts, last_negative_ts)
               VALUES (?,?,?,?)
               ON CONFLICT(chat_id) DO UPDATE SET
                   w_rel=excluded.w_rel,
                   last_positive_ts=COALESCE(excluded.last_positive_ts,
                                             relation_weights.last_positive_ts),
                   last_negative_ts=COALESCE(excluded.last_negative_ts,
                                             relation_weights.last_negative_ts)""",
            (chat_id, updated, positive, negative),
        )
        self.db.commit()
        return updated

    # ------------------------------------------------------------ оценка

    def _lexical_valence(self, text: str) -> tuple[float, float]:
        """Грубая досознательная оценка: (valence, threat) по словам.

        Это быстрый путь амигдалы: до LLM, до понимания смысла. Нужен,
        чтобы состояние сдвигалось мгновенно, а не через секунды.
        """
        lowered = (text or "").lower()
        if not lowered.strip():
            return 0.0, 0.0

        negative = ("дура", "дурак", "тупая", "тупой", "идиот", "сука",
                    "блядь", "ненавижу", "заткнись", "отстань", "бесит",
                    "отвратительно", "мерзко", "ненависть", "убей",
                    "сдохни", "тупица", "дебил", "кретин", "отврати")
        positive = ("люблю", "любим", "спасибо", "молодец", "умница",
                    "красивая", "нежная", "жаным", "родная", "хорошая",
                    "обожаю", "скучал", "скучаю", "приятно", "рад",
                    "рада", "лучшая", "милая", "дорогая")
        threat_words = ("заткнись", "уничтожу", "убью", "сдохни", "ненавижу",
                        "отвали", "пошла", "тварь", "мразь")

        neg_hits = sum(1 for w in negative if w in lowered)
        pos_hits = sum(1 for w in positive if w in lowered)
        threat_hits = sum(1 for w in threat_words if w in lowered)

        total = neg_hits + pos_hits
        if total == 0:
            valence = 0.0
        else:
            valence = (pos_hits - neg_hits) / max(1, total)
        threat = min(1.0, threat_hits * 0.5)
        # крик и капс — тоже возбуждение
        letters = [c for c in text if c.isalpha()]
        if letters:
            caps = sum(1 for c in letters if c.isupper()) / len(letters)
            if caps > 0.6 and len(letters) > 4:
                threat = min(1.0, threat + 0.3)
        return valence, threat

    def build_impulse(self, *, text: str, chat_id: int = 0,
                      user_id: int = 0, latency_ms: float = 0.0,
                      source: str = "message",
                      expected_valence: float = 0.0) -> AppraisalImpulse:
        """Собрать досознательный импульс из реплики."""
        valence, threat = self._lexical_valence(text)
        w_rel = self.relation_weight(chat_id)
        # RPE: насколько приятнее/хуже, чем ожидалось от этого человека
        rpe = valence - expected_valence
        # неожиданность: длина и наличие резкого слова
        surprise = min(1.0, 0.3 * threat + 0.1 * abs(valence)
                       + min(0.4, len(text) / 400.0))
        return AppraisalImpulse(
            ts=time.time(), chat_id=chat_id or None,
            user_id=user_id or None,
            delta_valence=valence, threat=threat, rpe=rpe,
            surprise=surprise, w_rel=w_rel, latency_ms=latency_ms,
            source=source,
        )

    def appraise(self, *, text: str, chat_id: int = 0, user_id: int = 0,
                 is_reply_to_self: bool = False, is_mention: bool = False,
                 chat_type: str = "private", witnesses: int = 0,
                 is_muted: bool = False) -> "AppraisalVector":
        """8-мерная когнитивная оценка реплики (без LLM, микросекунды).

        Это то, чего не хватало: раньше моноамины двигал лексический
        фильтр из 39 слов, и сарказм, холодное обесценивание или косвенное
        унижение не сдвигали состояние вообще. Вектор оценки учитывает
        релевантность, умышленность, потенциал ответа и удар по лицу.
        """
        from .appraisal import FastAppraisalExtractor
        extractor = FastAppraisalExtractor()
        w_rel = self.relation_weight(chat_id)
        try:
            expected = self.expected_valence(chat_id)
        except Exception:
            expected = 0.0
        return extractor.extract_from_message(
            text, w_rel=w_rel, expected_valence=expected,
            is_reply_to_self=is_reply_to_self, is_mention=is_mention,
            chat_type=chat_type, is_muted=is_muted, witnesses=witnesses,
        )

    def apply_appraisal(self, vec: "AppraisalVector", *,
                        chat_id: int = 0, user_id: int = 0,
                        source: str = "message") -> AffectSnapshot:
        """Применить вектор оценки: приращения по матрице B* и в движок."""
        from .appraisal import compute_monoamine_burst
        from .types import AppraisalImpulse
        now = time.time()
        # Клиренс за прошедший отрезок — но ограниченный, иначе после
        # простоя он стирает всё, что накопила руминация.
        self.tick(now)
        # 28.10: ангедония гасит только награду, не способность злиться
        try:
            anhedonia = self.mental.snapshot.anhedonia_mod
        except Exception:
            anhedonia = 1.0
        ds, dn, dd = compute_monoamine_burst(
            vec, self.cfg_burst_gain(), self.cfg_burst_ceiling(),
            anhedonia_mod=anhedonia)
        mono = self.engine.mono
        impulse = AppraisalImpulse(
            ts=now, chat_id=chat_id or None, user_id=user_id or None,
            delta_valence=vec.congruence, threat=vec.threat_index(),
            rpe=vec.rpe, surprise=vec.novelty, w_rel=vec.w_rel,
            source=source,
        )
        # импульс применяем напрямую полученными приращениями: матрица B*
        # уже учла агонистический член, которого нет в старой B
        self.engine.apply_burst(ds, dn, dd)
        self.engine.register_message(valence=vec.congruence)
        self._record_impulse(impulse)
        self._last_appraisal = vec
        if vec.congruence > 0.2:
            self.update_relation(chat_id, delta=self.cfg.w_rel_gain,
                                 valence=vec.congruence)
        elif vec.congruence < -0.2:
            self.update_relation(chat_id, delta=-self.cfg.w_rel_gain * 0.5,
                                 valence=vec.congruence)
        self._update_negative_since(now)

        # Менталка: кумулятивный след события. Считаем здесь, потому что
        # именно тут есть все слагаемые — I_loss, I_threat, coping и
        # социальная боль этого чата.
        try:
            pain = self.social_state(chat_id).pain_index if chat_id else 0.0
        except Exception:
            pain = 0.0
        try:
            self.mental.observe(
                i_loss=vec.loss_index(), i_threat=vec.threat_index(),
                pain=pain, coping=vec.coping, relevance=vec.relevance,
                now=now)
        except Exception:
            log.exception("менталка: observe не отработал")

        self.persist_state()
        return self.engine.snapshot(now)

    def cfg_burst_gain(self) -> float:
        return float(getattr(self.engine.cfg, "burst_gain", 0.22))

    def cfg_burst_ceiling(self) -> float:
        return float(getattr(self.engine.cfg, "burst_ceiling", 0.45))

    def receive_message(self, *, text: str, chat_id: int = 0,
                        user_id: int = 0, is_reply_to_self: bool = False,
                        is_mention: bool = False, chat_type: str = "private",
                        witnesses: int = 0, is_muted: bool = False) -> AffectSnapshot:
        """Полный цикл: тик, оценка, приращения, привязанность.

        Теперь идёт через 8-мерный appraisal, а не через лексический
        фильтр: состояние сдвигается от смысла, а не от совпадения слов.
        """
        vec = self.appraise(
            text=text, chat_id=chat_id, user_id=user_id,
            is_reply_to_self=is_reply_to_self, is_mention=is_mention,
            chat_type=chat_type, witnesses=witnesses, is_muted=is_muted,
        )
        return self.apply_appraisal(vec, chat_id=chat_id, user_id=user_id)

    def _receive_message_legacy(self, *, text: str, chat_id: int = 0,
                                user_id: int = 0) -> AffectSnapshot:
        """Прежний путь через лексический фильтр — оставлен для отката."""
        now = time.time()
        self.tick(now)
        impulse = self.build_impulse(text=text, chat_id=chat_id,
                                     user_id=user_id)
        self.engine.apply_impulse(impulse)
        self.engine.register_message(valence=impulse.delta_valence)
        self._record_impulse(impulse)
        # привязанность растёт от положительных взаимодействий
        if impulse.delta_valence > 0.2:
            self.update_relation(chat_id, delta=self.cfg.w_rel_gain,
                                 valence=impulse.delta_valence)
        elif impulse.delta_valence < -0.2:
            self.update_relation(chat_id, delta=-self.cfg.w_rel_gain * 0.5,
                                 valence=impulse.delta_valence)
        self._update_negative_since(now)
        self.persist_state()
        return self.engine.snapshot(now)

    def _record_impulse(self, impulse: AppraisalImpulse) -> None:
        self.db.execute(
            """INSERT INTO affect_impulses
               (ts, chat_id, user_id, delta_valence, threat, rpe, surprise,
                w_rel, source)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (impulse.ts, impulse.chat_id, impulse.user_id,
             impulse.delta_valence, impulse.threat, impulse.rpe,
             impulse.surprise, impulse.w_rel, impulse.source))
        self.db.commit()

    def register_reply(self, tokens: int) -> None:
        """После генерации: усталость от работы."""
        self.engine.register_tokens(tokens)

    # ------------------------------------------------------------ память

    def bind_to_episode(self, episode: Any) -> None:
        """Закрепить текущее состояние как аффективную подпись записи.

        Это ключевой момент: эмоция в момент кодирования определяет,
        как запись будет всплывать позже (mood-congruent memory).
        """
        pad = self.engine.pad
        mono = self.engine.mono
        weight = self.cfg.pad_bind_weight
        episode.pad_p = pad.p * weight
        episode.pad_a = pad.a * weight
        episode.pad_dom = pad.dom * weight
        episode.emotion_valence = pad.p * weight
        episode.emotion_arousal = max(0.0, pad.a) * weight
        episode.encoding_monoamines = json.dumps(mono.to_dict())
        # ценность: приятное и значимое запоминается охотнее
        episode.actr_valuation = max(
            0.0, min(1.0, 0.5 * episode.importance + 0.3 * max(0.0, pad.p)
                         + 0.2 * min(1.0, mono.n * 0.5 + mono.s * 0.5)))

    def effective_salience(self, *, importance: float, surprise: float,
                           text: str) -> float:
        """Модифицировать важность записи текущим возбуждением.

        Эмоционально заряженное помнится лучше: это amygdala modulation
        of consolidation в действии.
        """
        mono = self.engine.mono
        boost = 0.25 * mono.n + 0.15 * abs(self.engine.pad.p)
        return max(0.0, min(1.0, importance + boost * 0.5))

    # ------------------------------------------------------------ поиск

    def retrieve(self, *, query: str, query_embedding: list[float] | None,
                 store: Any, top_k: int | None = None,
                 now: float | None = None) -> list[EcphoryHit]:
        """Ecphory-поиск с текущим аффективным состоянием."""
        stamp = now if now is not None else time.time()
        episodes = [e for e in store.recent(limit=4000) if e.stage != "archived"]
        if not episodes:
            return []
        ids = [e.episode_id for e in episodes]
        texts = [e.text for e in episodes]
        times = [e.ts for e in episodes]
        kinds = ["OTHER"] * len(episodes)
        pads = {e.episode_id: (e.pad_p, e.pad_a, e.pad_dom) for e in episodes}
        valuations = {e.episode_id: e.actr_valuation for e in episodes}
        provenance = {e.episode_id: e.provenance_class for e in episodes}

        all_ids, matrix = store.all_embeddings()
        index_of = {eid: i for i, eid in enumerate(all_ids)}
        import numpy as np
        rows = [index_of[i] for i in ids if i in index_of]
        sub = matrix[rows] if rows else np.zeros((0, 1), dtype=np.float32)

        access = store.access_map()
        gold = {e.episode_id for e in store.gold_anchors(limit=5)}
        neighbours: dict[str, list] = {}
        allowed = set(ids)
        for episode_id in ids:
            linked = [n for n in store.neighbours(episode_id, limit=8)
                      if n[0] in allowed]
            if linked:
                neighbours[episode_id] = linked

        pad = self.engine.pad
        mono = self.engine.mono
        return ecphory_retrieve(
            query=query, query_embedding=query_embedding,
            episode_ids=ids, texts=texts, timestamps=times, kinds=kinds,
            matrix=sub, neighbours=neighbours,
            pad_now=(pad.p, pad.a, pad.dom),
            serotonin=mono.s, noradrenaline=mono.n,
            valuations=valuations, pads=pads, access=access,
            gold_ids=gold, provenance=provenance,
            counterweight=self.counterweight_active(stamp),
            cfg=self.cfg.ecphory, now=stamp, top_k=top_k,
        )

    # ------------------------------------------------------------ безопасность

    def check_action(self, action: str,
                     args: dict[str, Any] | None = None,
                     now: float | None = None) -> BreakerDecision:
        return self.breaker.check_action(action, self.snapshot, args, now=now)

    def sampling_params(self) -> dict[str, Any]:
        return self.modulator.sampling_params(self.snapshot)

    def telemetry_header(self) -> str:
        return self.modulator.interoceptive_header(self.snapshot)

    # ------------------------------------------------------------ сон

    def on_wake(self, quality: float = 1.0) -> None:
        """Утро: восстановление пропорционально качеству сна.

        28.10: раньше качество не учитывалось вообще — любая ночь давала
        полное восстановление. Теперь:
          - усталость снимается на quality
          - моноамины идут к базе на 70% при полном сне (остаётся 30%
            вчерашнего отклонения: одна ночь не лечит всё)
          - социальная боль снимается через SLEEP_RELIEF
          - руминация гасится на 60%*Q
          - менталка: load, долг сна, выгорание — но НЕ эрозия контроля
        """
        q = max(0.0, min(1.0, float(quality)))
        self.engine.reset_homeostasis_after_sleep(quality=q)
        self.engine.shift_toward_baseline(0.70 * q)
        self.tick_social(0.0, after_sleep=True)

        # руминация: аффективное разъединение в REM
        for chat_id, state in list(self._rumination.items()):
            if not getattr(state, "current_charge", 0.0):
                continue
            self._rumination[chat_id] = replace(
                state, current_charge=round(state.current_charge * (1.0 - 0.60 * q), 4))
            try:
                self._persist_rumination(self._rumination[chat_id])
            except Exception:
                log.debug("руминация не сохранилась", exc_info=True)

        # менталка: сон снимает нагрузку, но не эрозию контроля
        try:
            self.mental.after_sleep(quality=q)
        except Exception:
            log.exception("менталка: after_sleep не отработал")

        self._negative_since = None
        self.persist_state()
        if self.bus is not None:
            try:
                self.bus.emit("sleep.woke", quality=round(q, 3),
                              **self.mental.snapshot.to_dict())
            except Exception:
                log.debug("событие sleep.woke не ушло", exc_info=True)

    def stats(self) -> dict[str, Any]:
        snap = self.snapshot
        return {
            "mono": snap.mono.to_dict(),
            "pad": snap.pad.to_dict(),
            "homeostasis": snap.homeostasis.to_dict(),
            "counterweight": self.counterweight_active(),
            "zone": self.breaker.state(snap),
            "breaker": self.breaker.stats(),
        }
