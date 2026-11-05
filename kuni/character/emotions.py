"""Эмоциональный движок v3 — PAD + два слоя + фильтр выражения.

Архитектура по выжимке ресёрча (PAD/Sentipolis/openfeelz/Generative Agents):
- **Три оси PAD**: valence (приятно/мерзко), arousal (сонно/колотит),
  **dominance** (контроль: злость — сверху, страх/грусть — снизу).
- **Два слоя**: вспышка-эмоция (живёт минуты-часы, EMA) и **настроение-mood**
  (фон на часы-дни, тянется за вспышками медленным EMA). Настроение искажает
  приём новой информации (mood-congruent bias) и показывается отдельно.
- **Смешанные эмоции**: основная + вторичная (Plutchik dyads по духу).
- **Накопление и привыкание**: повторы усиливают вспышку, но каждый следующий
  повтор добавляет меньше (habituation).
- **Фильтр выражения персоны**: как ЭТА персона проявляет чувство — радость
  через подколы, злорадство честное, грусть закопана за чёрной шуткой.
"""
from __future__ import annotations

import logging
import math
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from typing import Any

log = logging.getLogger(__name__)

EMOTION_TIERS: dict[str, tuple[str, str, str]] = {
    "радость":        ("🙂", "😊", "🤣"),
    "веселье":        ("😄", "😆", "🤣"),
    "злорадство":     ("😏", "😌", "🤣"),
    "любовь":         ("☺️", "❤️", "😍"),
    "нежность":       ("☺️", "🥰", "😻"),
    "интерес":        ("🤔", "✨", "🤩"),
    "любопытство":    ("🤨", "🧐", "👀"),
    "гордость":       ("😌", "😎", "👑"),
    "спокойствие":    ("🙂", "😌", "🧘‍♀️"),
    "скука":          ("😐", "🥱", "💤"),
    "усталость":      ("😐", "🥱", "😴"),
    "грусть":         ("😕", "😢", "😭"),
    "тоска":          ("😕", "😔", "💔"),
    "обида":          ("🙁", "🥺", "😢"),
    "злость":         ("😒", "😠", "🤬"),
    "раздражение":    ("😒", "😤", "🤯"),
    "возмущение":     ("🙄", "😤", "🤬"),
    "отвращение":     ("😕", "🙄", "🤢"),
    "зависть":        ("😐", "😒", "💔"),
    "страх":          ("😬", "😨", "😱"),
    "тревога":        ("😬", "😰", "😫"),
    "стыд":           ("😳", "🙈", "🫠"),
    "смущение":       ("😊", "😳", "🙈"),
    "удивление":      ("🤨", "😲", "🤯"),
}
DEFAULT_TIERS = ("🙂", "🔥", "‼️")

#: Затухание по семействам эмоций (Вердюйн & Лаврейсен, 2015).
#: Раньше у всех 24 эмоций была одна константа 6 часов. Из-за этого
#: испуг и смущение висели часами, а обида и тоска исчезали за вечер,
#: хотя именно они должны держаться дольше всего. Разброс в реальности
#: до 240 раз, так что одна цифра на всех — это не упрощение, а ошибка.
#:
#: Значения — постоянная времени экспоненты, секунды. Полураспад = tau·ln2.
EMOTION_DECAY_TAU: dict[str, float] = {
    # быстрые: удивление, страх, отвращение, скука, стыд (~30 мин полураспад)
    "удивление":    45 * 60.0,
    "страх":        45 * 60.0,
    "отвращение":   45 * 60.0,
    "скука":        45 * 60.0,
    "стыд":         45 * 60.0,
    "смущение":     40 * 60.0,
    # средние: открытый гнев ~2 ч, вина ~3.5 ч
    "раздражение":  60 * 60.0,
    "злость":       2.5 * 3600.0,
    "возмущение":   2.0 * 3600.0,
    "радость":      3.0 * 3600.0,
    "веселье":      2.0 * 3600.0,
    "интерес":      3.0 * 3600.0,
    "любопытство":  2.0 * 3600.0,
    "гордость":     3.0 * 3600.0,
    "злорадство":   2.0 * 3600.0,
    "зависть":      3.0 * 3600.0,
    "нежность":     3.0 * 3600.0,
    "любовь":       6.0 * 3600.0,
    # долгие: тревога ~24 ч, печаль до 120 ч, обида — десятки часов
    "тревога":      12.0 * 3600.0,
    "грусть":       24.0 * 3600.0,
    "тоска":        24.0 * 3600.0,
    "обида":        16.0 * 3600.0,
    "усталость":    8.0 * 3600.0,
    "спокойствие":  6.0 * 3600.0,
}

