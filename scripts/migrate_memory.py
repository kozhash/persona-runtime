"""Миграция старой памяти (diary.db) в новую (memory.db).

Проблема: после переписывания памяти приложение открывает memory.db,
а все записи остались в diary.db. В логе было «дневник=0 записей»,
хотя история есть.

Что делает:
  * читает diary + diary_links из старой базы;
  * конвертирует каждую запись в Episode (stage=consolidated,
    provenance_class по источнику);
  * переносит эмбеддинги как есть — размерность совпадает (4096);
  * переносит ассоциативные рёбра в edges;
  * идемпотентно: повторный запуск не дублирует.

Запуск:
    python scripts/migrate_memory.py [--dry-run]
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kuni.memory.episodic import Episode, EpisodicStore  # noqa: E402


def provenance_for(source: str) -> str:
    """Источник записи -> класс происхождения.

    Эмоциональные записи — собственная интерпретация, не наблюдение.
    Это важно для защиты от «ИИ-психоза»: своё мнение не должно
    становиться фактом наравне с тем, что реально сказали.
    """
    if source == "emotion":
        return "SELF_INFERENCE"
    if source in ("news", "diary_save"):
        return "OBSERVATION"
    return "OBSERVATION"


def migrate(old_path: Path, new_path: Path, dry_run: bool = False) -> dict:
    if not old_path.exists():
        raise SystemExit(f"нет файла старой памяти: {old_path}")

    old = sqlite3.connect(str(old_path))
    rows = old.execute(
        "SELECT entry_id, ts, text, tags, source, embedding FROM diary "
        "ORDER BY ts").fetchall()

    stats = {"total": len(rows), "moved": 0, "skipped": 0, "links": 0}

    if dry_run:
        print(f"[dry-run] записей к переносу: {len(rows)}")
        for row in rows[:5]:
            print(f"  {row[1]:.0f} [{row[4]}] {row[2][:60]}")
        return stats

    store = EpisodicStore(new_path)
    existing = {r[0] for r in store.db.execute(
        "SELECT episode_id FROM episodes")}

    for entry_id, ts, text, tags, source, blob in rows:
        if entry_id in existing:
            stats["skipped"] += 1
            continue
        embedding = None
        if blob:
            import numpy as np
            embedding = np.frombuffer(blob, dtype=np.float32).tolist()
        episode = Episode(
            episode_id=entry_id,
            ts=ts or time.time(),
            text=text or "",
            source=source or "chat",
            stage="consolidated",        # старая память уже пережила ночь
            provenance=f"migrated:{source}",
            provenance_class=provenance_for(source or "chat"),
            importance=0.5 if source == "emotion" else 0.4,
            embedding=embedding,
            tags=[t for t in (tags or "").split(",") if t],
        )
        store.add_episode(episode)
        stats["moved"] += 1

    # рёбра: старая таблица diary_links(from_id, to_id, weight)
    try:
        links = old.execute(
            "SELECT from_id, to_id, weight FROM diary_links").fetchall()
    except sqlite3.OperationalError:
        links = []
    known = {r[0] for r in store.db.execute("SELECT episode_id FROM episodes")}
    for from_id, to_id, weight in links:
        if from_id in known and to_id in known:
            store.link(from_id, to_id, kind="hebbian",
                       weight=float(weight or 0.1),
                       confidence=float(weight or 0.5))
            stats["links"] += 1

    return stats


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--old", default="data/diary.db")
    parser.add_argument("--new", default="data/memory.db")
    args = parser.parse_args()

    old = ROOT / args.old
    new = ROOT / args.new
    stats = migrate(old, new, args.dry_run)

    print()
    print(f"всего в старой базе : {stats['total']}")
    print(f"перенесено          : {stats['moved']}")
    print(f"пропущено (уже есть): {stats['skipped']}")
    print(f"рёбер графа         : {stats['links']}")

    if not args.dry_run:
        store = EpisodicStore(new)
        print()
        print("итог memory.db:", store.stats())
    return 0


if __name__ == "__main__":
    sys.exit(main())
