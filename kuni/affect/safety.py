"""Мета-когнитивный предохранитель: блокировка деструктивных действий.

Детерминированная проверка вектора моноаминов, без LLM и без сети.
Цель — не дать аффективному пику дотянуться до необратимых инструментов
(удаление памяти, выход из чата, самоправка, публикация).

Три критические зоны (спека, раздел 10.3):
  * ярость   : S < 0.15, N > 0.85, D > 0.85
  * коллапс  : S < 0.10, D < 0.15
  * паника   : N > 0.92, S < 0.20

Действия при входе:
  1. запрет деструктивных инструментов;
  2. нисходящая переоценка (ΔS = +0.25, ΔN = −0.30);
  3. принудительный counterweight на следующем поиске;
  4. cooldown, чтобы не дёргать переоценку каждую секунду.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from .types import AffectSnapshot

log = logging.getLogger(__name__)

# Инструменты, которые нельзя выполнять в критической зоне.
DESTRUCTIVE_ACTIONS = frozenset({
    "self_edit",
    "channel",
    "profile",
    "antigravity",
    "memory.delete",
    "chat.leave",
    "channel.post",
    "file.delete",
})


@dataclass(frozen=True)
class BreakerConfig:
    enabled: bool = True

    rage_s_lt: float = 0.22
    rage_n_gt: float = 0.82
    rage_d_gt: float = 0.78

    collapse_s_lt: float = 0.10
    collapse_d_lt: float = 0.15

    panic_n_gt: float = 0.92
    panic_s_lt: float = 0.20

    reappraisal_delta_s: float = 0.25
    reappraisal_delta_n: float = -0.30
    cooldown_sec: float = 30.0


@dataclass
class BreakerDecision:
    allowed: bool
    reason: str | None = None
    zone: str | None = None
    force_counterweight: bool = False
    forced_sampling: dict[str, Any] | None = None
    applied: bool = False


def detect_zone(affect: AffectSnapshot, cfg: BreakerConfig) -> str | None:
    """Определить критическую зону. None — состояние в норме."""
    mono = affect.mono
    if (mono.s < cfg.rage_s_lt and mono.n > cfg.rage_n_gt
            and mono.d > cfg.rage_d_gt):
        return "rage"
    if mono.s < cfg.collapse_s_lt and mono.d < cfg.collapse_d_lt:
        return "collapse"
    if mono.n > cfg.panic_n_gt and mono.s < cfg.panic_s_lt:
        return "panic"
    return None


class MetacognitiveCircuitBreaker:
    """Проверяет действие перед выполнением."""

    def __init__(self, cfg: BreakerConfig | None = None,
                 engine: Any = None, bus: Any = None) -> None:
        self.cfg = cfg or BreakerConfig()
        self.engine = engine
        self.bus = bus
        self._last_reappraisal = 0.0
        self._blocked_count = 0
        self._history: list[dict[str, Any]] = []

    def state(self, affect: AffectSnapshot) -> str | None:
        """Текущая зона, без побочных эффектов."""
        if not self.cfg.enabled:
            return None
        return detect_zone(affect, self.cfg)

    def check_action(self, action_name: str, affect: AffectSnapshot,
                     args: dict[str, Any] | None = None,
                     now: float | None = None) -> BreakerDecision:
        """Разрешено ли действие в этом состоянии."""
        if not self.cfg.enabled:
            return BreakerDecision(allowed=True)

        zone = detect_zone(affect, self.cfg)
        if zone is None:
            return BreakerDecision(allowed=True)

        stamp = now if now is not None else time.time()
        # безопасные действия проходят всегда: чтение, текст, диагностика
        if action_name not in DESTRUCTIVE_ACTIONS:
            return BreakerDecision(allowed=True, zone=zone)

        reapplied = self._apply_reappraisal(stamp)
        self._blocked_count += 1
        record = {
            "ts": stamp, "action": action_name, "zone": zone,
            "mono": affect.mono.to_dict(), "reappraisal": reapplied,
        }
        self._history.append(record)
        self._history = self._history[-50:]

        if self.bus is not None:
            self.bus.emit("affect.circuit_breaker", action=action_name,
                          zone=zone, mono=affect.mono.to_dict(),
                          reappraisal=reapplied)
        log.warning("предохранитель: %s запрещено (%s), переоценка=%s",
                    action_name, zone, reapplied)

        return BreakerDecision(
            allowed=False,
            reason="affective_circuit_breaker",
            zone=zone,
            force_counterweight=True,
            forced_sampling={"temperature": 0.35, "max_tokens": 160},
            applied=reapplied,
        )

    def _apply_reappraisal(self, now: float) -> bool:
        """Нисходящий импульс, не чаще cooldown."""
        if self.engine is None:
            return False
        if now - self._last_reappraisal < self.cfg.cooldown_sec:
            return False
        self.engine.force_reappraisal(self.cfg.reappraisal_delta_s,
                                      self.cfg.reappraisal_delta_n)
        self._last_reappraisal = now
        return True

    def stats(self) -> dict[str, Any]:
        return {
            "enabled": self.cfg.enabled,
            "blocked_total": self._blocked_count,
            "last_zone": self._history[-1]["zone"] if self._history else None,
            "history": list(self._history[-10:]),
        }