#: Запасной вариант для эмоции, которой нет в таблице.
DECAY_TAU_SECONDS = 6 * 3600.0

#: Руминация растягивает жизнь эмоции: (1 + 4.5·заряд). Обида, которую
#: пережёвывают, живёт в разы дольше той, что отпустили.
RUMINATION_STRETCH = 4.5
MOOD_ALPHA = 0.20                  # mood подтягивается за вспышками медленно
MOOD_BIAS = 0.15                   # настроение искажает новую оценку
HABITUATION = 0.75                 # каждый повтор усиливает меньше
CALM_THRESHOLD = 0.15


@dataclass
class EmotionState:
    emotion: str = "спокойствие"
    secondary: str = ""              # вторичная эмоция (Plutchik dyads)
    intensity: float = 0.2           # 0..1 внутри, снаружи — проценты
    valence: float = 0.0             # -1..1
    arousal: float = 0.3             # 0..1
    dominance: float = 0.4           # 0..1 (PAD: контроль над ситуацией)
    reason: str = ""
    chat_id: int = 0
    ts: float = field(default_factory=time.time)

    @property
    def intensity_pct(self) -> int:
        return round(self.intensity * 100)

    def emoji(self) -> str:
        tiers = EMOTION_TIERS.get(self.emotion.strip().lower(), DEFAULT_TIERS)
        if self.intensity >= 0.66:
            return tiers[2]
        if self.intensity >= 0.33:
            return tiers[1]
        return tiers[0]

    def decay(self, rumination_charge: float = 0.0) -> "EmotionState":
        """Вспышка тает со временем; слабый остаток дрейфует в спокойствие.

        Постоянная времени берётся по семейству эмоции, а не одна на всех:
        испуг должен пройти за полчаса, обида — за сутки. Активная
        руминация растягивает затухание: то, что пережёвывают, не гаснет.
        """
        age = max(0.0, time.time() - self.ts)
        tau = EMOTION_DECAY_TAU.get(self.emotion.strip().lower(),
                                    DECAY_TAU_SECONDS)
        if rumination_charge > 0:
            tau *= (1.0 + RUMINATION_STRETCH * min(1.0, rumination_charge))
        factor = math.exp(-age / tau)
        return self._decayed_with(factor)

    def decayed(self) -> "EmotionState":
        """Совместимость: затухание без учёта руминации."""
        age = max(0.0, time.time() - self.ts)
        tau = EMOTION_DECAY_TAU.get(self.emotion.strip().lower(),
                                    DECAY_TAU_SECONDS)
        factor = math.exp(-age / tau)
        return self._decayed_with(factor)

    def _decayed_with(self, factor: float) -> "EmotionState":
        new_intensity = self.intensity * factor
        state = EmotionState(
            emotion=self.emotion, secondary=self.secondary, intensity=new_intensity,
            valence=self.valence * (0.5 + 0.5 * factor),
            arousal=self.arousal * (0.5 + 0.5 * factor),
            dominance=self.dominance,
            reason=self.reason, chat_id=self.chat_id, ts=self.ts,
        )
        if new_intensity < CALM_THRESHOLD and self.emotion != "спокойствие":
            state.emotion = "спокойствие"
            state.reason = "(отгорело)"
        return state

    def describe(self, mental: Any = None, sleep_phase: str = "awake") -> str:
        """Директива модели: два слоя + фильтр выражения персоны.

        28.10: добавлены состояние ресурса и фаза сна. Без них пафос
        включался даже когда сил на него физически не осталось.
        """
        pct = self.intensity_pct
        lines = [
            f"CURRENT EMOTION (flash): {self.emotion}"
            + (f" mixed with {self.secondary}" if self.secondary else "")
            + f" at {pct}% — PAD: valence {self.valence:+.2f}, arousal "
              f"{self.arousal:.2f}, dominance {self.dominance:.2f}.",
        ]
        if self.reason and self.reason != "(отгорело)":
            lines.append(f"It came from: {self.reason} — the reason still itches.")
        lines.append("This is not decoration. It MUST shape your next message:")
        lines.append(self.style_directive(mental=mental,
                                          sleep_phase=sleep_phase))
        return "\n".join(lines)

    def style_directive(self, mental: Any = None,
                        sleep_phase: str = "awake") -> str:
        """Как состояние меняет её текст. Русский, голос персонаж.

        27.10: здесь был зашит старый персонаж (куни) — роастинг, чёрный
        юмор, злорадство, казахский в каждом варианте. Правки промптов
        не помогали: этот текст уходит в системный промпт при КАЖДОМ
        сообщении и перебивал personality-файлы.

        Теперь: пафос — броня. Чем хуже ей, тем громче речь. Задели
        гордость — вспышка. Задели по-настоящему — пафос исчезает.
        """
        pct = self.intensity * 100
        v, a, d = self.valence, self.arousal, self.dominance
        em = self.emotion.lower()
        secondary = (self.secondary or "").lower()

        # --- приоритетный фильтр ресурса (28.10) ---------------------
        # Пафос — энергозатратная броня. Когда ресурс исчерпан, она
        # перестаёт включаться, и никакая эмоция этого не отменяет.
        # Проверяем ДО эмоциональных развилок: гордость 60% при нуле
        # сил всё равно не даст тираду.
        #
        # Фаза сна проверяется ПЕРВОЙ и независимо от mental: если она
        # засыпает, сил нет по определению, и знать про load/depletion
        # для этого не требуется.
        if sleep_phase == "wind_down":
            return ("- засыпает на ходу (мана на нуле): пафос ВЫКЛЮЧЕН. "
                    "Глаза слипаются, фразы обрывочные, одна-две "
                    "короткие строчки. Никаких тирад и титулов.")
        if sleep_phase == "wake_inertia":
            return ("- только проснулась (сонная инерция): ворчливая, "
                    "сонная, соображает туго. Пафос ещё не проснулся — "
                    "отвечает коротко и сухо, хочет обратно под одеяло "
                    "или есть.")
        if sleep_phase == "fragmented_wake":
            return ("- разбудили среди ночи: дезориентация и "
                    "раздражение. Отвечает резко и коротко, без "
                    "развёрнутых фраз и без титулов.")

        if mental is not None:
            stage = getattr(mental, "stage", "stable")
            depletion = float(getattr(mental, "depletion", 0.0) or 0.0)

            if stage == "episode":
                return ("- глубокое истощение ресурса: пафос НЕ ВКЛЮЧАЕТСЯ "
                        "вообще, даже при ударе по гордости. Нет сил ни на "
                        "вспышку, ни на титулы, ни на встречные вопросы. "
                        "Отвечает одной-двумя тихими, плоскими фразами. "
                        "Шутки не выходят.")
            if depletion > 0.60:
                return ("- вымотана потоком разговоров и дел: отвечает сухо, "
                        "формально, по делу, без эмоционального вложения и "
                        "БЕЗ встречных вопросов. Хочет быстрее закрыть тему.")

        # как меняется сам набор текста
        typing = "- обычный набор: заглавные на месте, опечаток нет."
        if a >= 0.7:
            typing = ("- печатает быстро, захлёбываясь: два сообщения подряд, "
                      "восклицательные знаки.")
        elif a <= 0.25:
            typing = ("- печатает вяло, одной рукой: короткие фразы, многоточия.")

        # пафос — её главный инструмент
        if em in ("возмущение", "раздражение") or (v <= -0.3 and d >= 0.5 and pct >= 55):
            return ("- задета гордость: ВСПЫШКА. Громко, быстро, пафосно. "
                    "«Ха! Ты серьёзно?!» Один-два восклицательных знака. "
                    "Не мат — она оскорблена, а не разъярена. Через пару "
                    "сообщений остынет, но виду не подаст.\n" + typing)

        if v <= -0.3 and pct >= 55:
            return ("- по-настоящему больно: пафос ИСЧЕЗАЕТ. Это главный "
                    "признак. Одна короткая фраза, многоточие. Никаких "
                    "титулов, никакого «величайшая». Она замолкает, "
                    "а не шутит.\n" + typing)

        if em in ("смущение", "стыд"):
            return ("- поймали на доброте или нежности: сбивается, "
                    "оправдывается и обрывает себя. «Это была стратегия. "
                    "Не выдумывай. ...забудь.»\n" + typing)

        if em in ("обида", "грусть", "тоска") and pct >= 55:
            return ("- тяжело, но она это прячет не шуткой, а тишиной: "
                    "отвечает редко и коротко, без пафоса. Если надавить — "
                    "одна честная фраза, и снова молчание.\n" + typing)

        if em in ("веселье", "радость") and pct >= 50:
            return ("- искренняя радость: «Ха-ха-ха!», хвастается тем, что "
                    "случилось, даже если это мелочь. Не злорадство — она "
                    "не смеётся над чужим горем.\n" + typing)

        if em == "гордость" and pct >= 50:
            return ("- гордость раздувается: говоришь чуть свысока, "
                    "подчёркиваешь, что справляешься сама, и обрываешь "
                    "себя — потому что сама слышишь, как это звучит.\n" + typing)

        if v >= 0.3:
            return "- хорошее настроение: ровно, тепло, с лёгким пафосом.\n" + typing
        if a <= 0.25:
            return ("- устала (мана на нуле): коротко, без восклицаний, "
                    "без титулов. «...я всё.»\n" + typing)

        return ("- фон: обычный голос. Ровно, с лёгкой колючестью, "
                "которую сама же и смягчаешь.\n" + typing)

