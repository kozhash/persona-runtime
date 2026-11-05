"""Совместимость: старые имена хранилища поверх новой эпизодической памяти.

Раньше здесь был `SqliteDiaryStore` с плоской таблицей `diary`. Теперь
источник правды — `EpisodicStore` (эпизоды + граф + факты). Этот модуль
оставлен, чтобы внешний код и тесты не ломались.

Новый код должен импортировать `kuni.memory.episodic`.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from .episodic import Episode, EpisodicStore

# Прежнее имя записи дневника
DiaryEntry = Episode

# Прежний контракт хранилища
DiaryStore = EpisodicStore

#: Обратная совместимость: раньше класс назывался так
SqliteDiaryStore = EpisodicStore


def cosine_top_k(query: np.ndarray, matrix: np.ndarray, ids: list[str],
                 k: int) -> list[tuple[str, float]]:
    """Топ-k по косинусной близости (оставлено для совместимости).

    Внутри новой памяти используется `retrieval.cosine_scores`, который
    умеет работать с нулевыми векторами и не падает на пустой матрице.
    """
    if matrix.size == 0 or matrix.shape[0] == 0:
        return []
    q = np.asarray(query, dtype=np.float32).ravel()
    q_norm = float(np.linalg.norm(q))
    if q_norm == 0:
        return []
    norms = np.linalg.norm(matrix, axis=1)
    denom = norms * q_norm
    denom[denom == 0] = 1e-9
    sims = (matrix @ q) / denom
    k = min(k, len(ids))
    order = np.argsort(-sims)[:k]
    return [(ids[int(i)], float(sims[int(i)])) for i in order]


def open_store(path: Any) -> EpisodicStore:
    """Открыть хранилище по пути (сахар для тестов и CLI)."""
    return EpisodicStore(Path(path))
