"""Достроить эмбеддинги для эпизодов, записанных без вектора.

Причина: раньше секция [llm.embedding] указывала на api.munito.ai, который
не умеет /embeddings (404). Записи сохранялись без вектора и находились
только по тексту — граф и семантический recall по ним не работали.

Скрипт идемпотентный: берёт только episodes с embedding IS NULL.
"""
from __future__ import annotations

import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from kuni.core.config import ConfigService  # noqa: E402
from kuni.llm.client import LLMClient  # noqa: E402
from kuni.memory.episodic import _pack  # noqa: E402
from kuni.memory.episodic import connect  # noqa: E402


async def main() -> int:
    svc = ConfigService(pathlib.Path("config.toml"))
    cfg = svc.current
    ep = cfg.llm.embedding
    model = ep.model or cfg.llm.embed_model
    client = LLMClient(ep.base_url or cfg.llm.base_url,
                       ep.api_key or cfg.llm.api_key, cfg.llm.model, model)

    db = connect(pathlib.Path("data/memory.db"))
    rows = db.execute(
        "SELECT episode_id, text FROM episodes WHERE embedding IS NULL").fetchall()
    print(f"без вектора: {len(rows)}")
    done = failed = 0
    for episode_id, text in rows:
        try:
            vec = await client.embed_one((text or "").strip() or "…")
        except Exception as e:  # noqa: BLE001
            print(f"  {episode_id}: ошибка {type(e).__name__}: {str(e)[:120]}")
            failed += 1
            continue
        db.execute("UPDATE episodes SET embedding=? WHERE episode_id=?",
                   (_pack(vec), episode_id))
        db.commit()
        done += 1
    await client.aclose()

    left = db.execute(
        "SELECT COUNT(*) FROM episodes WHERE embedding IS NULL").fetchone()[0]
    print(f"готово: {done}, ошибок: {failed}, осталось без вектора: {left}")
    return 0 if left == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