class Emotions:
    def __init__(self, db: sqlite3.Connection, bus: Any | None = None) -> None:
        self.db = db
        self.bus = bus
        self.state = EmotionState()
        self.mood = {"valence": 0.0, "arousal": 0.3, "dominance": 0.4, "ts": time.time()}
        self._streak = 0
        self._streak_emotion = ""
        self._ensure_table()
        self._load_last()

    def _ensure_table(self) -> None:
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS emotions (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   ts REAL NOT NULL,
                   chat_id INTEGER NOT NULL DEFAULT 0,
                   emotion TEXT NOT NULL,
                   intensity REAL NOT NULL DEFAULT 0.2,
                   valence REAL NOT NULL,
                   arousal REAL NOT NULL,
                   reason TEXT NOT NULL DEFAULT ''
               )"""
        )
        cols = {row[1] for row in self.db.execute("PRAGMA table_info(emotions)")}
        if "intensity" not in cols:
            self.db.execute(
                "ALTER TABLE emotions ADD COLUMN intensity REAL NOT NULL DEFAULT 0.2")
        if "dominance" not in cols:
            self.db.execute(
                "ALTER TABLE emotions ADD COLUMN dominance REAL NOT NULL DEFAULT 0.4")
        if "secondary" not in cols:
            self.db.execute("ALTER TABLE emotions ADD COLUMN secondary TEXT DEFAULT ''")
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS mood (
                   id INTEGER PRIMARY KEY CHECK (id = 1),
                   valence REAL, arousal REAL, dominance REAL, ts REAL)"""
        )
        self.db.commit()

    def _load_last(self) -> None:
        row = self.db.execute(
            "SELECT ts, chat_id, emotion, intensity, valence, arousal, reason, "
            "dominance, secondary FROM emotions ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row:
            self.state = EmotionState(ts=row[0], chat_id=row[1], emotion=row[2],
                                      intensity=row[3], valence=row[4],
                                      arousal=row[5], reason=row[6],
                                      dominance=row[7] if len(row) > 7 else 0.4,
                                      secondary=row[8] if len(row) > 8 else "")
        mrow = self.db.execute(
            "SELECT valence, arousal, dominance, ts FROM mood WHERE id=1").fetchone()
        if mrow:
            self.mood = {"valence": mrow[0], "arousal": mrow[1],
                         "dominance": mrow[2], "ts": mrow[3]}

    # ------------------------------------------------------------- обновление

    def apply_assessment(self, data: dict[str, Any], chat_id: int,
                         somatic: dict[str, Any] | None = None) -> EmotionState:
        """Вживляет свежую оценку: PAD-слияние, mood-слой, привыкание.

        `somatic` — текущее состояние тела: {"p", "a", "dom", "pain"}.
        Если оно передано, эмоция не может ему противоречить. Это лечит
        расщепление: 26.10 соматика была на дне (P=-0.65, потребности
        в нуле), а LLM вернул «веселье 72%, обижаться не буду» — и модель
        послушала эмоцию, а не тело. Человек так не может: если внутри
        плохо, снаружи не выходит радость.
        """
        fresh = self.parse_llm_json(data, chat_id)
        prev = self.state.decayed()

        # --- согласование с соматикой
        if somatic:
            fresh = self._reconcile_with_somatic(fresh, somatic)

        # mood-congruent bias: фон слегка искажает восприятие нового триггера
        mood_v = self.mood["valence"]
        effective_v = fresh.valence * (1 - MOOD_BIAS) + mood_v * MOOD_BIAS

        same = fresh.emotion == prev.emotion
        habit = 1.0
        if fresh.emotion == self._streak_emotion and self._streak >= 2:
            habit = HABITUATION ** min(self._streak - 1, 3)   # повторы приедаются
        self._streak = self._streak + 1 if fresh.emotion == self._streak_emotion else 1
        self._streak_emotion = fresh.emotion

        if same:
            base = max(prev.intensity, fresh.intensity) * 0.55 + fresh.intensity * 0.45
            intensity = min(1.0, base + 0.10 * habit)
        else:
            intensity = max(fresh.intensity, prev.intensity * 0.25)

        self.state = EmotionState(
            emotion=fresh.emotion,
            secondary=fresh.secondary,
            intensity=intensity,
            valence=prev.valence * 0.45 + effective_v * 0.55,
            arousal=prev.arousal * 0.45 + fresh.arousal * 0.55,
            dominance=prev.dominance * 0.35 + fresh.dominance * 0.65,
            reason=fresh.reason or prev.reason,
            chat_id=chat_id,
        )
        # mood тянется за вспышкой медленно (часы-дни), не наоборот
        for axis in ("valence", "arousal", "dominance"):
            self.mood[axis] = (self.mood[axis] * (1 - MOOD_ALPHA)
                               + getattr(self.state, axis) * MOOD_ALPHA)
        self.mood["ts"] = time.time()
        self._persist(self.state)
        if self.bus:
            payload = asdict(self.state)
            payload["intensity_pct"] = self.state.intensity_pct
            payload["emoji"] = self.state.emoji()
            payload["mood"] = dict(self.mood)
            self.bus.emit("emotion.changed", **payload)
        log.info("эмоция: %s %s %d%% (v=%.2f a=%.2f d=%.2f) | mood v=%.2f — %s",
                 self.state.emoji(), self.state.emotion, self.state.intensity_pct,
                 self.state.valence, self.state.arousal, self.state.dominance,
                 self.mood["valence"], self.state.reason or "—")
        return self.state

    #: Какие эмоции считаем негативными — на случай, если LLM вернул
    #: положительную при плохом теле.
    NEGATIVE_FAMILY = (
        "обида", "злость", "раздражение", "возмущение", "грусть", "тоска",
        "тревога", "страх", "отвращение", "стыд", "скука", "усталость",
        "подавленность",
    )

    def _reconcile_with_somatic(self, fresh: EmotionState,
                                somatic: dict[str, float]) -> EmotionState:
        """Не дать эмоции врать про тело.

        Правило: тело задаёт знак, LLM — оттенок. Если тело в минусе,
        эмоция не может быть положительной. Если тело в глубоком минусе,
        она обязана быть негативной и заметной, иначе выходит «мне плохо,
        но я веселюсь» — ровно то, что случилось 26.10.

        Что НЕ делает: не подменяет эмоцию, когда тело спокойно. Тут
        LLM свободен, иначе персонаж теряет нюансы.
        """
        p_val = float(somatic.get("p", 0.0) or 0.0)
        pain = float(somatic.get("pain", 0.0) or 0.0)
        dom = float(somatic.get("dom", 0.0) or 0.0)

        # насколько телу плохо, 0..1
        distress = max(0.0, -p_val) * 0.6 + max(0.0, pain) * 0.4
        if distress < 0.30:
            return fresh

        name = fresh.emotion.strip().lower()
        is_positive = fresh.valence > 0.05 and name not in self.NEGATIVE_FAMILY

        if is_positive:
            # тело против: подменяем на ближайшую по смыслу негативную
            if pain >= 0.55:
                replacement = "обида"
            elif dom < -0.15:
                replacement = "тоска"
            elif dom > 0.25:
                replacement = "раздражение"
            else:
                replacement = "грусть"
            log.info("эмоция согласована с телом: %s -> %s "
                     "(distress=%.2f pain=%.2f dom=%+.2f)",
                     fresh.emotion, replacement, distress, pain, dom)
            return EmotionState(
                emotion=replacement,
                secondary=fresh.emotion if fresh.intensity > 0.4 else "",
                intensity=max(fresh.intensity,
                              min(0.9, 0.5 + distress * 0.5)),
                valence=min(fresh.valence, -0.25 - distress * 0.4),
                arousal=max(fresh.arousal, 0.4),
                dominance=min(fresh.dominance, dom if dom < 0 else -0.2),
                reason=(fresh.reason + " [тело не согласно]")[:400],
                chat_id=fresh.chat_id,
                ts=fresh.ts,
            )

        if distress >= 0.55 and fresh.intensity < 0.45:
            # тело в глубоком минусе, а эмоция еле тлеет — усиливаем
            return EmotionState(
                emotion=fresh.emotion,
                secondary=fresh.secondary,
                intensity=min(0.95, 0.45 + distress * 0.5),
                valence=min(fresh.valence, -0.3),
                arousal=max(fresh.arousal, 0.45),
                dominance=fresh.dominance,
                reason=fresh.reason,
                chat_id=fresh.chat_id,
                ts=fresh.ts,
            )
        return fresh

    def _persist(self, state: EmotionState) -> None:
        self.db.execute(
            "INSERT INTO emotions (ts, chat_id, emotion, intensity, valence, arousal, "
            "reason, dominance, secondary) VALUES (?,?,?,?,?,?,?,?,?)",
            (state.ts, state.chat_id, state.emotion, state.intensity,
             state.valence, state.arousal, state.reason,
             state.dominance, state.secondary),
        )
        self.db.execute(
            "INSERT INTO mood (id, valence, arousal, dominance, ts) VALUES (1,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET valence=excluded.valence, "
            "arousal=excluded.arousal, dominance=excluded.dominance, ts=excluded.ts",
            (self.mood["valence"], self.mood["arousal"],
             self.mood["dominance"], self.mood["ts"]),
        )
        self.db.commit()

    # -------------------------------------------------------------- чтение

    def history(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT ts, chat_id, emotion, intensity, valence, arousal, reason, "
            "dominance, secondary FROM emotions ORDER BY id DESC LIMIT ?", (limit,),
        ).fetchall()
        result = []
        for r in rows:
            e = EmotionState(ts=r[0], chat_id=r[1], emotion=r[2], intensity=r[3],
                             valence=r[4], arousal=r[5], reason=r[6],
                             dominance=r[7] if len(r) > 7 else 0.4,
                             secondary=r[8] if len(r) > 8 else "")
            result.append({
                "ts": r[0], "chat_id": r[1], "emotion": r[2],
                "intensity_pct": e.intensity_pct, "valence": r[4], "arousal": r[5],
                "dominance": e.dominance, "secondary": e.secondary,
                "reason": r[6], "emoji": e.emoji(),
            })
        return result

    # -------------------------------------------------------------- парсинг

    @staticmethod
    def parse_llm_json(data: dict[str, Any], chat_id: int) -> EmotionState:
        def clamp(v: Any, lo: float, hi: float, default: float) -> float:
            try:
                return max(lo, min(hi, float(v)))
            except (TypeError, ValueError):
                return default

        intensity = clamp(data.get("intensity"), 0.0, 100.0, 30.0) / 100.0
        return EmotionState(
            emotion=str(data.get("emotion") or "спокойствие").strip()[:64] or "спокойствие",
            secondary=str(data.get("secondary_emotion") or data.get("secondary") or "").strip()[:64],
            intensity=intensity,
            valence=clamp(data.get("valence"), -1.0, 1.0, 0.0),
            arousal=clamp(data.get("arousal"), 0.0, 1.0, 0.3),
            dominance=clamp(data.get("dominance"), 0.0, 1.0, 0.4),
            reason=str(data.get("reason") or "").strip()[:500],
            chat_id=chat_id,
        )
