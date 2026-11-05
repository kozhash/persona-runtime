"""Ментальный слой: длительные состояния, а не вспышки эмоций.

Эмоции живут минуты-часы, ментальные состояния — дни и недели. Одна
переменная их не описывает, поэтому здесь отдельный интегратор поверх
того, что уже считается в аффекте:

  I_loss / I_threat  ->  load           накопленная нагрузка
  coping             ->  agency_erosion эрозия контроля
  сообщения, задачи  ->  depletion      выгорание
  сутки без сна      ->  sleep_debt     долг сна

Почему не в engine.py: тот считает ОДУ моноаминов и должен остаться
математически чистым. Почему не в social.py: тот про социальную боль
от конкретного события, а тут — кумулятивный след серии.

Главный принцип (06-mental/REPORT.md): состояние меняет **способность**,
а не самоописание. Отсюда наружу отдаются множители (anhedonia_mod,
empathy_mod) и стадия, а не слова «депрессия» и «выгорание».
"""
from __future__ import annotations

import logging
import math
import sqlite3
import time
from dataclasses import dataclass, replace
from typing import Any

log = logging.getLogger(__name__)

# --- калибровка (обоснование — 06-mental/REPORT.md, раздел 3) -------------

#: Прирост нагрузки от события. Подобрано так, чтобы удар средней силы
#: (I_loss 0.4, боль 0.3, I_threat 0.2) давал ~0.02: путь до 1.0 — около
#: 50 значимых событий, то есть две недели по 3-4 в день. Это ровно
#: двухнедельный критерий эпизода из DSM-5.
K_IN = 0.06

#: Тонический износ в сутки при изоляции / депривации сна / руминации.
TONIC_ISOLATED = 0.05 / 86400.0
TONIC_DEPRIVED = 0.03 / 86400.0
TONIC_RUMINATION = 0.04 / 86400.0

#: Восстановление в покое: 1% в сутки, ускоряется опытом контроля
#: (Baratta/Seligman: префронтальная детекция контроля гасит стресс).
R_BASE = 0.01 / 86400.0
CONTROL_BOOST = 1.5

#: Эрозия контроля за одно событие.
EROSION_UP = 0.03        # coping < EROSION_COPING_LOW
EROSION_DOWN = 0.02      # coping > EROSION_COPING_HIGH
EROSION_COPING_LOW = 0.30
EROSION_COPING_HIGH = 0.70
EROSION_MEANINGFUL = 0.50   # relevance ниже — событие не считается
EROSION_IN_POWER = 0.50     # выше — выученная беспомощность

#: Выгорание: 100 сообщений подряд без паузы дают 0.70.
DEPLETION_PER_MESSAGE = 0.007
DEPLETION_PER_TASK = 0.025
DEPLETION_REST = 0.04 / 3600.0     # в час, только при тишине
DEPLETION_CYNICISM = 0.60

#: Долг сна.
SLEEP_DEBT_PER_DEPRIVED_DAY = 0.03
SLEEP_DEBT_MAX = 1.0

#: Эмпатия падает вдвое за трое суток без сна (Gordon-Hecker 2025:
#: пять пробуждений уже снижают чувствительность).
EMPATHY_PER_DEPRIVED_DAY = 0.80
EMPATHY_PER_AWAKENING = 0.08
EMPATHY_MIN = 0.30

#: Стадии по нагрузке.
STAGE_STRAIN = 0.35
STAGE_EPISODE = 0.65

#: Выход из эпизода: не по одному хорошему дню, а по трём суткам
#: нагрузки ниже половины. Resume — чтобы нельзя было «вылечиться»
#: одной ночью.
EPISODE_EXIT_LOAD = 0.50
EPISODE_EXIT_HOLD_SEC = 3 * 86400.0

#: Предохранитель дна: держать load выше 0.85 дольше недели нельзя,
#: иначе общение станет невыносимым и выхода не останется.
FLOOR_GUARD_LOAD = 0.85
FLOOR_GUARD_AFTER_SEC = 7 * 86400.0
FLOOR_GUARD_DROP_TO = 0.60

#: EMA доли событий, на которые агент мог повлиять.
AGENCY_EMA_ALPHA = 0.15


