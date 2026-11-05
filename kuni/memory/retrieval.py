"""Извлечение памяти: двухпутевая активация, а не просто kNN.

Ресерч (HeLa-Mem, HippoRAG 2, SYNAPSE) сходится в одном: косинусная
близость видит только лексическое сходство. Чтобы вспомнить событие,
которое лексически не похоже на запрос, но связано опытом, нужны
два пути.

  1. Базовый путь: семантика (косинус) + лексика (BM25-подобное
     пересечение) + свежесть, умноженные на затухание по типу знания.
  2. Ассоциативный путь (flip): активация перетекает по рёбрам графа
     пропорционально весам Хебба — «одно потянуло другое».

Плюс латеральное торможение: сильно активированный узел приглушает
соседей того же уровня, а рёбра `prevents` дают отрицательный вклад
(ГАМК-аналог). Без этого поиск отдаёт пять почти одинаковых записей.

Модуль чистый: принимает матрицы и списки, не ходит в сеть.
"""
from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

import numpy as np

from . import decay as decay_mod

_WORD_RE = re.compile(r"[0-9a-zA-Zа-яА-ЯёЁұүіңғқөһә]+", re.UNICODE)
STOPWORDS = {
    "и", "в", "во", "не", "что", "он", "на", "я", "с", "со", "как", "а", "то",
    "все", "она", "так", "его", "но", "да", "ты", "к", "у", "же", "вы", "за",
    "бы", "по", "только", "ее", "мне", "было", "вот", "от", "меня", "еще",
    "нет", "о", "из", "ему", "теперь", "когда", "даже", "ну", "вдруг", "ли",
    "если", "уже", "или", "ни", "быть", "был", "него", "до", "вас", "нибудь",
    "опять", "уж", "вам", "ведь", "там", "потом", "себя", "ничего", "ей",
    "может", "они", "тут", "где", "есть", "надо", "ней", "для", "мы", "тебя",
    "их", "чем", "была", "сам", "чтоб", "без", "будто", "чего", "раз", "тоже",
    "себе", "под", "будет", "ж", "тогда", "кто", "этот", "того", "потому",
    "этого", "какой", "совсем", "ним", "здесь", "этом", "один", "почти",
    "мой", "тем", "чтобы", "нее", "сейчас", "были", "куда", "зачем", "всех",
    "никогда", "можно", "при", "наконец", "два", "об", "другой", "хоть",
    "после", "над", "больше", "тот", "через", "эти", "нас", "про", "всего",
    "них", "какая", "много", "разве", "три", "эту", "моя", "впрочем",
    "хорошо", "свою", "этой", "перед", "иногда", "лучше", "чуть", "том",
    "нельзя", "такой", "им", "более", "всегда", "конечно", "всю", "между",
}


def tokenize(text: str) -> list[str]:
    """Слова в нижнем регистре без стоп-слов — основа лексического скора."""
    return [w.lower() for w in _WORD_RE.findall(text or "")
            if len(w) > 1 and w.lower() not in STOPWORDS]


@dataclass
class Node:
    """Кандидат на всплытие: эпизод с базовым и итоговым скором."""
    episode_id: str
    text: str
    ts: float
    kind: str = "EVENT"
    base_score: float = 0.0
    score: float = 0.0
    via: str = ""
    path: list[str] = field(default_factory=list)
    #: атрибуция (27.10): без неё информация теряется на выходе поиска,
    #: даже если в базе она есть — модель получает текст без автора.
    chat_id: int = 0
    speaker_id: int = 0
    speaker_name: str = ""
    chat_title: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.episode_id,
            "episode_id": self.episode_id,
            "text": self.text,
            "ts": self.ts,
            "when": time.strftime("%Y-%m-%d %H:%M", time.localtime(self.ts)),
            "score": round(self.score, 4),
            "base_score": round(self.base_score, 4),
            "similarity": round(max(0.0, min(1.0, self.score)), 3),
            "via": self.via,
            "path": list(self.path),
            "chat_id": self.chat_id,
            "speaker_id": self.speaker_id,
            "speaker_name": self.speaker_name,
            "chat_title": self.chat_title,
        }


