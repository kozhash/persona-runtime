"""Фоновый режим: она в сети всегда, а память подшивается по чуть-чуть.

Зачем: ночной прогон делает всё разом в 4 утра. Минусы очевидны — до утра
память «сырая», а в 4 часа машина может быть выключена, и тогда прохода не
будет вовсе. Здесь тот же Consolidator, но разбитый на короткие проходы по
расписанию: захват тегов, связи, дистилляция, забывание — отдельными шагами.

Режим переключается в конфиге: `background.mode = "sleep" | "background"`.
`sleep` — прежнее поведение (один прогон в consolidation_hour).
`background` — всё понемногу и постоянно.
"""
from __future__ import annotations

import logging
import time
from typing import Any

log = logging.getLogger(__name__)


class BackgroundLoop:
    """Короткие проходы по памяти вместо одного ночного."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self.last_pass_ts = 0.0
        self.last_pass_stats: dict[str, Any] = {}
        self.passes = 0
        self.errors = 0

    # ------------------------------------------------------------- режим

    def mode(self) -> str:
        raw = str(getattr(self.app.config.current.background, "mode", "background"))
        raw = raw.strip().lower()
        return raw if raw in ("sleep", "background") else "background"

    def is_background(self) -> bool:
        return self.mode() == "background"

    # ------------------------------------------------------------- проходы

    async def tick(self) -> None:
        """Один короткий проход: захват + связи. Дёшево, без LLM."""
        cfg = self.app.config.current
        bcfg = cfg.background
        if not self.is_background():
            return
        if self.app.sleep_cycle.sleeping:
            return                      # не мешаем ручному сну

        now = time.time()
        stats: dict[str, Any] = {"ts": now}

        # 1. NREM: лабильный буфер -> долговременная память
        try:
            captured = await self.app.sleep_cycle._nrem(now)
            stats["captured"] = captured
        except Exception as e:
            self.errors += 1
            log.warning("фон: захват тегов не удался: %s", e)
            stats["captured"] = 0

        # 2. Переигровка: связать похожие эпизоды рёбрами
        try:
            pairs = self.app.sleep_cycle._replay(now)
            stats["pairs"] = pairs
        except Exception as e:
            self.errors += 1
            log.warning("фон: связи не удались: %s", e)
            stats["pairs"] = 0

        self.last_pass_ts = now
        self.last_pass_stats = stats
        self.passes += 1

        if stats.get("captured") or stats.get("pairs"):
            log.info("фон: захвачено=%s связей=%s (проход %d)",
                     stats.get("captured"), stats.get("pairs"), self.passes)

    async def distill_step(self) -> None:
        """Дистилляция пачки кластеров через LLM — самый дорогой шаг.

        Отдельно от tick: LLM медленный, его нельзя делать каждые 20 минут
        на всех эпизодах. Берём только недавние и не больше лимита.
        """
        cfg = self.app.config.current
        bcfg = cfg.background
        if not self.is_background() or self.app.sleep_cycle.sleeping:
            return
        if self.app.modules.get("llm") != "ok":
            return

        consolidator = self.app.sleep_cycle
        limit = max(1, int(bcfg.max_episodes_per_pass))
        now = time.time()
        try:
            episodes = consolidator.store.older_than(now, stage="consolidated")
            if not episodes:
                return
            episodes = episodes[-limit:]
            by_id = {e.episode_id: e for e in episodes}
            ids = list(by_id)

            _, matrix = consolidator.store.all_embeddings()
            all_ids, _ = consolidator.store.all_embeddings()
            index_of = {eid: i for i, eid in enumerate(all_ids)}
            rows = [index_of[eid] for eid in ids if eid in index_of]
            if not rows:
                return
            sub = matrix[rows]

            from ..memory.consolidation import ConsolidationStats
            stats = ConsolidationStats(started_at=now)
            clusters = consolidator._cluster(ids, sub,
                                            cfg.memory.similarity_threshold)
            done = 0
            for cluster_ids in clusters[:limit]:
                cluster = [by_id[i] for i in cluster_ids if i in by_id]
                if len(cluster) < 2:
                    continue
                await consolidator._distill_cluster(cluster, stats, now)
                done += 1
            self.last_pass_stats["distilled"] = done
            if done:
                log.info("фон: дистиллировано %d кластеров", done)
        except Exception as e:
            self.errors += 1
            log.warning("фон: дистилляция не удалась: %s", e)

    async def decay_step(self) -> None:
        """Раз в сутки: забывание и затухание рёбер."""
        if not self.is_background() or self.app.sleep_cycle.sleeping:
            return
        cfg = self.app.config.current
        now = time.time()
        try:
            from ..memory.consolidation import ConsolidationStats
            stats = ConsolidationStats(started_at=now)
            self.app.sleep_cycle._forget(stats, now)
            self.app.sleep_cycle.store.decay_edges(cfg.memory.edge_decay, now=now)
            log.info("фон: забывание — в архив %d, рёбер удалено %d",
                     stats.archived, stats.edges_pruned)
        except Exception as e:
            self.errors += 1
            log.warning("фон: забывание не удалось: %s", e)

    # ------------------------------------------------------------- сводка

    def snapshot(self) -> dict[str, Any]:
        return {
            "mode": self.mode(),
            "passes": self.passes,
            "errors": self.errors,
            "last_pass_ts": self.last_pass_ts,
            "last_pass": self.last_pass_stats,
        }

    # ------------------------------------------------------------- установка

    def set_mode(self, mode: str) -> str:
        """Переключить режим и записать в config.toml."""
        mode = str(mode).strip().lower()
        aliases = {"on": "background", "вкл": "background", "фон": "background",
                   "background": "background",
                   "sleep": "sleep", "сон": "sleep", "off": "sleep"}
        mode = aliases.get(mode, mode)
        if mode not in ("sleep", "background"):
            raise ValueError("режимы: sleep | background")

        import re
        path = self.app.config.path
        text = path.read_text(encoding="utf-8")
        if re.search(r'^\[background\]', text, re.M):
            # заменить mode внутри секции [background]
            def _sub(block: str) -> str:
                if re.search(r'^mode\s*=', block, re.M):
                    return re.sub(r'^(mode\s*=\s*)"[^"]*"', rf'\1"{mode}"',
                                  block, count=1, flags=re.M)
                return block.replace("[background]", f'[background]\nmode = "{mode}"', 1)
            text = re.sub(r'(?ms)^\[background\].*?(?=^\[|\Z)', _sub, text)
        else:
            text = text.rstrip() + f'\n[background]\nmode = "{mode}"\n'
        path.write_text(text, encoding="utf-8")
        self.app.config.maybe_reload(force=True)
        if self.app.bus:
            self.app.bus.emit("background.mode", mode=mode)
        log.info("режим памяти: %s", mode)
        return mode