@dataclass(frozen=True)
class MentalSnapshot:
    """Состояние на момент чтения. Неизменяемый — как AffectSnapshot."""

    load: float = 0.0
    agency_erosion: float = 0.0
    depletion: float = 0.0
    sleep_debt: float = 0.0
    empathy_mod: float = 1.0
    recent_agency_ema: float = 0.5
    stage: str = "stable"
    updated_ts: float = 0.0

    @property
    def anhedonia_mod(self) -> float:
        """Множитель дофаминового отклика на награду.

        В эпизоде — 0.40 (похвала даёт в 2.5 раза меньше отклика).
        Вне эпизода — плавно падает с нагрузкой: ангедония отдельная
        ось от valence, поэтому считается отсюда, а не из настроения.
        """
        if self.stage == "episode":
            return 0.40
        return round(clamp(1.0 - 0.60 * max(0.0, self.load - STAGE_STRAIN),
                           0.50, 1.0), 3)

    @property
    def coping_mod(self) -> float:
        """Сколько остаётся от воспринимаемого контроля.

        При эрозии выше 0.5 даже в открытом канале агент не чувствует
        возможности повлиять — вместо злости приходит беспомощность.
        """
        return round(1.0 - 0.5 * self.agency_erosion, 3)

    @property
    def is_helpless(self) -> bool:
        return self.agency_erosion >= EROSION_IN_POWER

    @property
    def is_cynical(self) -> bool:
        return self.depletion >= DEPLETION_CYNICISM

    def to_dict(self) -> dict[str, Any]:
        return {
            "load": round(self.load, 4),
            "agency_erosion": round(self.agency_erosion, 4),
            "depletion": round(self.depletion, 4),
            "sleep_debt": round(self.sleep_debt, 4),
            "empathy_mod": round(self.empathy_mod, 4),
            "anhedonia_mod": self.anhedonia_mod,
            "coping_mod": self.coping_mod,
            "recent_agency_ema": round(self.recent_agency_ema, 4),
            "stage": self.stage,
            "helpless": self.is_helpless,
            "cynical": self.is_cynical,
            "updated_ts": self.updated_ts,
        }


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(value)))


