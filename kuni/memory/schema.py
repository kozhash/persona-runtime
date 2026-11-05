"""Схема памяти: эпизоды, пропозиции, рёбра, теги, битемпоральные факты.

Одна SQLite-база на всё: «сырые» события (E-LTP, лабильный буфер),
консолидированные энграммы (L-LTP), ассоциативный граф с весами Хебба
и семантические факты с valid-time / transaction-time.

Разделение по уровням (CLS: complementary learning systems):
  episodes      — гиппокампальный быстрый след (single-shot, ортогональный)
  propositions  — DG: разделение паттернов, атомарные пропозиции
  edges         — CA3: автоассоциативный граф, пластичность Хебба
  facts         — неокортекс: медленная семантическая память, битемпоральность
  syn_tags      — STC: синаптические теги и захват PRP

Ничего не знает про LLM и Telegram — только SQL.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 5

_DDL = """
CREATE TABLE IF NOT EXISTS episodes (
    episode_id      TEXT PRIMARY KEY,
    ts              REAL NOT NULL,
    text            TEXT NOT NULL,
    source          TEXT NOT NULL DEFAULT 'chat',
    speaker         TEXT NOT NULL DEFAULT '',
    chat_id         INTEGER NOT NULL DEFAULT 0,
    speaker_id      INTEGER NOT NULL DEFAULT 0,       -- кто сказал (user_id)
    speaker_name    TEXT NOT NULL DEFAULT '',         -- имя на тот момент
    chat_title      TEXT NOT NULL DEFAULT '',         -- где это было
    stage           TEXT NOT NULL DEFAULT 'labile',   -- labile | consolidated | archived
    confidence      REAL NOT NULL DEFAULT 0.0,        -- -1..1
    importance      REAL NOT NULL DEFAULT 0.0,        -- 0..1
    surprise        REAL NOT NULL DEFAULT 0.0,        -- 0..1
    emotion_valence REAL NOT NULL DEFAULT 0.0,        -- -1..1
    emotion_arousal REAL NOT NULL DEFAULT 0.0,        -- 0..1
    embedding       BLOB,
    tags            TEXT NOT NULL DEFAULT '',         -- через запятую
    provenance      TEXT NOT NULL DEFAULT '',         -- откуда пришло, неизменяемо
    access_count    INTEGER NOT NULL DEFAULT 0,
    last_access     REAL NOT NULL DEFAULT 0.0,
    checksum        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_ep_ts     ON episodes(ts);
CREATE INDEX IF NOT EXISTS idx_ep_stage  ON episodes(stage, ts);
CREATE INDEX IF NOT EXISTS idx_ep_chat   ON episodes(chat_id, ts);

CREATE TABLE IF NOT EXISTS propositions (
    prop_id         TEXT PRIMARY KEY,
    episode_id      TEXT NOT NULL,
    text            TEXT NOT NULL,
    kind            TEXT NOT NULL DEFAULT 'OTHER',     -- ENTITY_DESCRIPTION|THOUGHT|EVENT|FACT|OTHER
    entities        TEXT NOT NULL DEFAULT '',         -- json-массив канонических имён
    confidence      REAL NOT NULL DEFAULT 0.0,
    embedding       BLOB,
    ts              REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_prop_ep   ON propositions(episode_id);
CREATE INDEX IF NOT EXISTS idx_prop_kind ON propositions(kind);

CREATE TABLE IF NOT EXISTS edges (
    src             TEXT NOT NULL,
    dst             TEXT NOT NULL,
    kind            TEXT NOT NULL DEFAULT 'hebbian',  -- hebbian|synonymy|temporal|causal|prevents
    weight          REAL NOT NULL DEFAULT 0.1,
    confidence      REAL NOT NULL DEFAULT 0.5,
    valid_from      REAL NOT NULL DEFAULT 0.0,        -- битемпоральность: когда факт верен
    valid_to        REAL,
    txn_from        REAL NOT NULL DEFAULT 0.0,        -- когда мы это узнали
    txn_to          REAL,
    last_reinforced REAL NOT NULL DEFAULT 0.0,
    PRIMARY KEY (src, dst, kind)
);
CREATE INDEX IF NOT EXISTS idx_edge_src  ON edges(src);
CREATE INDEX IF NOT EXISTS idx_edge_dst  ON edges(dst);

CREATE TABLE IF NOT EXISTS facts (
    fact_id         TEXT PRIMARY KEY,
    subject         TEXT NOT NULL,
    predicate       TEXT NOT NULL,
    object          TEXT NOT NULL,
    serial          INTEGER NOT NULL DEFAULT 0,       -- монотонный счётчик для max(serial)
    confidence      REAL NOT NULL DEFAULT 0.0,
    origin_episode  TEXT NOT NULL DEFAULT '',
    valid_from      REAL NOT NULL DEFAULT 0.0,
    valid_to        REAL,
    txn_from        REAL NOT NULL DEFAULT 0.0,
    txn_to          REAL
);
CREATE INDEX IF NOT EXISTS idx_fact_sp    ON facts(subject, predicate);
CREATE INDEX IF NOT EXISTS idx_fact_serial ON facts(subject, predicate, serial);

CREATE TABLE IF NOT EXISTS syn_tags (
    tag_id          TEXT PRIMARY KEY,
    episode_id      TEXT NOT NULL,
    strength        REAL NOT NULL DEFAULT 0.0,
    created_at      REAL NOT NULL,
    expires_at      REAL NOT NULL,                    -- тег живёт 1-2 часа без захвата
    captured        INTEGER NOT NULL DEFAULT 0,
    captured_at     REAL
);
CREATE INDEX IF NOT EXISTS idx_tag_ep    ON syn_tags(episode_id);
CREATE INDEX IF NOT EXISTS idx_tag_live  ON syn_tags(captured, expires_at);

CREATE TABLE IF NOT EXISTS meta (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL
);

-- версия 4: аффективная подпись, происхождение, ACT-R
CREATE TABLE IF NOT EXISTS memory_access (
    node_id         TEXT NOT NULL,
    ts              REAL NOT NULL,
    source          TEXT NOT NULL DEFAULT 'retrieval'
);
CREATE INDEX IF NOT EXISTS idx_access_node_ts ON memory_access(node_id, ts);

CREATE TABLE IF NOT EXISTS neuro_state (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    ts              REAL NOT NULL,
    s               REAL NOT NULL,
    n               REAL NOT NULL,
    d               REAL NOT NULL,
    pad_p           REAL NOT NULL DEFAULT 0.0,
    pad_a           REAL NOT NULL DEFAULT 0.0,
    pad_dom         REAL NOT NULL DEFAULT 0.0,
    fatigue         REAL NOT NULL DEFAULT 0.0,
    cognitive_load  REAL NOT NULL DEFAULT 0.0,
    social_satiety  REAL NOT NULL DEFAULT 0.0,
    rumination_since REAL,
    counterweight_active INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS affect_impulses (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL NOT NULL,
    chat_id         INTEGER,
    user_id         INTEGER,
    delta_valence   REAL NOT NULL DEFAULT 0.0,
    threat          REAL NOT NULL DEFAULT 0.0,
    rpe             REAL NOT NULL DEFAULT 0.0,
    surprise        REAL NOT NULL DEFAULT 0.0,
    w_rel           REAL NOT NULL DEFAULT 1.0,
    source          TEXT NOT NULL DEFAULT 'message'
);
CREATE INDEX IF NOT EXISTS idx_impulses_ts ON affect_impulses(ts);

CREATE TABLE IF NOT EXISTS relation_weights (
    chat_id         INTEGER PRIMARY KEY,
    user_id         INTEGER,
    w_rel           REAL NOT NULL DEFAULT 1.0,
    trust           REAL NOT NULL DEFAULT 0.0,
    expected_valence REAL NOT NULL DEFAULT 0.0,
    last_positive_ts REAL,
    last_negative_ts REAL
);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    """Открыть базу памяти и привести схему к актуальной версии."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    migrate(conn)
    return conn


def _add_column(conn: sqlite3.Connection, table: str, column: str,
                decl: str) -> None:
    """Добавить колонку, если её ещё нет (идемпотентно)."""
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column in columns:
        return
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    conn.commit()


def _backfill(conn: sqlite3.Connection) -> None:
    """Заполнить новые поля для уже существующих данных.

    Важно: эмоциональные записи помечаем как SELF_INFERENCE — это
    собственная интерпретация модели, а не наблюдение.
    """
    conn.execute(
        """UPDATE episodes SET provenance_class = CASE
               WHEN source = 'emotion' THEN 'SELF_INFERENCE'
               ELSE 'OBSERVATION'
           END
           WHERE provenance_class IS NULL OR provenance_class = ''""")
    conn.execute(
        """UPDATE episodes SET
               pad_p = COALESCE(emotion_valence, 0.0),
               pad_a = COALESCE(emotion_arousal, 0.0)
           WHERE pad_p = 0.0 AND pad_a = 0.0""")
    # доступы: время кодирования + последнее обращение
    existing = conn.execute(
        "SELECT COUNT(*) FROM memory_access").fetchone()[0]
    if not existing:
        conn.execute(
            """INSERT INTO memory_access (node_id, ts, source)
               SELECT episode_id, ts, 'encoding' FROM episodes""")
        conn.execute(
            """INSERT INTO memory_access (node_id, ts, source)
               SELECT episode_id, last_access, 'legacy_access' FROM episodes
               WHERE last_access IS NOT NULL AND last_access > 0""")
    conn.commit()


def migrate(conn: sqlite3.Connection) -> int:
    """Идемпотентная миграция. Возвращает установленную версию схемы."""
    conn.executescript(_DDL)
    _add_column(conn, "episodes", "tags", "TEXT NOT NULL DEFAULT ''")
    # версия 4: аффективная подпись и происхождение
    for column, decl in (
        ("provenance_class", "TEXT NOT NULL DEFAULT 'OBSERVATION'"),
        ("pad_p", "REAL NOT NULL DEFAULT 0.0"),
        ("pad_a", "REAL NOT NULL DEFAULT 0.0"),
        ("pad_dom", "REAL NOT NULL DEFAULT 0.0"),
        ("actr_valuation", "REAL NOT NULL DEFAULT 0.0"),
        ("is_gold_anchor", "INTEGER NOT NULL DEFAULT 0"),
        ("encoding_monoamines", "TEXT"),
        ("original_ts", "REAL"),
    ):
        _add_column(conn, "episodes", column, decl)
    for column, decl in (
        ("provenance_class", "TEXT NOT NULL DEFAULT 'OBSERVATION'"),
        ("pad_p", "REAL NOT NULL DEFAULT 0.0"),
        ("pad_a", "REAL NOT NULL DEFAULT 0.0"),
        ("pad_dom", "REAL NOT NULL DEFAULT 0.0"),
        ("actr_valuation", "REAL NOT NULL DEFAULT 0.0"),
        ("is_gold_anchor", "INTEGER NOT NULL DEFAULT 0"),
    ):
        _add_column(conn, "propositions", column, decl)
    _add_column(conn, "facts", "provenance_class",
                "TEXT NOT NULL DEFAULT 'OBSERVATION'")
    # версия 5: атрибуция — кто сказал и где. Без неё «люблю кофе»
    # от владельца и от чужого бота сливались в один факт без автора.
    for column, decl in (
        ("speaker_id", "INTEGER NOT NULL DEFAULT 0"),
        ("speaker_name", "TEXT NOT NULL DEFAULT ''"),
        ("chat_title", "TEXT NOT NULL DEFAULT ''"),
    ):
        _add_column(conn, "episodes", column, decl)
    _backfill(conn)
    current = 0
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if row:
        try:
            current = int(row[0])
        except (TypeError, ValueError):
            current = 0
    if current < SCHEMA_VERSION:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )
    conn.commit()
    return SCHEMA_VERSION


def tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
    return [r[0] for r in rows]