def cosine_scores(query: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    """Косинус запроса ко всем строкам. Безопасно к нулевым векторам."""
    if matrix.size == 0 or matrix.shape[0] == 0:
        return np.zeros(0, dtype=np.float32)
    q = np.asarray(query, dtype=np.float32).ravel()
    q_norm = float(np.linalg.norm(q))
    if q_norm == 0:
        return np.zeros(matrix.shape[0], dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1)
    norms[norms == 0] = 1e-9
    return (matrix @ q) / (norms * q_norm)


def lexical_scores(query_tokens: Sequence[str], corpus: Sequence[str]) -> np.ndarray:
    """BM25-подобный скор: IDF-взвешенное пересечение токенов."""
    n = len(corpus)
    if n == 0:
        return np.zeros(0, dtype=np.float32)
    qset = set(query_tokens)
    if not qset:
        return np.zeros(n, dtype=np.float32)
    tokenized = [set(tokenize(text)) for text in corpus]
    df: dict[str, int] = {}
    for toks in tokenized:
        for token in toks:
            df[token] = df.get(token, 0) + 1
    scores = np.zeros(n, dtype=np.float32)
    for i, toks in enumerate(tokenized):
        if not toks:
            continue
        hit = 0.0
        for token in qset:
            if token in toks:
                # BM25 IDF без параметра k1: редкое слово весит больше
                idf = math.log(1.0 + (n - df.get(token, 0) + 0.5) / (df.get(token, 0) + 0.5))
                hit += idf
        # нормировка на длину запроса, чтобы длинные запросы не доминировали
        scores[i] = hit / max(1.0, math.sqrt(len(qset)))
    if scores.max() > 0:
        scores = scores / scores.max()
    return scores


def temporal_weight(ts: float, now: float, kind: str) -> float:
    """Затухание по типу знания: свежее и «долгоживущее» весит больше."""
    age = max(0.0, now - ts)
    return decay_mod.type_decay(age, kind)


def base_activation(
    semantic: float, lexical: float, freshness: float,
    *,
    w_semantic: float = 0.55,
    w_lexical: float = 0.25,
    w_freshness: float = 0.20,
) -> float:
    """Базовый скор узла: смесь семантики, лексики и свежести."""
    return (
        w_semantic * max(0.0, semantic)
        + w_lexical * max(0.0, lexical)
        + w_freshness * max(0.0, min(1.0, freshness))
    )


def spreading_activation(
    base: dict[str, float],
    neighbours: dict[str, list[tuple[str, str, float]]],
    *,
    beta: float = 0.35,
    hops: int = 2,
    damping: float = 0.65,
) -> dict[str, float]:
    """Распространение активации по графу (приближение Personalized PageRank).

    base       — стартовые скоры узлов запроса
    neighbours — node -> [(dst, kind, weight)]

    Отрицательные рёбра `prevents` вычитают активацию (латеральное
    торможение / ГАМК). Каждый шаг ослабляется damping, чтобы дальние
    узлы не забивали прямые совпадения.
    """
    scores = dict(base)
    frontier = {node: score for node, score in base.items() if score > 0}
    for _ in range(max(0, hops)):
        incoming: dict[str, float] = {}
        for node, activation in frontier.items():
            if activation <= 0:
                continue
            for dst, kind, weight in neighbours.get(node, []):
                contribution = activation * weight * beta * damping
                if kind == "prevents":
                    contribution = -abs(contribution)
                incoming[dst] = incoming.get(dst, 0.0) + contribution
        if not incoming:
            break
        for dst, delta in incoming.items():
            scores[dst] = scores.get(dst, 0.0) + delta
        # следующий фронт — только положительно выросшие узлы
        frontier = {node: delta for node, delta in incoming.items() if delta > 0}
    return scores


def lateral_inhibition(scores: dict[str, float], *, radius: float = 0.15,
                       strength: float = 0.30) -> dict[str, float]:
    """Подавить почти одинаковые по скору узлы, оставив сильнейший.

    Имитация латерального торможения: без него пять воспоминаний
    об одном и том же событии занимают весь бюджет контекста.
    """
    if not scores:
        return {}
    ordered = sorted(scores.items(), key=lambda kv: -kv[1])
    kept: dict[str, float] = {}
    for node_id, score in ordered:
        suppressed = False
        for kept_id, kept_score in kept.items():
            if abs(kept_score - score) <= radius * max(1e-9, abs(kept_score)):
                suppressed = True
                break
        if not suppressed:
            kept[node_id] = score
    return kept


def dual_path_retrieve(
    query: str,
    query_embedding: Sequence[float] | None,
    episode_ids: Sequence[str],
    texts: Sequence[str],
    timestamps: Sequence[float],
    kinds: Sequence[str],
    matrix: np.ndarray,
    neighbours: dict[str, list[tuple[str, str, float]]],
    *,
    top_k: int = 5,
    flip_m: int = 3,
    now: float | None = None,
    min_score: float = 0.05,
    apply_inhibition: bool = True,
    beta: float = 0.35,
    hops: int = 2,
    attributions: Sequence[dict] | None = None,
) -> list[Node]:
    """Полный двухпутевой поиск. Возвращает отсортированные узлы.

    Реализует формулу из ресерча:
        S_base(v_i) = (sim(q,e_i) + alpha*keyword_match) * gamma(v_i)
        S(v_j)      = S_base(v_j) + beta * sum_i S_base(v_i) * w_ij
        R_final     = Top-k(S_base) UNION Top-m(S_flip)
    """
    stamp = now or time.time()
    count = len(episode_ids)
    if count == 0:
        return []

    tokens = tokenize(query)
    if query_embedding is not None and matrix.size and matrix.shape[0] == count:
        semantic = cosine_scores(np.asarray(query_embedding, dtype=np.float32), matrix)
    else:
        semantic = np.zeros(count, dtype=np.float32)
    lexical = lexical_scores(tokens, texts)

    base: dict[str, float] = {}
    nodes: dict[str, Node] = {}
    for i, episode_id in enumerate(episode_ids):
        kind = kinds[i] if i < len(kinds) else "EVENT"
        freshness = temporal_weight(timestamps[i], stamp, kind)
        score = base_activation(float(semantic[i]), float(lexical[i]), freshness)
        base[episode_id] = score
        # 27.10: атрибуция нужна на выходе поиска, иначе модель получает
        # текст без автора и путает, кто это сказал.
        attr = attributions[i] if attributions and i < len(attributions) else {}
        nodes[episode_id] = Node(
            episode_id=episode_id, text=texts[i], ts=timestamps[i], kind=kind,
            base_score=score, score=score, via="прямое совпадение",
            chat_id=int(attr.get("chat_id") or 0),
            speaker_id=int(attr.get("speaker_id") or 0),
            speaker_name=str(attr.get("speaker_name") or ""),
            chat_title=str(attr.get("chat_title") or ""),
        )

    # flip-путь: активация перетекает по ассоциациям
    spread = spreading_activation(base, neighbours, beta=beta, hops=hops)
    base_cutoff = sorted(base.values(), reverse=True)[:top_k]
    floor = base_cutoff[-1] if base_cutoff else 0.0
    associative: dict[str, float] = {}
    for node_id, score in spread.items():
        node = nodes.get(node_id)
        if node is None:
            continue            # узел вне текущего среза (другой источник)
        if score > node.base_score:
            node.score = score
        if score > floor and node.base_score <= floor:
            associative[node_id] = score

    # flip-путь: до m узлов, всплывших ТОЛЬКО по ассоциации
    flip = sorted(associative.items(), key=lambda kv: -kv[1])[:flip_m]
    for node_id, score in flip:
        node = nodes[node_id]
        node.via = "потянуло по ассоциации"
        # помечаем путь через самого сильного соседа
        best = max(neighbours.get(node_id, [("", "", 0.0)]),
                   key=lambda nb: nb[2], default=None)
        if best and best[0]:
            node.path = [best[0], node_id]

    scored = {node_id: node.score for node_id, node in nodes.items()
              if node.score >= min_score}
    if apply_inhibition:
        scored = lateral_inhibition(scored)

    top = sorted(scored.items(), key=lambda kv: -kv[1])[:top_k]
    result = [nodes[node_id] for node_id, _ in top]
    for node in result:
        if node.via == "прямое совпадение" and node.score > node.base_score + 1e-6:
            node.via = "подкреплено ассоциацией"
    return result


def rank_facts(facts: Iterable[Any], subject: str | None = None,
               now: float | None = None) -> list[Any]:
    """Детерминированный выбор актуального факта: max(serial), не мнение LLM."""
    stamp = now or time.time()
    items = [f for f in facts
             if getattr(f, "valid_to", None) is None
             and (subject is None or getattr(f, "subject", "") == subject)]
    return sorted(items, key=lambda f: (-int(getattr(f, "serial", 0)),
                                        -float(getattr(f, "confidence", 0.0))))
