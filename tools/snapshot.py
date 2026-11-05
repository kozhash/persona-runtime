"""Снимок состояния kuni перед экспериментом и откат к нему.

Зачем: владелец хочет проверить, как она переживает отвержение. Такие
эксперименты необратимы, если их не обставить: обида осядет в памяти,
уйдёт в дневник, испортит отношение. Здесь — снимок всех баз и
восстановление одной командой.

Использование:
    python tools/snapshot.py save     # снять снимок
    python tools/snapshot.py restore  # вернуть последний
    python tools/snapshot.py list     # что есть
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
SNAP_DIR = DATA / "snapshots"

#: Что входит в снимок. Только состояние, не код.
TARGETS = ("memory.db", "kuni.db", "diary.db", "night_queue.db",
           "working_memory.md", "message_log.jsonl")


def _stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def save(label: str = "") -> Path:
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    target = SNAP_DIR / f"{_stamp()}{'-' + label if label else ''}"
    target.mkdir()
    copied = []
    for name in TARGETS:
        src = DATA / name
        if not src.exists():
            continue
        dst = target / name
        if src.suffix == ".db":
            # sqlite: копируем через backup API, иначе ловим полу-записанный файл
            try:
                src_con = sqlite3.connect(str(src))
                dst_con = sqlite3.connect(str(dst))
                with dst_con:
                    src_con.backup(dst_con)
                src_con.close()
                dst_con.close()
            except sqlite3.Error:
                shutil.copy2(src, dst)
        else:
            shutil.copy2(src, dst)
        copied.append(name)
    (target / "MANIFEST.txt").write_text(
        f"создан: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"файлы: {', '.join(copied)}\n", encoding="utf-8")
    print(f"снимок: {target}")
    print(f"файлов: {len(copied)} — {', '.join(copied)}")
    return target


def restore(path: str | None = None) -> Path:
    snaps = sorted([p for p in SNAP_DIR.glob("*") if p.is_dir()])
    if not snaps:
        raise SystemExit("снимков нет — нечего восстанавливать")
    target = Path(path) if path else snaps[-1]
    if not target.is_absolute():
        target = SNAP_DIR / target
    if not target.exists():
        raise SystemExit(f"нет такого снимка: {target}")

    restored = []
    for name in TARGETS:
        src = target / name
        if not src.exists():
            continue
        dst = DATA / name
        if dst.suffix == ".db":
            try:
                src_con = sqlite3.connect(str(src))
                dst_con = sqlite3.connect(str(dst))
                with dst_con:
                    src_con.backup(dst_con)
                src_con.close()
                dst_con.close()
            except sqlite3.Error:
                shutil.copy2(src, dst)
        else:
            shutil.copy2(src, dst)
        restored.append(name)
    print(f"восстановлено из {target.name}: {', '.join(restored)}")
    return target


def listing() -> None:
    snaps = sorted([p for p in SNAP_DIR.glob("*") if p.is_dir()])
    if not snaps:
        print("снимков нет")
        return
    for snap in snaps:
        files = [f.name for f in snap.iterdir() if f.name != "MANIFEST.txt"]
        size = sum(f.stat().st_size for f in snap.iterdir() if f.is_file())
        print(f"{snap.name}  ({len(files)} файлов, {size // 1024} КБ)")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    if cmd == "save":
        save(sys.argv[2] if len(sys.argv) > 2 else "")
    elif cmd == "restore":
        restore(sys.argv[2] if len(sys.argv) > 2 else None)
    else:
        listing()