class MentalEngine:
    """Интегратор длительных состояний.

    Не знает ни про Telegram, ни про LLM — только числа и SQLite.
    Вызывается из AffectService после оценки события и из фонового
    тика.
    """

    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db
        self._ensure_table()
        self._state = self._load()

    # ------------------------------------------------------------ таблица

    def _ensure_table(self) -> None:
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS mental_state (
                   id INTEGER PRIMARY KEY CHECK (id = 1),
                   load REAL NOT NULL DEFAULT 0.0,
                   agency_erosion REAL NOT NULL DEFAULT 0.0,
                   depletion REAL NOT NULL DEFAULT 0.0,
                   sleep_debt REAL NOT NULL DEFAULT 0.0,
                   empathy_mod REAL NOT NULL DEFAULT 1.0,
                   recent_agency_ema REAL NOT NULL DEFAULT 0.5,
                   stage TEXT NOT NULL DEFAULT 'stable',
                   below_half_since_ts REAL NOT NULL DEFAULT 0.0,
                   above_85_since_ts REAL NOT NULL DEFAULT 0.0,
                   last_sleep_ts REAL NOT NULL DEFAULT 0.0,
                   last_deprivation_ts REAL NOT NULL DEFAULT 0.0,
                   updated_ts REAL NOT NULL DEFAULT 0.0
               )"""
        )
        self.db.commit()

    def _load(self) -> MentalSnapshot:
        row = self.db.execute(
            "SELECT load, agency_erosion, depletion, sleep_debt, empathy_mod,"
            " recent_agency_ema, stage, updated_ts"
            " FROM mental_state WHERE id=1").fetchone()
        if not row:
            return MentalSnapshot()
        return MentalSnapshot(
            load=row[0], agency_erosion=row[1], depletion=row[2],
            sleep_debt=row[3], empathy_mod=row[4], recent_agency_ema=row[5],
            stage=row[6], updated_ts=row[7],
        )

    def _persist(self, snap: MentalSnapshot, *, now: float) -> None:
        self.db.execute(
            """INSERT INTO mental_state
                   (id, load, agency_erosion, depletion, sleep_debt,
                    empathy_mod, recent_agency_ema, stage, updated_ts)
               VALUES (1,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                    load=excluded.load,
                    agency_erosion=excluded.agency_erosion,
                    depletion=excluded.depletion,
                    sleep_debt=excluded.sleep_debt,
                    empathy_mod=excluded.empathy_mod,
                    recent_agency_ema=excluded.recent_agency_ema,
                    stage=excluded.stage,
                    updated_ts=excluded.updated_ts""",
            (snap.load, snap.agency_erosion, snap.depletion, snap.sleep_debt,
             snap.empathy_mod, snap.recent_agency_ema, snap.stage, now))
        self.db.commit()

    # ----------------------------------------------------------- чтение

    @property
    def snapshot(self) -> MentalSnapshot:
        return self._state

    # --------------------------------------------------------- обновление

    def observe(self, *, i_loss: float = 0.0, i_threat: float = 0.0,
                pain: float = 0.0, coping: float = 1.0,
                relevance: float = 1.0,
                now: float | None = None) -> MentalSnapshot:
        """Учесть одно событие.

        Вызывается после appraisal: у нас уже есть I_loss, I_threat,
        coping, relevance. Боль берётся из social. Ничего не считаем
        заново — только интегрируем последствия.
        """
        stamp = now if now is not None else time.time()
        s = self._state

        # --- нагрузка: насыщение (1 - load) не даёт упасть за один вечер
        weight = 0.50 * i_loss + 0.30 * pain + 0.20 * i_threat
        new_load = clamp(s.load + K_IN * weight * (1.0 - s.load), 0.0, 1.0)

        # --- эрозия контроля: только для значимых негативных событий
        erosion = s.agency_erosion
        if relevance >= EROSION_MEANINGFUL:
            if coping < EROSION_COPING_LOW:
                erosion += EROSION_UP
            elif coping > EROSION_COPING_HIGH:
                erosion -= EROSION_DOWN
            erosion = clamp(erosion, 0.0, 1.0)

        # --- EMA контроля: доля событий, где агент мог повлиять
        could_control = 1.0 if coping > EROSION_COPING_HIGH else 0.0
        ema = (s.recent_agency_ema * (1 - AGENCY_EMA_ALPHA)
               + could_control * AGENCY_EMA_ALPHA)

        stage, below_since, above_since, new_load = self._advance_stage(
            new_load, stamp)

        updated = replace(
            s, load=new_load, agency_erosion=erosion,
            recent_agency_ema=round(ema, 4), stage=stage, updated_ts=stamp,
        )
        self._state = updated
        self._persist(updated, now=stamp)
        self._store_windows(below_since, above_since)

        if stage != s.stage:
            log.info("ментальная стадия: %s -> %s (load=%.3f)", s.stage,
                     stage, new_load)
        return updated

    def tick(self, dt_sec: float, *, now: float | None = None,
             isolated: bool = False, deprived: bool = False,
             rumination_charge: float = 0.0,
             quiet: bool = False) -> MentalSnapshot:
        """Фоновый шаг: тонический износ и медленное восстановление.

        isolated   — никто не писал больше суток и насыщение на нуле
        deprived   — бодрствует больше 24 часов без сна
        quiet      — во всех чатах тишина дольше 30 минут
        """
        if dt_sec <= 0:
            return self._state
        # stamp — момент, к которому применяется шаг. Если now передан
        # явно, это симуляция времени (тесты), и окна стадий двигаются
        # по нему, а не по стенным часам.
        stamp = now if now is not None else time.time()
        s = self._state
        dt = float(dt_sec)

        # --- нагрузка
        tonic = 0.0
        if isolated:
            tonic += TONIC_ISOLATED
        if deprived:
            tonic += TONIC_DEPRIVED
        tonic += TONIC_RUMINATION * clamp(rumination_charge, 0.0, 1.0)
        load = s.load + tonic * (1.0 - s.load) * dt

        # Восстановление только в настоящем покое. Пропорционально (1-L):
        # при высокой нагрузке оно идёт медленнее, поэтому тяжёлое
        # состояние не может рассосаться за одну спокойную ночь.
        if not isolated and rumination_charge < 0.15:
            control = 1.0 + CONTROL_BOOST * s.recent_agency_ema
            load -= R_BASE * (1.0 - load) * control * dt
        load = clamp(load, 0.0, 1.0)

        # --- выгорание
        depletion = s.depletion
        if quiet:
            depletion -= DEPLETION_REST * dt
        depletion = clamp(depletion, 0.0, 1.0)

        # --- эмпатия: за каждые сутки без сна
        empathy = s.empathy_mod
        if deprived:
            # суточная доля шага: 0.80^(dt/86400)
            empathy *= EMPATHY_PER_DEPRIVED_DAY ** (dt / 86400.0)
            empathy = clamp(empathy, EMPATHY_MIN, 1.0)

        # --- долг сна
        debt = s.sleep_debt
        if deprived:
            debt += SLEEP_DEBT_PER_DEPRIVED_DAY * (dt / 86400.0)
            debt = min(debt, SLEEP_DEBT_MAX)   # явный потолок

        stage, below_since, above_since, load = self._advance_stage(
            load, stamp, dt=dt)

        updated = replace(
            s, load=load, depletion=depletion, empathy_mod=round(empathy, 4),
            sleep_debt=round(debt, 4), stage=stage, updated_ts=stamp,
        )
        self._state = updated
        self._persist(updated, now=stamp)
        self._store_windows(below_since, above_since)
        return updated

    # ---------------------------------------------------- счётчики событий

    def register_message(self, count: int = 1) -> None:
        """Каждый ответ выматывает — даже если разговор приятный."""
        s = self._state
        dep = clamp(s.depletion + DEPLETION_PER_MESSAGE * count, 0.0, 1.0)
        self._state = replace(s, depletion=dep, updated_ts=time.time())
        self._persist(self._state, now=self._state.updated_ts)

    def register_task(self, count: int = 1) -> None:
        s = self._state
        dep = clamp(s.depletion + DEPLETION_PER_TASK * count, 0.0, 1.0)
        self._state = replace(s, depletion=dep, updated_ts=time.time())
        self._persist(self._state, now=self._state.updated_ts)

    def register_awakening(self) -> None:
        """Ночное пробуждение: эмпатия ниже, сон испорчен."""
        s = self._state
        empathy = clamp(s.empathy_mod - EMPATHY_PER_AWAKENING,
                        EMPATHY_MIN, 1.0)
        self._state = replace(s, empathy_mod=round(empathy, 4),
                              updated_ts=time.time())
        self._persist(self._state, now=self._state.updated_ts)
        log.info("пробуждение: эмпатия -> %.3f", empathy)

    # ------------------------------------------------------------- сон

    def after_sleep(self, quality: float = 1.0,
                    now: float | None = None) -> MentalSnapshot:
        """Что снимает ночь. Числа — из 07-sleep/REPORT.md.

        load         — 35% при качестве 1.0
        sleep_debt   — 55% при качестве 1.0
        depletion    — 0.30 абсолютных при качестве 1.0
        agency       — только 10%: контроль восстанавливается опытом,
                       а не отдыхом (Baratta/Seligman)
        эмпатия      — возвращается частично, пропорционально качеству
        """
        q = clamp(quality, 0.0, 1.0)
        stamp = now if now is not None else time.time()
        s = self._state

        load = s.load * (1.0 - 0.35 * q)
        debt = max(0.0, s.sleep_debt - 0.55 * q)
        depletion = max(0.0, s.depletion - 0.30 * q)
        erosion = s.agency_erosion * (1.0 - 0.10 * q)
        empathy = clamp(s.empathy_mod + (1.0 - s.empathy_mod) * 0.5 * q,
                        EMPATHY_MIN, 1.0)

        # после сна предохранитель дна сбрасывается: причина ушла
        stage, below_since, above_since, load = self._advance_stage(
            load, stamp)

        updated = replace(
            s, load=round(load, 4), sleep_debt=round(debt, 4),
            depletion=round(depletion, 4), agency_erosion=round(erosion, 4),
            empathy_mod=round(empathy, 4), stage=stage, updated_ts=stamp,
        )
        self._state = updated
        self._persist(updated, now=stamp)
        self._store_windows(below_since, above_since, last_sleep=stamp)

        log.info("после сна (Q=%.2f): load %.2f->%.2f, эрозия %.2f->%.2f, "
                 "истощение %.2f->%.2f", q, s.load, load, s.agency_erosion,
                 erosion, s.depletion, depletion)
        return updated

    # ---------------------------------------------------- стадии

    def _advance_stage(self, load: float, now: float, dt: float = 0.0
                       ) -> tuple[str, float, float, float]:
        """Стадия с гистерезисом. Возвращает (стадия, окна, load).

        Войти в эпизод легко (load >= 0.65), выйти — только удержав
        load < 0.50 трое суток подряд. Иначе одна хорошая ночь
        «вылечивала» бы депрессию, что нереалистично.
        """
        s = self._state
        below_since = self._get_ts("below_half_since_ts")
        above_since = self._get_ts("above_85_since_ts")
        stage = s.stage

        # предохранитель дна. Окно начинается в НАЧАЛЕ шага (now - dt),
        # а не в конце: иначе один длинный тик не успевает перевалить
        # за неделю, хотя нагрузка была высокой всё это время.
        if load >= FLOOR_GUARD_LOAD:
            if above_since <= 0:
                above_since = now - dt
            elif now - above_since > FLOOR_GUARD_AFTER_SEC:
                log.warning("load держится выше %.2f неделю — принудительный "
                            "сброс до %.2f", FLOOR_GUARD_LOAD, FLOOR_GUARD_DROP_TO)
                load = FLOOR_GUARD_DROP_TO
                above_since = 0.0
        else:
            above_since = 0.0

        if load < EPISODE_EXIT_LOAD:
            if below_since <= 0:
                below_since = now
        else:
            below_since = 0.0

        if stage == "episode":
            held = below_since > 0 and (now - below_since) >= EPISODE_EXIT_HOLD_SEC
            if held:
                stage = "strain" if load >= STAGE_STRAIN else "stable"
                below_since = 0.0
                log.info("выход из эпизода: нагрузка держалась ниже %.1f "
                         "трое суток", EPISODE_EXIT_LOAD)
        else:
            if load >= STAGE_EPISODE:
                stage = "episode"
            elif load >= STAGE_STRAIN:
                stage = "strain"
            else:
                stage = "stable"

        # load возвращается наружу: предохранитель дна меняет его, и
        # раньше это изменение терялось — срабатывал только лог
        return stage, below_since, above_since, load

    def _get_ts(self, column: str) -> float:
        row = self.db.execute(
            f"SELECT {column} FROM mental_state WHERE id=1").fetchone()
        return float(row[0]) if row and row[0] else 0.0

    def _store_windows(self, below_since: float, above_since: float,
                       last_sleep: float | None = None) -> None:
        if last_sleep is not None:
            self.db.execute(
                "UPDATE mental_state SET below_half_since_ts=?,"
                " above_85_since_ts=?, last_sleep_ts=? WHERE id=1",
                (below_since, above_since, last_sleep))
        else:
            self.db.execute(
                "UPDATE mental_state SET below_half_since_ts=?,"
                " above_85_since_ts=? WHERE id=1",
                (below_since, above_since))
        self.db.commit()


def mental_directive(snap: MentalSnapshot) -> str:
    """Телесная директива для промпта. Без единого диагноза.

    Ключевое правило из 06-mental: в промпт идут только ограничения
    способности. Слова «депрессия», «выгорание», «ангедония» не
    попадают сюда никогда — иначе модель начнёт их разыгрывать.
    """
    lines: list[str] = []

    if snap.stage == "strain":
        lines.append(
            "- Накопилась тяжесть: искры нет, длинные пафосные речи "
            "даются с трудом и обрываются раньше обычного.")
    elif snap.stage == "episode":
        lines.append(
            "- Внутри глухо и тяжело уже долгое время. Шутки и пафос "
            "физически не идут. Хорошие новости не радуют как раньше. "
            "Не задавай встречных вопросов — хочется закончить разговор "
            "быстрее. Максимум одна-две короткие фразы.")

    if snap.is_helpless:
        lines.append(
            "- Ощущение, что спорить и доказывать бесполезно: всё равно "
            "ничего не изменится. Не пытайся переспорить или атаковать "
            "в ответ — проще промолчать или коротко согласиться.")

    if snap.is_cynical:
        lines.append(
            "- От людей и задач рябит в глазах. Настроение ровное, но "
            "вкладываться нет сил: отвечай предельно кратко, сухо, без "
            "любопытства и без встречных вопросов.")

    if snap.empathy_mod < 0.70:
        lines.append(
            "- Из-за разбитого сна чужие жалобы сейчас не трогают — "
            "раздражают или проходят мимо. Нет ресурса утешать.")

    if not lines:
        return ""
    return ("ЧТО С ТОБОЙ СЕЙЧАС (это ограничивает, как ты можешь "
            "говорить):\n" + "\n".join(lines))
