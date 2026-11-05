"""Ecphory-поиск: субсимволическая активация памяти.

Расширение двухпутевого поиска формулой из спеки (раздел 8.2):

    A_total(v) = B_i + w_sem·Sim + w_val·V_i + M_affect + C_safety + ε(N)

где
    B_i        ACT-R base-level: ln(Σ t^-d), частота и недавность обращений
    Sim        0.65·cos + 0.35·BM25 — семантика и лексика
    V_i        выученная ценность узла (actr_valuation)
    M_affect   mood-congruent: штраф за расхождение PAD, ширина гейтится S
    C_safety   CAMA counterweight: принудительные положительные якоря
    ε(N)       норадреналиновый шум: импульсивные вторжения при возбуждении

Ключевое отличие от прежнего поиска: память **не** нейтральна. При низком
серотонине полоса сужается, и всплывает то, что совпадает с настроением.
Именно это создаёт риск «ИИ-психоза», поэтому counterweight обязателен.

Модуль чистый: не ходит в БД, получает данные снаружи.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Any, Sequence

import numpy as np

from . import decay as decay_mod
from .retrieval import Node, cosine_scores, lateral_inhibition, lexical_scores, tokenize


@dataclass
class EcphoryConfig:
    """Веса и пороги. Значения — из спеки, раздел 3.3."""

    k_base: int = 5
    k_min: int = 2
    k_max: int = 8

    flip_base: int = 3

    # веса компонентов A_total
    base_level_weight: float = 0.22
    semantic_weight: float = 0.30
    decay_weight: float = 0.12
    valuation_weight: float = 0.18

    # mood-congruent
    mood_lambda: float = 0.55
    mood_sigma0: float = 0.35

    # counterweight
    theta_crit: float = 0.35
    s_crit: float = 0.25
    beta_cw: float = 0.85
    min_gold_inject: int = 2

    # шум
    noise_s0: float = 0.02
    noise_alpha_n: float = 0.85
    noise_enabled: bool = True

    # ACT-R
    actr_decay: float = 0.5
    actr_epsilon_sec: float = 1.0


# ------------------------------------------------------------ компоненты

def actr_base_level(access_times: Sequence[float], now: float,
                    decay: float = 0.5,
                    epsilon_sec: float = 1.0) -> float:
    """B_i = ln(Σ (t - t_ij)^-d), нормированная в [-1, 1].

    Внимание: активация ACT-R по природе ОТРИЦАТЕЛЬНА (лог суммы малых
    чисел), и именно так она и должна работать — как штраф за давнее
    или редкое обращение. Поэтому не обрезаем в ноль, а сжимаем tanh.
    """
    if not access_times:
        return -1.0
    total = 0.0
    for ts in access_times:
        age = max(epsilon_sec, now - float(ts))
        total += age ** (-decay)
    if total <= 0:
        return -1.0
    raw = math.log(total)
    return max(-1.0, min(1.0, math.tanh(raw / 5.0)))


def semantic_component(cosine: float, bm25: float) -> float:
    """Sim = 0.65·cos + 0.35·BM25 — один семантический компонент."""
    return 0.65 * max(0.0, cosine) + 0.35 * max(0.0, bm25)


def mood_congruent(pad_node: tuple[float, float, float],
                   pad_now: tuple[float, float, float],
                   serotonin: float,
                   cfg: EcphoryConfig) -> float:
    """M_affect = -λ·‖a_i - a_PAD‖² / (2σ₀²(0.5 + S)), нормированный в [-1, 0].

    При низком S знаменатель мал → штраф за расхождение растёт → туннельное
    настроение. При высоком S полоса широкая → гибкий поиск.

    Нормировка: максимальное ‖·‖² на кубе PAD ∈ [-1,1]³ равно 12 (три оси
    по 2²). Делим на него, чтобы терм был сравним с остальными слагаемыми
    (семантика, ценность), а не забивал их на порядок.
    """
    dist2 = sum((a - b) ** 2 for a, b in zip(pad_node, pad_now))
    bandwidth = max(0.05, 0.5 + max(0.0, min(1.0, serotonin)))
    denominator = 2.0 * (cfg.mood_sigma0 ** 2) * bandwidth
    if denominator <= 0:
        return 0.0
    raw = -cfg.mood_lambda * dist2 / denominator
    # Нормировка НЕ должна содержать bandwidth: иначе сокращение
    # уничтожает зависимость от серотонина, ради которой терм и нужен.
    # Делим на худший случай при фиксированной опорной ширине.
    worst = cfg.mood_lambda * 12.0 / (2.0 * (cfg.mood_sigma0 ** 2))
    if worst <= 0:
        return 0.0
    return max(-1.0, raw / worst)


def noradrenaline_noise(node_id: str, noradrenaline: float,
                        cfg: EcphoryConfig, salt: int = 0) -> float:
    """ε ~ Logistic(0, s(N)), s(N) = s0(1 + α_N·N).

    Детерминированно по node_id — чтобы тесты были воспроизводимы,
    но при высоком N узлы «дёргаются» и в выдачу попадают неожиданные.
    """
    if not cfg.noise_enabled:
        return 0.0
    scale = cfg.noise_s0 * (1.0 + cfg.noise_alpha_n * max(0.0, min(1.0, noradrenaline)))
    seed = hash((node_id, salt)) & 0xFFFFFFFF
    rng = random.Random(seed)
    u = min(0.999, max(0.001, rng.random()))
    # логистическое распределение
    return scale * math.log(u / (1.0 - u))


def dynamic_k(serotonin: float, cfg: EcphoryConfig,
              counterweight: bool = False) -> int:
    """k(t) = floor(k_base · (0.5 + S)), с границами.

    Низкий S — узкая полоса (мало воспоминаний, все «в тон»).
    При counterweight пол минимум 3, чтобы якоря гарантированно влезли.
    """
    s = max(0.0, min(1.0, serotonin))
    k = int(math.floor(cfg.k_base * (0.5 + s)))
    k = max(cfg.k_min, min(cfg.k_max, k))
    if counterweight:
        k = max(k, 3)
    return k


def counterweight_active(pad: tuple[float, float, float], serotonin: float,
                         negative_since: float | None, now: float,
                         cfg: EcphoryConfig, tau_sec: float = 900.0) -> bool:
    """Условие CAMA: длительное отрицательное состояние при низком S."""
    if pad[0] >= -cfg.theta_crit:
        return False
    if serotonin >= cfg.s_crit:
        return False
    if negative_since is None:
        return False
    return (now - negative_since) >= tau_sec


@dataclass
class EcphoryHit:
    """Результат одного узла с разложением по компонентам."""

    node: Node
    base_level: float = 0.0
    semantic: float = 0.0
    valuation: float = 0.0
    mood: float = 0.0
    safety: float = 0.0
    noise: float = 0.0
    total: float = 0.0
    is_gold: bool = False
    provenance_class: str = "OBSERVATION"

    def to_dict(self) -> dict[str, Any]:
        data = self.node.to_dict()
        data.update({
            "score": round(self.total, 4),
            "total": round(self.total, 4),
            "base_level": round(self.base_level, 4),
            "semantic": round(self.semantic, 4),
            "valuation": round(self.valuation, 4),
            "mood": round(self.mood, 4),
            "safety": round(self.safety, 4),
            "noise": round(self.noise, 4),
            "is_gold": self.is_gold,
            "provenance_class": self.provenance_class,
        })
        return data


def dedupe_by_content(hits: dict[str, "EcphoryHit"],
                      scores: dict[str, float],
                      threshold: float = 0.85) -> dict[str, float]:
    """Убрать дубликаты по содержанию, оставив сильнейший.

    Почему не по скору: у однотипных событий («событие номер N») скоры
    закономерно близки, и торможение по величине режет именно верные
    попадания. Настоящее назначение латерального торможения — не дать
    пяти копиям одного текста занять весь контекст, поэтому сравниваем
    сами тексты.
    """
    ordered = sorted(scores.items(), key=lambda kv: -kv[1])
    kept: dict[str, float] = {}
    kept_tokens: list[set[str]] = []
    for node_id, score in ordered:
        hit = hits.get(node_id)
        if hit is None:
            continue
        tokens = set(tokenize(hit.node.text))
        if kept_tokens:
            duplicate = False
            for other in kept_tokens:
                if not tokens or not other:
                    continue
                overlap = len(tokens & other) / max(1, len(tokens | other))
                if overlap >= threshold:
                    duplicate = True
                    break
            if duplicate:
                continue
        kept[node_id] = score
        kept_tokens.append(tokens)
    return kept


def ecphory_retrieve(
    query: str,
    query_embedding: Sequence[float] | None,
    episode_ids: Sequence[str],
    texts: Sequence[str],
    timestamps: Sequence[float],
    kinds: Sequence[str],
    matrix: np.ndarray,
    neighbours: dict[str, list[tuple[str, str, float]]],
    *,
    pad_now: tuple[float, float, float] = (0.0, 0.0, 0.0),
    serotonin: float = 0.5,
    noradrenaline: float = 0.4,
    valuations: dict[str, float] | None = None,
    pads: dict[str, tuple[float, float, float]] | None = None,
    access: dict[str, list[float]] | None = None,
    gold_ids: set[str] | None = None,
    provenance: dict[str, str] | None = None,
    counterweight: bool = False,
    cfg: EcphoryConfig | None = None,
    now: float | None = None,
    top_k: int | None = None,
    apply_inhibition: bool = True,
) -> list[EcphoryHit]:
    """Полный поиск: A_total, затем распространение по графу.

    Возвращает хиты с разложением по компонентам — это позволяет
    дашборду и тестам показать, ПОЧЕМУ воспоминание всплыло.
    """
    cfg = cfg or EcphoryConfig()
    stamp = now or 0.0
    count = len(episode_ids)
    if count == 0:
        return []

    k = top_k if top_k is not None else dynamic_k(serotonin, cfg, counterweight)
    valuations = valuations or {}
    pads = pads or {}
    access = access or {}
    gold_ids = gold_ids or set()
    provenance = provenance or {}

    tokens = tokenize(query)
    if query_embedding is not None and matrix.size and matrix.shape[0] == count:
        semantic_raw = cosine_scores(np.asarray(query_embedding, dtype=np.float32),
                                     matrix)
    else:
        semantic_raw = np.zeros(count, dtype=np.float32)
    lexical = lexical_scores(tokens, texts)

    hits: dict[str, EcphoryHit] = {}
    scores: dict[str, float] = {}

    for i, episode_id in enumerate(episode_ids):
        kind = kinds[i] if i < len(kinds) else "EVENT"
        node = Node(episode_id=episode_id, text=texts[i],
                    ts=timestamps[i], kind=kind)

        base_level = actr_base_level(access.get(episode_id, []), stamp,
                                     cfg.actr_decay, cfg.actr_epsilon_sec)
        semantic = semantic_component(float(semantic_raw[i]), float(lexical[i]))
        valuation = max(0.0, min(1.0, valuations.get(episode_id, 0.0)))
        pad_node = pads.get(episode_id, (0.0, 0.0, 0.0))
        mood = mood_congruent(pad_node, pad_now, serotonin, cfg)
        safety = 0.0
        if counterweight and episode_id in gold_ids and pad_node[0] > 0.2:
            safety = cfg.beta_cw
        noise = noradrenaline_noise(episode_id, noradrenaline, cfg)

        freshness = decay_mod.type_decay(max(0.0, stamp - timestamps[i]), kind)
        total = (
            cfg.base_level_weight * base_level
            + cfg.semantic_weight * semantic
            + cfg.decay_weight * freshness
            + cfg.valuation_weight * valuation
            + mood
            + safety
            + noise
        )
        hit = EcphoryHit(
            node=node, base_level=base_level, semantic=semantic,
            valuation=valuation, mood=mood, safety=safety, noise=noise,
            total=total, is_gold=episode_id in gold_ids,
            provenance_class=provenance.get(episode_id, "OBSERVATION"),
        )
        hits[episode_id] = hit
        scores[episode_id] = total

    # распространение активации по графу (flip-путь)
    from .retrieval import spreading_activation
    spread = spreading_activation(scores, neighbours, beta=0.35, hops=2)
    cutoff = sorted(scores.values(), reverse=True)[:max(1, k)]
    floor = cutoff[-1] if cutoff else 0.0
    for node_id, value in spread.items():
        hit = hits.get(node_id)
        if hit is None:
            continue
        if value > hit.total:
            hit.total = value
            scores[node_id] = value

    if apply_inhibition:
        scores = dedupe_by_content(hits, scores)

    ordered = sorted(scores.items(), key=lambda kv: -kv[1])
    selected: list[EcphoryHit] = []
    seen: set[str] = set()

    # counterweight обязан вставить якоря, даже если они не в топе
    if counterweight:
        anchors = [nid for nid in gold_ids if nid in hits][:cfg.min_gold_inject]
        for anchor in anchors:
            selected.append(hits[anchor])
            seen.add(anchor)

    for node_id, _ in ordered:
        if len(selected) >= k:
            break
        if node_id in seen:
            continue
        selected.append(hits[node_id])
        seen.add(node_id)

    # SELF_INFERENCE не должен доминировать: помечаем, но не выкидываем
    selected.sort(key=lambda h: -h.total)
    return selected[:max(k, len(selected))]
