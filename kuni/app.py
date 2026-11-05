"""Точка сборки: строит все модули и связывает их событиями.

Принцип изоляции: каждый модуль включается/выключается конфигом и
падает независимо. Нет tdjson — остальное живёт. Нет LLM — работает
echo-диагностика. Нет SD — скилл draw просто не регистрируется.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import sqlite3
import sys
import time
from collections import defaultdict, deque
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .character.emotions import Emotions, EmotionState
from .character.persona import PersonaBuilder
from .character.prompts import PromptLibrary
from .affect import AffectConfig, AffectService
from .affect.engine import EngineConfig
from .affect.modulation import ModulationConfig
from .affect.safety import BreakerConfig
from .memory.ecphory import EcphoryConfig
from .core.bus import Bus
from .core.config import ConfigService
from .core.night_queue import NightQueue, QueuedMessage
from .infra.metrics import Metrics
from .infra.scheduler import Scheduler
from .llm.client import LLMClient, LLMError, extract_json
from .memory.consolidation import Consolidator, estimate_importance
from .memory.diary import Diary
from .memory.episodic import EpisodicStore
from .memory.working import WorkingMemory
from .skills import SkillRegistry
from .skills.builtin import ImageSkill, WebSearchSkill, memory_skills
from .skills.voice import VoiceService
from .telegram.client import IncomingMessage
from .telegram import splitter as _splitter, TelegramModule
from .core import clock

log = logging.getLogger(__name__)


class KuniApp:
    #: 28.10: сколько мут считается актуальным. Дальше делаем одну пробную
    #: отправку — иначе флаг не снять никогда (дедлок, нашёл критик).
    MUTE_TTL_SEC = 3 * 3600.0
    #: 28.10: минимальный зазор между ответами владельцу в ОДНОЙ группе.
    #: Без него она отвечала на каждую его строку: 8 сообщений за 11 секунд.
    OWNER_GROUP_GAP_SEC = 90.0
    #: 28.10: старше этого — бэклог TDLib после рестарта, а не разговор.
    #: 3 минуты: человек ещё может ждать ответа, но часовой давности — нет.
    STALE_MESSAGE_SEC = 180.0
    def __init__(self, config_path: Path, interactive: bool = False) -> None:
        self.root = config_path.parent
        self.config = ConfigService(config_path)
        cfg = self.config.current
        self.data_dir = (self.root / cfg.general.data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.bus = Bus()
        self.modules: dict[str, Any] = {"app": self}

        # --- слой данных (общая sqlite для эмоций и задач)
        self.db = sqlite3.connect(str(self.data_dir / "kuni.db"))
        # память: эпизоды + граф + семантические факты в одной базе
        self.diary_store = EpisodicStore(self.data_dir / "memory.db")
        if not cfg.memory.enabled:
            # обратная совместимость: старая плоская база
            self.diary_store = EpisodicStore(self.data_dir / "diary.db")

        # --- персонаж
        self.prompts = PromptLibrary((self.root / cfg.general.prompts_dir).resolve())
        self.emotions = Emotions(self.db, self.bus)
        # журнал причин: что повлияло на состояние и на какое сообщение
        from .character.emotion_log import EmotionLog
        self.emotion_log = EmotionLog(self.db, self.bus)
        # аффект: моноамины + гомеостаз + безопасность (новая система)
        self.affect = AffectService(
            self.data_dir / "memory.db",
            config=self._affect_config(cfg),
            bus=self.bus,
            owner_chat_id=cfg.general.papik_chat_id or 0,
        )
        self.persona = PersonaBuilder(self.prompts, self.emotions, self.config,
                                      affect=self.affect)

        # --- llm: основной мозг + отдельные эндпоинты для функций
        llm_cfg = cfg.llm
        self.llm = LLMClient(llm_cfg.base_url, llm_cfg.api_key, llm_cfg.model,
                             llm_cfg.embed_model, self.bus)
        self.vision_llm = self._endpoint_client(llm_cfg.vision, llm_cfg.vision_model or llm_cfg.model)
        self.embed_llm = self._endpoint_client(llm_cfg.embedding, llm_cfg.embed_model or llm_cfg.model)
        self.emotion_llm = self._endpoint_client(llm_cfg.emotion, llm_cfg.model)
        self.sleep_llm = self._endpoint_client(llm_cfg.sleep, llm_cfg.model)

        # --- память (эмбеддинги — через свой эндпоинт, если задан)
        self.diary = Diary(self.diary_store, self.embed_llm, self.bus)
        self.working = WorkingMemory(self.data_dir / "working_memory.md", self.bus)
        self.sleep_cycle = Consolidator(
            self.diary_store, working=self.working, embedder=self.embed_llm,
            distill=self._build_distill(), bus=self.bus,
        )
        # фоновый режим: память подшивается по чуть-чуть, а не одним прогоном
        from .memory.background import BackgroundLoop
        self.background = BackgroundLoop(self)

        # --- скиллы
        self.skills = SkillRegistry(self.bus)
        for skill in memory_skills(self.diary, self.working):
            self.skills.register(skill)
        if cfg.images.enabled:
            self.skills.register(ImageSkill(
                cfg.images.url, cfg.images.steps,
                llm=self.vision_llm, prompts=self.prompts,
                character_name=cfg.general.character_name,
                appearance=self.prompts.get(
                    "character_appearance", name=cfg.general.character_name),
                quality_gate=cfg.prompts.image_quality_gate,
            ).skill())
        self.skills.register(WebSearchSkill().skill())
        self.voice = VoiceService(cfg.voice.whisper_url, cfg.voice.tts_url,
                                  cfg.voice.tts_voice, cfg.voice.tts_model)

        # --- телеграм
        self.telegram = TelegramModule(self.config, self.bus, self.data_dir / "tg",
                                       interactive=interactive)
        self.telegram.on_message = self.handle_message
        self.telegram.on_social = self._on_social

        # --- руки: файлы на компьютере хозяина + гейт одобрений
        from .harness import ApprovalManager
        from .harness.fs_skill import FsSkill
        from .harness.paths import PathPolicy
        hcfg = cfg.harness
        self.approvals = ApprovalManager(
            self.telegram, cfg.general.papik_chat_id, self.bus)
        self.telegram.on_callback = self._on_callback
        self.path_policy = PathPolicy(
            hcfg.roots, hcfg.deny_patterns,
            hcfg.max_read_bytes, hcfg.max_write_bytes)
        if hcfg.enabled:
            self.skills.register(FsSkill(
                self.path_policy, self.approvals, self.bus).skill())
            from .harness.code_task import CodeTaskSkill
            self.code_task = CodeTaskSkill(
                self.path_policy, hcfg, self.approvals,
                self.data_dir, self.bus, on_progress=self._harness_progress)
            self.skills.register(self.code_task.skill())
            from .harness.shell_skill import ShellSkill
            self.shell_skill = ShellSkill(
                self.path_policy, hcfg, self.approvals, self.data_dir, self.bus)
            self.skills.register(self.shell_skill.skill())

            # её собственный архив + операции с сообщениями
            from .skills.files import FilesSkill
            from .skills.messenger import MessengerSkill
            self.vault = Path(hcfg.vault_path).expanduser()
            self.vault.mkdir(parents=True, exist_ok=True)
            self.skills.register(FilesSkill(
                self.vault, self.telegram, self.approvals, bus=self.bus).skill())
            self.skills.register(MessengerSkill(
                self.telegram, self.approvals, self.bus).skill())
            log.info("руки включены: %s", self.path_policy.describe())
            log.info("архив: %s", self.vault)
        else:
            self.code_task = None
            self.shell_skill = None
            self.vault = None

        # --- скиллы, которым нужен телеграм: самоправка, канал, профиль/сторис
        from .skills.self_edit import SelfEditSkill
        from .skills.channel import ChannelSkill
        from .skills.profile import ProfileSkill
        from .skills.antigravity import AntigravitySkill
        from .skills.reaction import ReactionSkill
        # реакция как инструмент: текстовая команда #react почти не используется
        self.reaction_skill = ReactionSkill(self.telegram, self.bus)
        self.skills.register(self.reaction_skill.skill())
        self.skills.register(SelfEditSkill(self.root, sys.executable, bus=self.bus).skill())
        self.skills.register(ChannelSkill(self.telegram, self.data_dir,
                                          self.skills.get("draw"), self.bus).skill())
        self.skills.register(ProfileSkill(self.telegram, self.skills.get("draw"),
                                          self.bus).skill())
        self.skills.register(AntigravitySkill(
            self.root, cfg.assistant.agy_command,
            cfg.assistant.agy_timeout_minutes, self.bus).skill())

        # --- ассистент-режим: канал-доска задач + делегирование антигравити
        from .assistant import AssistantModule
        self.assistant = AssistantModule(self)
        # задачи, оставшиеся в 'running' от прошлого процесса, возвращаем в очередь
        self.assistant.reset_all_running()

        # --- ночная очередь: сообщения, пришедшие во время сна
        self.night_queue = NightQueue(self.data_dir / "night_queue.db")

        # --- история диалогов (в памяти, мягкий потолок по символам)
        self.histories: dict[int, deque] = defaultdict(lambda: deque(maxlen=40))
        # последний message_id по чату — чтобы журнал эмоций знал,
        # на какое именно сообщение пришлась реакция
        self._last_message_ids: dict[int, int] = {}

        # 27.10: досмотр «чем кончилось». Когда она участвовала в разговоре,
        # через 7 минут тишины подтягиваем 20 сообщений ВПЕРЁД — иначе она
        # помнит спор с середины и не знает, чем он закончился.
        # chat_id -> {"last_ts": float, "last_message_id": int, "armed": bool}
        self._chat_activity: dict[int, dict[str, Any]] = {}
        # 28.10: когда последний раз отвечали владельцу в конкретной группе.
        # Отдельно от _chat_activity: тот итерируется как dict в _idle_peek.
        self._owner_reply_ts: dict[int, float] = {}

        # 28.10: когда в последний раз сообщали об отвержении в этом чате.
        # Нужно, чтобы мут не приходил трижды (см. _on_social).
        self._social_seen: dict[tuple[int, str], float] = {}
        self.idle_peek_seconds = 7 * 60

        # --- мысли, вселённые извне (дашборд): она принимает их за свои
        self.injected_thoughts: list[dict[str, Any]] = []

        # --- постоянный журнал переписки (переживает рестарты)
        self._message_log = open(self.data_dir / "message_log.jsonl", "a", encoding="utf-8")
        self.bus.subscribe("telegram.*", self._log_message)

        # --- проктивность (A: импульсы, B: мысли+желания, C: потребности)
        from .proactivity import ProactivityEngine
        self.proactivity = ProactivityEngine(self)

        # --- планировщик и метрики
        self.scheduler = Scheduler(self.bus)
        self.metrics: Metrics | None = None
        self.dashboard = None
        self.proxy_server = None
        self.dashboard_server = None
        self._tasks: list[asyncio.Task] = []
        self._last_sleep_day = -1
        self._instance = None

    def _log_message(self, payload: dict) -> None:
        """telegram.message / telegram.sent -> jsonl, чтобы переписка не терялась."""
        topic = self.bus.history[-1].topic if self.bus.history else ""
        if topic not in ("telegram.message", "telegram.sent"):
            return
        import json as _json
        try:
            self._message_log.write(_json.dumps(
                {"ts": time.time(), "topic": topic, **payload}, ensure_ascii=False) + "\n")
            self._message_log.flush()
        except Exception:
            pass

    # ------------------------------------------------------------- запуск

    async def run(self) -> None:
        self.bus.bind_loop(asyncio.get_running_loop())
        cfg = self.config.current

        # один процесс на проект: иначе два экземпляра получают одни и те же
        # сообщения и отвечают дважды (видно в логах 2026-10-25)
        from .core.singleton import lock_for
        self._instance = lock_for(self.data_dir)
        if not self._instance.acquire():
            log.error("kuni уже запущена (см. %s) — второй экземпляр не стартует",
                      self._instance.path)
            raise SystemExit(3)

        if cfg.metrics.enabled:
            self.metrics = Metrics(self.bus, cfg.metrics.port)
            self.modules["metrics"] = "running"

        tg_ok = await self.telegram.start()
        if tg_ok:
            self.modules["telegram"] = "started"

        llm_ok = await self.llm.healthy()
        self.modules["llm"] = "ok" if llm_ok else "unreachable"
        if not llm_ok:
            log.warning("LLM %s недоступен — включаю echo-диагностику автоматически",
                        cfg.llm.base_url)

        await self._start_http_servers()

        # фоновые ритмы
        self.scheduler.add_interval("config.reload", 2.0, self._reload_config)
        # память: либо один ночной прогон, либо постоянный фон (переключается)
        if self.background.is_background():
            bcfg = cfg.background
            self.scheduler.add_interval(
                "memory.tick", max(60.0, bcfg.interval_minutes * 60),
                self.background.tick)
            self.scheduler.add_interval(
                "memory.distill", max(300.0, bcfg.interval_minutes * 60 * 2),
                self.background.distill_step)
            self.scheduler.add_daily(
                "memory.decay", bcfg.decay_hour, self.background.decay_step)
            self.scheduler.add_interval(
                "diary.save", max(600.0, bcfg.diary_minutes * 60), self._diary_save)
            log.info("память: фоновый режим (тик %.0f мин, дневник %.0f мин)",
                     bcfg.interval_minutes, bcfg.diary_minutes)
        else:
            if cfg.prompts.diary_save:
                self.scheduler.add_interval("diary.save", 3600.0, self._diary_save)
            log.info("память: ночной режим (сон в %d:00)", cfg.diary.consolidation_hour)

        # 27.10: сон был доступен ТОЛЬКО в ночном режиме памяти — в
        # background-ветке его не регистрировали вообще (так вышло в
        # f096a10). При mode="background" она не спала никогда, а
        # усталость копилась без возможности восстановиться.
        # Теперь консолидация памяти идёт по расписанию в любом режиме:
        # фоновая обработка и сон — разные вещи, не альтернативы.
        self.scheduler.add_daily("sleep", cfg.diary.consolidation_hour,
                                 self._do_sleep)
        self.scheduler.add_interval("offline_check", 60.0, self._offline_check)
        # раз в минуту проверяем, не пора ли досмотреть, чем кончился разговор
        self.scheduler.add_interval("idle_peek", 60.0, self._idle_peek)
        # 28.10: автомат сна опрашивается раз в 30 секунд. Он не блокирует
        # ничего: считает давление и гейт, возвращает фазу.
        self.scheduler.add_interval("sleep.tick", 30.0, self._sleep_tick)

        # автозапуск диалога: она пишет сама, сразу после старта
        #
        # 28.10: НЕ на каждом рестарте. В логе 20+ перезапусков дали 5
        # почти дословных опенеров («Ха. Бот, который отвечает с эмодзи…»
        # в 05:43, 05:45, 08:07, 11:05, 05:06). Со стороны это выглядит
        # как поломка: она знакомится заново каждый раз.
        # Отметка о последнем автодиалоге живёт в data/, переживает рестарт.
        acfg = cfg.autochat
        if acfg.enabled and acfg.chat_id:
            if self._autochat_recently_done(acfg.chat_id):
                log.info("автодиалог: уже был недавно — пропускаю")
            else:
                self._tasks.append(asyncio.ensure_future(self._autochat_start(acfg)))
                log.info("автодиалог включён: чат %s (%s), через %.0f с",
                         acfg.chat_id, acfg.title or "без названия", acfg.delay_seconds)
            if acfg.repeat_minutes > 0:
                self.scheduler.add_interval(
                    "autochat.repeat", acfg.repeat_minutes * 60,
                    lambda: self._autochat_start(acfg, repeat=True))
        if cfg.news.enabled and cfg.news.channels:
            self.scheduler.add_interval(
                "news", cfg.news.digest_interval_hours * 3600, self._news_digest)
        # проктивность: три независимые петли
        pcfg = cfg.proactivity
        if pcfg.enabled and cfg.telegram.enabled:
            if pcfg.impulse_enabled:
                self.scheduler.add_interval("proactivity.impulse",
                                            pcfg.impulse_interval_minutes * 60,
                                            self.proactivity.run_impulse)
            if pcfg.thought_stream_enabled:
                self.scheduler.add_interval("proactivity.thoughts",
                                            pcfg.thought_interval_minutes * 60,
                                            self.proactivity.run_thought)
            if pcfg.needs_enabled:
                self.scheduler.add_interval("proactivity.needs",
                                            pcfg.needs_check_minutes * 60,
                                            self.proactivity.run_needs)
            log.info("проктивность: импульсы=%s мысли=%s потребности=%s",
                     pcfg.impulse_enabled, pcfg.thought_stream_enabled, pcfg.needs_enabled)
        # ассистент: канал-доска задач + исполнитель
        if cfg.telegram.enabled and cfg.assistant.channel_id:
            self.scheduler.add_interval(
                "assistant.poll", cfg.assistant.check_interval_minutes * 60,
                self.assistant.poll_channel)
            self.scheduler.add_daily("assistant.run", cfg.assistant.run_hour,
                                     self.assistant.run_due)
            # «сейчас»-задачи подхватываем чаще
            self.scheduler.add_interval("assistant.due", 300, self.assistant.run_due)
            log.info("ассистент: канал %s, режим %s", cfg.assistant.channel_id,
                     cfg.assistant.mode)
        async def _heartbeat() -> None:
            self.bus.emit("app.heartbeat", uptime=time.time() - self._start_time)

        self.scheduler.add_interval("heartbeat", 30.0, _heartbeat)

        async def _affect_social_tick() -> None:
            """Затухание обиды и тонический дрейф руминации.

            Без этой петли руминация была бы просто записью в базе:
            заряд обязан реально тянуть серотонин вниз, иначе «пережёвывает»
            остаётся словом, а не состоянием.
            """
            if self.affect is None:
                return
            # Реальный прошедший интервал, а не фиксированные 300 с:
            # иначе после простоя обида не заживает вовсе.
            now = time.time()
            dt = max(1.0, now - getattr(self, "_last_social_tick", now))
            self._last_social_tick = now
            # Не гоняем больше часа за один тик: RK4 на сутках простоя
            # вешает event loop на секунды.
            dt = min(dt, 3600.0)
            try:
                self.affect.tick_social(dt, now=now)
                ds, dn = self.affect.step_rumination(dt, now=now)
                # ВАЖНО: сначала клиренс, потом дрейф. Без engine.step
                # серотонин упирался в ноль и оставался там навсегда:
                # дрейф капал, а смывать его было нечем.
                self.affect.tick(now)
                if ds or dn:
                    self.affect.engine.apply_burst(ds, dn, 0.0)
            except Exception:
                log.debug("тик социальной боли не прошёл", exc_info=True)

        self.scheduler.add_interval("affect.social", 300.0, _affect_social_tick)
        self._start_time = time.time()
        self.bus.emit("app.started", llm=llm_ok, telegram=tg_ok)
        log.info("persona-runtime запущена: llm=%s telegram=%s дневник=%d записей",
                 llm_ok, tg_ok, self.diary_store.count())

    async def _start_http_servers(self) -> None:
        cfg = self.config.current
        import uvicorn

        if cfg.dashboard.enabled:
            from .dashboard.server import DashboardState, create_dashboard_app
            state = DashboardState(self.bus, self.diary, self.working, self.emotions,
                                   self.diary_store, app_ref=self)
            static_dir = Path(__file__).parent / "dashboard" / "static"
            # 27.10: если слушаем не localhost, нужен токен — иначе любой
            # в сети сможет дёрнуть POST /api/config и переписать конфиг.
            token = self._dashboard_token(cfg.dashboard)
            self.dashboard = create_dashboard_app(state, static_dir, token=token)
            self.dashboard_server = uvicorn.Server(uvicorn.Config(
                self.dashboard, host=cfg.dashboard.host, port=cfg.dashboard.port, log_level="warning"))
            self._tasks.append(asyncio.ensure_future(self.dashboard_server.serve()))
            self.modules["dashboard"] = "running"
            log.info("дашборд: http://%s:%d", cfg.dashboard.host, cfg.dashboard.port)
            if token and cfg.dashboard.host not in ("127.0.0.1", "localhost", "::1"):
                # сам токен в лог не пишем: у сетевого доступа могут быть
                # глаза, а токен = пароль. Адреса есть, токен владелец знает.
                for ip in self._lan_ips():
                    log.info("дашборд (сеть): http://%s:%d/ (нужен токен)",
                             ip, cfg.dashboard.port)

        if cfg.proxy.enabled:
            from .proxy.server import create_proxy_app
            proxy_app = create_proxy_app(
                self.llm, self.persona, self.diary, self.working, self.skills,
                self.root / cfg.proxy.logs_dir)
            self.proxy_server = uvicorn.Server(uvicorn.Config(
                proxy_app, host=cfg.proxy.host, port=cfg.proxy.port, log_level="warning"))
            self._tasks.append(asyncio.ensure_future(self.proxy_server.serve()))
            self.modules["proxy"] = "running"
            log.info("OpenAI-прокси: http://%s:%d/v1", cfg.proxy.host, cfg.proxy.port)

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await self.scheduler.stop()
        self.telegram.stop()
        for client in (self.llm, self.vision_llm, self.embed_llm,
                       self.emotion_llm, self.sleep_llm):
            try:
                await client.aclose()
            except Exception:
                pass
        self.db.close()
        if self._instance is not None:
            self._instance.release()
        self.bus.emit("app.stopped")

    # --------------------------------------------------------- конвейер ответа

    async def handle_message(self, msg: IncomingMessage) -> None:
        cfg = self.config.current
        # запомнить, на какое сообщение придётся реакция: журнал эмоций
        # привязывает изменение состояния именно к нему
        if msg.message_id:
            self._last_message_ids[msg.chat_id] = msg.message_id
        owner = cfg.general.papik_chat_id
        if owner and msg.chat_id == owner:
            if await self._owner_command(msg):
                return

        # входящее закрывает ожидание ответа. Если оно пришло после
        # таймаута — это «напрасное ожидание», и оно бьёт по социальной
        # боли: она ждала, а её оставили.
        if self.affect is not None:
            try:
                was, burned = self.affect.commitments.on_received(msg.chat_id)
                if burned:
                    log.info("ожидание в чате %s истекло напрасно",
                             msg.chat_id)
                    self._burned_wait(msg.chat_id)
            except Exception:
                log.debug("трекинг обязательств не отработал", exc_info=True)

        # 28.10: в заблокированный чат не пишем вовсе. TDLib отвечал
        # YOU_BLOCKED_USER и копил «yet unsent message», ретрая их вечно.
        if self.telegram.is_blocked(msg.chat_id):
            log.info("чат %s: заблокирован — молчу", msg.chat_id)
            return

        echo = cfg.telegram.echo_mode or self.modules.get("llm") != "ok"
        if echo:
            await self._echo_reply(msg)
            return

        # 28.10: бэклог после рестарта — не разговор. Молчим, но помним.
        if await self._is_stale(msg):
            log.info("чат %s: сообщение %.0f мин давности — бэклог, молчу",
                     msg.chat_id, (time.time() - msg.message_ts) / 60)
            return

        # 28.10: «спит» теперь решает автомат сна, а не статические часы.
        # Он знает фазу, качество и то, участвовал ли чат в разговоре.
        if self._should_queue_message(msg):
            self.night_queue.push(QueuedMessage(
                chat_id=msg.chat_id, message_id=msg.message_id,
                sender_user_id=msg.sender_user_id, text=msg.text,
                ts=msg.ts, chat_title=msg.chat_title, chat_type=msg.chat_type,
                is_voice=msg.is_voice, is_photo=msg.is_photo,
                is_sticker=msg.is_sticker,
            ))
            log.info("ночь — сообщение из чата %s в очереди (%d ждёт)",
                     msg.chat_id, self.night_queue.pending_count())
            return

        # 28.10: если её замьютили — не пытаемся отвечать вовсе. Раньше
        # каждая попытка падала с ошибкой прав, порождала новое событие
        # "muted" и она писала в чат очередное «не могу сюда писать».
        # Семь таких сообщений подряд — ровно этот сценарий.
        #
        # ВАЖНО (нашёл критик): первая версия дала ДЕДЛОК. Гард не пускал
        # к отправке, а флаг снимался ТОЛЬКО успешной отправкой. Мут
        # становился вечным, чат умирал навсегда. Теперь у мута есть срок:
        # через MUTE_TTL часов делаем одну пробную попытку. Если права
        # вернули — отправка пройдёт и repair_social снимет флаг. Если нет —
        # отправка упадёт, флаг обновится, ждём ещё TTL.
        if self.affect is not None:
            try:
                _st = self.affect.social_state(msg.chat_id)
                if _st.is_muted:
                    _age = time.time() - float(getattr(_st, "updated_ts", 0) or 0)
                    if _age < self.MUTE_TTL_SEC:
                        log.info("чат %s: замьючена (%.0f ч) — молчу",
                                 msg.chat_id, _age / 3600)
                        return
                    log.info("чат %s: мут старше %.0f ч — пробую написать",
                             msg.chat_id, _age / 3600)
            except Exception:
                log.debug("проверка мута упала", exc_info=True)

        # в группе не отвечаем на каждое сообщение: только когда обращаются
        if not self._should_reply_in_chat(msg):
            log.info("группа %s: не адресовано — молчу (%s)",
                     msg.chat_id, msg.text[:50])
            return

        # 27.10: она вступает в обсуждение — надо видеть, о чём был спор.
        # Раньше история писалась только для адресованных сообщений, и на
        # десятом сообщении спора она не знала первых девяти.
        if (msg.chat_type or "") not in ("private", "secret", ""):
            await self._peek_before(msg)

        await self._full_reply(msg)

    @staticmethod
    def _apply_slot_quota(associations: list[dict], interlocutor_id: int,
                          k: int, min_same: int = 2) -> list[dict]:
        """Гарантировать слоты воспоминаниям о текущем собеседнике.

        Порядок не меняем — только подтягиваем свои наверх, если их
        меньше min_same. Если своих в памяти мало, отдаём что есть:
        добивать нечем, а пустыми слотами хуже.
        """
        if not associations or not interlocutor_id:
            return associations
        same = [a for a in associations
                if int(a.get("speaker_id") or 0) == interlocutor_id]
        if not same:
            return associations          # про него вообще ничего нет
        other = [a for a in associations
                 if int(a.get("speaker_id") or 0) != interlocutor_id]

        # Гарантируем, что после обрезки до k свои не исчезнут. Прежний
        # вариант возвращал список как есть, если своих уже >= min_same —
        # но при k=3 и порядке «3 чужих, 2 своих» обрезка съедала обоих.
        keep = min(min_same, len(same), k)
        head = same[:keep]
        rest = same[keep:] + other
        return (head + rest)[:k]

    def _arm_idle_peek(self, chat_id: int, message_id: int = 0) -> None:
        """Запомнить, что в этом чате она участвовала — для досмотра."""
        self._chat_activity[chat_id] = {
            "last_ts": time.time(),
            "last_message_id": message_id or self._last_message_ids.get(chat_id, 0),
            "armed": True,
        }

    async def _idle_peek(self) -> None:
        """Через 7 минут тишины — досмотреть, чем кончился разговор.

        27.10: без этого она видела спор только до момента, когда её
        позвали. Финал («помирились» / «разошлись злые») проходил мимо.
        """
        now = time.time()
        for chat_id, st in list(self._chat_activity.items()):
            if not st.get("armed"):
                continue
            if now - float(st.get("last_ts") or 0) < self.idle_peek_seconds:
                continue
            st["armed"] = False          # один раз на разговор
            from_message_id = int(st.get("last_message_id") or 0)
            try:
                raw = await self.telegram.get_chat_history(
                    chat_id, limit=20,
                    from_message_id=from_message_id, offset=1)
            except Exception:
                log.debug("idle peek не удался (чат %s)", chat_id, exc_info=True)
                continue
            if not raw:
                continue

            added = 0
            lines: list[str] = []
            for m in reversed(raw):      # TDLib отдаёт новые первыми
                text = self._message_text(m)
                if not text:
                    continue
                sender_id = ((m.get("sender_id") or {}).get("user_id") or 0)
                is_me = sender_id == self.telegram.me_user_id
                if is_me:
                    name = self.config.current.general.character_name
                else:
                    name = await self.telegram.get_user_name(sender_id) or "кто-то"
                lines.append(f"[{name}]: {text}")
                added += 1

            if not lines:
                continue
            # в память, а не в диалог: это случилось без неё
            try:
                await self.diary.add(
                    "Чем кончился разговор (я уже не участвовала):\n"
                    + "\n".join(lines[-20:]),
                    source="idle_peek", chat_id=chat_id,
                    importance=0.35, provenance="idle_peek",
                )
            except Exception:
                log.debug("idle peek запись не удалась", exc_info=True)
            log.info("idle peek: досмотрел %d сообщений в чате %s",
                     added, chat_id)

    async def _peek_before(self, msg: IncomingMessage, depth: int = 20) -> None:
        """Подтянуть до 20 сообщений ДО того, на которое отвечаем.

        Зачем: в группе история писалась только когда к ней обращались,
        поэтому любой спор она видела с середины. Теперь, вступая в
        разговор, она сначала читает контекст.

        Только в группах: в личке вся переписка и так в истории.
        """
        try:
            raw = await self.telegram.get_chat_history(
                msg.chat_id, limit=depth,
                from_message_id=msg.message_id, offset=-depth)
        except Exception:
            log.debug("peek до не удался (чат %s)", msg.chat_id, exc_info=True)
            return
        if not raw:
            return

        history = self.histories[msg.chat_id]
        known = {item.get("content") for item in history}
        added = 0
        # TDLib отдаёт от новых к старым — разворачиваем в хронологию
        for m in reversed(raw):
            text = self._message_text(m)
            if not text:
                continue
            sender_id = ((m.get("sender_id") or {}).get("user_id") or 0)
            if sender_id == self.telegram.me_user_id:
                name = self.config.current.general.character_name
            else:
                name = await self.telegram.get_user_name(sender_id) or "кто-то"
            line = f"[{name}]: {text}"
            if line in known:
                continue
            history.append({"role": "user", "content": line,
                            "speaker": name, "peek": True})
            known.add(line)
            added += 1
            if added >= depth:
                break
        if added:
            log.info("peek до: подтянул %d сообщений в чате %s",
                     added, msg.chat_id)

    @staticmethod
    def _message_text(m: dict) -> str:
        """Текст из TDLib-сообщения; пустая строка для не-текста."""
        content = m.get("content") or {}
        ctype = content.get("@type", "")
        if ctype == "messageText":
            return (content.get("text") or {}).get("text", "").strip()
        if ctype == "messagePhoto":
            cap = (content.get("caption") or {}).get("text", "").strip()
            return f"[фото] {cap}".strip()
        if ctype == "messageVoiceNote":
            return "[голосовое]"
        if ctype == "messageSticker":
            return "[стикер]"
        # служебные и неподдерживаемые — без содержания, в контекст не идут
        if ctype in ("messageUnsupported", "messageBasicGroupChatCreate",
                     "messageChatChangeTitle", "messageChatChangePhoto",
                     "messageChatDeletePhoto", "messageChatJoinByLink",
                     "messageChatAddMembers", "messageChatDeleteMember",
                     "messagePinMessage", "messageChatSetTheme"):
            return ""
        if ctype.startswith("message"):
            return f"[{ctype.replace('message', '').lower()}]"
        return ""

    def _should_reply_in_chat(self, msg: IncomingMessage) -> bool:
        """Стоит ли вообще отвечать в этом чате.

        Личка — всегда да. Группа/супергруппа — только если:
          * её позвали по имени/юзернейму;
          * это ответ на её сообщение;
          * вопрос с вопросительным знаком;
          * владелец написал прямо.
        Канал — никогда: там нельзя отвечать.
        """
        chat_type = (msg.chat_type or "").lower()
        if chat_type in ("private", "secret", ""):
            return True
        if chat_type in ("channel", "supergroup") and msg.is_channel:
            return False

        cfg = self.config.current
        owner = cfg.general.papik_chat_id
        if owner and msg.sender_user_id == owner:
            # 28.10: владелец в группе — да, но НЕ на каждую строку.
            # Раньше безусловный return True давал 8 ответов за 11 секунд
            # и 18 подряд на бэклоге. Отвечаем не чаще, чем раз в
            # OWNER_GROUP_GAP секунд — остальное читаем молча.
            now = time.time()
            last = self._owner_reply_ts.get(msg.chat_id, 0.0)
            if now - last < self.OWNER_GROUP_GAP_SEC:
                log.info("группа %s: владелец пишет часто — читаю молча",
                         msg.chat_id)
                return False
            self._owner_reply_ts[msg.chat_id] = now
            return True

        text = (msg.text or "").lower()
        name = (cfg.general.character_name or "").lower()
        nickname = (cfg.general.character_nickname or "").lower()
        for handle in (name, nickname):
            if handle and handle in text:
                return True
        # ответ на её сообщение
        if msg.is_reply and msg.reply_to_self:
            return True
        # прямой вопрос
        if "?" in text or "？" in text:
            return bool(msg.is_reply and msg.reply_to_self) or bool(name and name in text)
        return False

    def _human_pause(self) -> bool:
        """Имитация человека (задержки, набор текста) — только вне ассистент-режима.

        В assistant-режиме она работает, а не играет: ответы уходят сразу.
        """
        return self.assistant.mode() != "assistant"

    async def _full_reply(self, msg: IncomingMessage) -> None:
        assert self.telegram.human is not None
        human = self.telegram.human
        cfg = self.config.current
        chat_label = msg.chat_title or str(msg.chat_id)

        await human.mark_read(msg.chat_id, [msg.message_id])
        await human.go_online()

        # голос -> текст
        user_text = msg.text
        if msg.is_voice and self.voice.can_transcribe:
            path = await self.telegram.download_file(msg.voice_file_id, msg.chat_id, msg.message_id)
            if path:
                try:
                    user_text = (await self.voice.transcribe(path) or user_text).strip()
                except Exception:
                    log.exception("whisper не справился")

        # фото -> описание (vision_model), картинку можно обсудить в разговоре
        if msg.is_photo:
            caption = user_text
            description = await self._describe_photo(msg)
            if description:
                user_text = f"[отправил фото"
                if caption:
                    user_text += f" с подписью: {caption}"
                user_text += f"]\nНа фото: {description}"
                log.info("фото распознано: %s", description[:100])
            else:
                user_text = "[отправил фото, но я его не разглядела]" + (f" Подпись: {caption}" if caption else "")

        # стикер/гифка -> смысл для разговора (sticker_to_text)
        if msg.is_sticker:
            meaning = await self._describe_sticker(msg, user_text)
            if meaning:
                user_text = f"[стикер] {meaning}"

        history = self.histories[msg.chat_id]
        history.append({"role": "user", "content": user_text or "[медиа]"})

        # --- досознательная оценка: состояние меняется ДО промпта
        if self.affect is not None:
            try:
                chat_type = msg.chat_type or "private"
                witnesses = 0 if chat_type == "private" else 3
                self.affect.receive_message(
                    text=user_text, chat_id=msg.chat_id,
                    user_id=msg.sender_user_id,
                    is_reply_to_self=msg.reply_to_self,
                    is_mention=msg.is_mention,
                    chat_type=chat_type,
                    witnesses=witnesses)
                self._after_appraisal(msg, user_text, witnesses)
            except Exception:
                log.exception("аффективный импульс не прошёл")

        # воспоминания всплывают сами: слова + настроение + ассоциативный граф
        associations = []
        try:
            if self.affect is not None:
                # ecphory: mood-congruent поиск с текущим состоянием
                query_vec = None
                if self.embed_llm is not None:
                    try:
                        query_vec = await self.embed_llm.embed_one(user_text or chat_label)
                    except Exception:
                        query_vec = None
                hits = self.affect.retrieve(
                    query=user_text or chat_label,
                    query_embedding=query_vec,
                    store=self.diary_store,
                    top_k=cfg.memory.flash_k,
                )
                associations = [h.to_dict() for h in hits]
            else:
                associations = await self.diary.flash(
                    user_text or chat_label, self.emotions.state,
                    k=cfg.memory.flash_k,
                    fresh_window=cfg.memory.flash_fresh_window_sec,
                )
            # 27.10: квота слотов. Без неё 85 эпизодов чужого бота
            # вытесняли 15 эпизодов владельца — про него просто не
            # всплывало ничего, и она путала, с кем говорит.
            associations = self._apply_slot_quota(
                associations, msg.sender_user_id, cfg.memory.flash_k)
        except Exception:
            log.exception("вспоминания не всплыли")

        # что с ней происходит в этом чате: мут, стадия обиды, боль
        social_payload = self._social_payload(msg.chat_id, chat_label)
        system = self.persona.build(
            chat_title=chat_label, chat_type=msg.chat_type,
            associations=associations, working_memory=self.working.content(),
            thoughts=self.consume_thoughts(),
            social=social_payload,
            interlocutor_id=msg.sender_user_id,
            interlocutor_name=msg.sender_name,
            mental=self.affect.mental.snapshot if self.affect else None,
            sleep_phase=(self.affect.sleep.snapshot.phase
                         if self.affect else "awake"),
        )
        messages = [system_msg(system)] + list(history)

        # скилл реакций должен знать, на какое сообщение ставить реакцию:
        # модель message_id не видит, а инструменту он нужен
        if getattr(self, "reaction_skill", None) is not None:
            self.reaction_skill.remember_message(msg.chat_id, msg.message_id)

        try:
            reply_text, artifacts = await self._chat_with_tools(messages, chat_label)
        except LLMError as e:
            log.exception("LLM не ответил")
            # 28.10: в группу беспомощную реплику не отправляем. Раньше
            # «…что-то я зависла, повтори?» уходило в любой чат — тот же
            # класс бага, что echo-диагностика, за которую её замутили.
            # В личке это ещё уместно, в группе — выглядит поломкой.
            if (msg.chat_type or "") in ("private", "secret", ""):
                await self.telegram.send_text(
                    msg.chat_id, "…что-то я зависла, повтори?")
            else:
                log.info("LLM упал в группе %s — молчу, не шлю заглушку",
                         msg.chat_id)
            return

        # Пустой ответ — это НЕ повод отправить "…" в чат.
        # Раньше сюда подставлялась заглушка, и человек получал многоточие
        # вместо ответа (видно в логе 04:24:50). Правильнее:
        #   1) один повтор с явным требованием ответить;
        #   2) если снова пусто — промолчать, это валидный ход.
        if not reply_text.strip():
            log.warning("пустой ответ модели — пробую ещё раз")
            try:
                retry_msgs = list(messages) + [{
                    "role": "user",
                    "content": "(ответь одной короткой фразой, без объяснений)",
                }]
                reply_text, retry_artifacts = await self._chat_with_tools(
                    retry_msgs, chat_label)
                artifacts = artifacts + retry_artifacts
            except Exception:
                log.warning("повтор тоже не дал ответа")
            if not reply_text.strip():
                log.info("модель молчит — пропускаю отправку (тишина уместна)")
                # откатываем последнюю user-реплику: она уже уйдёт из истории
                # при следующем сообщении, а висящая без ответа ломает контекст
                if history and history[-1].get("role") == "user":
                    history.pop()
                return

        # усталость от собственной работы
        if self.affect is not None:
            try:
                self.affect.register_reply(len(reply_text) // 4)
            except Exception:
                pass

        # 28.10: #wait — реализация обещания из prompts/anti_repeat.md.
        # Промпт велел «вызови #wait, лучше промолчать», но команды в коде
        # не существовало: она отвечала ВСЕГДА (0.99 ответа на сообщение).
        # Молчание — валидный ход в переписке, особенно в группе.
        if self._WAIT_RE.search(reply_text or ""):
            log.info("она решила промолчать (#wait) в чате %s", msg.chat_id)
            if self.affect is not None:
                try:
                    self.affect.register_silence(msg.chat_id)
                except Exception:
                    log.debug("тишина не учтена в аффекте", exc_info=True)
            return

        # #react-команды вынимаются и превращаются в реакции (у всех моделей)
        reply_text = await self._absorb_reactions(reply_text, msg)
        # #remember-команды вынимаются из текста ВСЕГДА, а не только
        # в fallback без tool-calling: иначе они утекают в чат (видно в логах)
        reply_text = await self._absorb_text_memory(reply_text, chat_label)

        # Если после вырезания служебных команд не осталось текста,
        # отправлять нечего: реакция уже стоит, заметка уже записана.
        # Раньше сюда подставлялось «…», и человек получал пустое многоточие.
        if not reply_text.strip():
            log.info("после служебных команд текста не осталось — молчу")
            return

        history.append({"role": "assistant", "content": reply_text})
        self._trim_history(history, cfg.llm.context_soft_cap)

        # 28.10: ПОВЕДЕНИЕ, а не личность. Замеры по её же логу:
        #   она  — средняя длина 94 симв, 39% ответов с пустой строкой,
        #          100% ответов цитатой, «...» в начале в 24% случаев
        #   люди — средняя длина 39 симв, 3% с пустой строкой,
        #          цитата в личке почти никогда
        # Поэтому здесь: убрать тик с многоточием, разбить полотно на
        # 1-3 живые реплики, ставить цитату только когда человек бы её
        # поставил, и «набирать» с человеческой скоростью.
        reply_text = _splitter.strip_leading_dots(reply_text)
        parts = _splitter.split_reply(reply_text) or [reply_text]

        cps = getattr(cfg.telegram, "typing_speed_cps", 3.0)
        quote = _splitter.should_quote(msg.chat_type or "")

        for idx, part in enumerate(parts):
            if idx and self._human_pause():
                await asyncio.sleep(_splitter.pause_between_parts())
            if self._human_pause():
                await human.before_reply(msg.chat_id, len(part))
                wait = _splitter.typing_seconds(part, cps=cps)
                await human.set_typing(msg.chat_id, "chatActionTyping",
                                       active=True)
                await asyncio.sleep(min(wait, 20.0))
                await human.set_typing(msg.chat_id, "chatActionCancel",
                                       active=False)
            try:
                await self.telegram.send_text(
                    msg.chat_id, part,
                    reply_to=msg.message_id if quote else 0)
            except Exception:
                log.warning("часть %d/%d не ушла", idx + 1, len(parts),
                            exc_info=True)
                break
            self._last_sent_ts = time.time()

        # 28.10: отправилось -> права есть. Снимаем залипший флаг мута,
        # иначе она продолжает считать себя замьюченной и жалуется на это
        # в каждом ответе, хотя пишет уже свободно.
        if self.affect is not None:
            try:
                self.affect.repair_social(msg.chat_id, "unmuted")
            except Exception:
                log.debug("снятие мута не отработало", exc_info=True)

        # 28.10: запомнить, что она ответила первой и/или задала вопрос.
        # Без этого трекер обязательств пуст, и она не знает, что ждёт
        # ответа — а значит не может задержаться ради него.
        if self.affect is not None:
            try:
                self.affect.commitments.on_sent(
                    msg.chat_id, text=reply_text, proactive=False,
                    w_rel=self.affect.relation_weight(msg.chat_id),
                    affect_high=self.emotions.state.intensity >= 0.55)
            except Exception:
                log.debug("обязательство не записалось", exc_info=True)
        for artifact in artifacts:
            path = Path(artifact)
            if path.suffix == ".png" and path.exists():
                await self.telegram.send_photo_file(msg.chat_id, path)
            elif path.exists():
                await self.telegram.send_voice_file(
                    msg.chat_id, path, self.voice.wav_duration(path))

        # голосом — когда собеседник сам прислал войс (естественный обмен)
        if msg.is_voice and self.voice.can_speak and reply_text.strip():
            await self._send_voice_reply(msg.chat_id, reply_text)

        # она ответила — значит участвует в этом разговоре. Через 7 минут
        # тишины досмотрим, чем он кончился (см. _idle_peek).
        self._arm_idle_peek(msg.chat_id, msg.message_id)

        # эмоциональная оценка обмена + возможная запись в дневник
        asyncio.ensure_future(self._assess_emotion(msg.chat_id, user_text, reply_text))
        await human.maybe_go_offline()

    async def _chat_resilient(self, messages: list[dict], **kwargs: Any):
        """Основная модель + цепочка фолбэков при rate-limit/ошибке (429 и т.п.).

        Параметры сэмплирования берутся из аффекта: температура,
        max_tokens и top_p зависят от S/N/D и усталости.
        """
        cfg = self.config.current
        if "temperature" not in kwargs:
            kwargs["temperature"] = cfg.llm.temperature
        if self.affect is not None:
            try:
                # 28.10: состояние ресурса и фаза сна влияют на генерацию
                params = self.affect.sampling_params(
                    mental=self.affect.mental.snapshot,
                    sleep_phase=self.affect.sleep.snapshot.phase)
                kwargs.setdefault("temperature", params["temperature"])
                if "max_tokens" not in kwargs:
                    kwargs["max_tokens"] = params["max_tokens"]
                kwargs.setdefault("top_p", params["top_p"])
            except Exception:
                log.debug("параметры аффекта недоступны", exc_info=True)
        models: list[str | None] = [cfg.llm.model] + list(cfg.llm.fallback_models)
        last_err: Exception | None = None
        for model in models:
            try:
                result = await self.llm.chat(messages, model=model, **kwargs)
                # упёрлась в лимит и вернула пустоту: модель потратила все
                # токены на размышления. Один повтор с запасом дешевле,
                # чем молчание в чате.
                if (not (result.content or "").strip()
                        and result.finish_reason == "length"):
                    # 27.10: страховка на случай, если провайдер проигнорировал
                    # reasoning_effort (тогда размышления съедят лимит и
                    # content придёт пустым). Один повтор с запасом.
                    retry_kwargs = dict(kwargs)
                    retry_kwargs["max_tokens"] = 2000
                    log.info("модель %s упёрлась в лимит — повтор с max_tokens=2000",
                             model)
                    result = await self.llm.chat(messages, model=model,
                                                 **retry_kwargs)
                return result
            except LLMError as e:
                last_err = e
                log.warning("модель %s не ответила (%s), пробую следующую", model,
                            str(e)[:120])
        raise last_err if last_err else LLMError("нет моделей для запроса")

    async def _chat_resilient_json(self, messages: list[dict], **kwargs: Any) -> dict[str, Any]:
        """_chat_resilient + разбор JSON из ответа."""
        result = await self._chat_resilient(messages, **kwargs)
        return extract_json(result.content)

    # ------------------------------------------------------------ дашборд

    def _dashboard_token(self, dcfg: Any) -> str:
        """Токен для сетевого доступа к дашборду.

        Пустой host-token при нелокальном хосте = генерируем один раз и
        кладём в data/dashboard.token, чтобы он не менялся между
        перезапусками (иначе сохранённая ссылка перестанет работать).
        """
        explicit = (getattr(dcfg, "token", "") or "").strip()
        if explicit:
            return explicit
        if (getattr(dcfg, "host", "") or "") in ("127.0.0.1", "localhost", "::1", ""):
            return ""        # только локально — токен не нужен

        import secrets
        path = self.data_dir / "dashboard.token"
        try:
            if path.exists():
                saved = path.read_text(encoding="utf-8").strip()
                if saved:
                    return saved
            tok = secrets.token_urlsafe(18)
            path.write_text(tok + "\n", encoding="utf-8")
            try:
                path.chmod(0o600)
            except OSError:
                pass
            log.info("дашборд: создан токен в %s", path)
            return tok
        except OSError:
            log.warning("дашборд: не смог прочитать/создать %s", path, exc_info=True)
            return ""

    @staticmethod
    def _lan_ips() -> list[str]:
        """Адреса этой машины в локальной сети (для лога)."""
        import socket
        out: list[str] = []
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None,
                                           socket.AF_INET):
                ip = info[4][0]
                if ip not in out and not ip.startswith("127."):
                    out.append(ip)
        except OSError:
            pass
        if not out:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.connect(("8.8.8.8", 80))
                out.append(s.getsockname()[0])
                s.close()
            except OSError:
                pass
        return out

    def _affect_config(self, cfg: Any) -> AffectConfig:
        """Собрать настройки аффекта из секции [affect] конфига."""
        a = getattr(cfg, "affect", None)
        if a is None or not getattr(a, "enabled", True):
            affect_cfg = AffectConfig()
            affect_cfg.engine.noise_enabled = False
            return affect_cfg
        engine_cfg = EngineConfig(
            baseline_s=a.baseline_s, baseline_n=a.baseline_n,
            baseline_d=a.baseline_d,
            noise_enabled=a.noise_enabled,
        )
        mod_cfg = ModulationConfig(
            temperature_base=a.temperature_base,
            max_tokens_base=a.max_tokens_base,
            steering_enabled=a.steering_enabled,
        )
        breaker_cfg = BreakerConfig(
            enabled=a.breaker_enabled,
            cooldown_sec=a.breaker_cooldown_sec,
        )
        ecphory_cfg = EcphoryConfig(
            k_base=a.ecphory_k_base,
            mood_lambda=a.mood_lambda,
            noise_enabled=a.noise_enabled,
        )
        return AffectConfig(
            enabled=True, engine=engine_cfg, modulation=mod_cfg,
            breaker=breaker_cfg, ecphory=ecphory_cfg,
            pad_bind_weight=a.pad_bind_weight,
            w_rel_owner=a.w_rel_owner,
        )

    def _build_distill(self):
        """Функция дистилляции кластера для ночного сна (или None без LLM)."""
        cfg = self.config.current
        if not cfg.memory.use_llm_distill:
            return None

        async def distill(cluster: list) -> dict:
            # основной промпт — sleep_consolidator (полная политика сна),
            # резервный — diary_consolidation (короткий JSON без политики).
            instruction = self.prompts.get("sleep_consolidator") or ""
            if not instruction:
                instruction = self.prompts.get(
                    "diary_consolidation",
                    name=self.config.current.general.character_name,
                ) or ""
            if not instruction:
                from .memory.sleep import DEFAULT_CONSOLIDATION_PROMPT
                instruction = DEFAULT_CONSOLIDATION_PROMPT
            listing = "\n".join(
                f"[{i + 1}] ({clock.strftime('%m-%d %H:%M', e.ts)}) {e.text}"
                for i, e in enumerate(cluster)
            )
            return await self._chat_resilient_json(
                [{"role": "system", "content": instruction},
                 {"role": "user", "content": f"Записи дневника:\n{listing}"}],
                function_label="sleep",
            )

        return distill

    def _endpoint_client(self, section: Any, fallback_model: str) -> LLMClient:
        """Клиент для отдельной функции; пустые поля наследуют [llm].

        ВАЖНО: четвёртый позиционный аргумент LLMClient — это embed_model,
        а не «что-нибудь». Раньше сюда передавалась пустая строка, из-за
        чего эмбеддинги уходили с `model: ""` и OpenRouter отвечал
        400 «Model does not exist». Модель надо передавать в оба поля.
        """
        cfg = self.config.current.llm
        base = section.base_url or cfg.base_url
        key = section.api_key or cfg.api_key
        model = section.model or fallback_model
        return LLMClient(base, key, model, model, self.bus)

    async def _autochat_start(self, acfg: Any, repeat: bool = False) -> None:
        """Она открывает диалог сама, сразу после старта.

        Зачем: нужно проверить, как она общается с другим ботом, без
        ручного «напиши ему». Отличие от proactivity: это не импульс
        по настроению, а явная задача автозапуска.

        Ждём delay_seconds, чтобы поднялись Telegram и LLM: иначе первое
        сообщение уйдёт в пустоту или в echo-режим.
        """
        try:
            if not repeat:
                await asyncio.sleep(max(0.0, acfg.delay_seconds))
            if self.modules.get("llm") != "ok":
                log.warning("автодиалог: LLM не готов — пропускаю")
                return
            if not getattr(self.telegram, "authorized", False):
                log.warning("автодиалог: телеграм не авторизован — пропускаю")
                return

            chat_id = int(acfg.chat_id)
            title = acfg.title or str(chat_id)
            # подтянуть инфо о чате, если его нет в кэше
            if chat_id not in self.telegram.chats:
                try:
                    await self.telegram._cache_chat(chat_id)
                except Exception:
                    log.debug("автодиалог: не смогла закэшировать чат %s", chat_id)

            brief = acfg.brief or (
                "Тебе скучно, и ты хочешь написать этому собеседнику первой. "
                "Это бот по имени УзбекГПТ — он отвечает по-своему, иногда грубо. "
                "Начни разговор по своему характеру: коротко, с подколом, без "
                "представления и без «привет, я бот»."
            )
            result = await self.proactive_send(chat_id, brief, source="autochat")
            if result.get("ok"):
                self._mark_autochat_done(chat_id)
                log.info("автодиалог: написала в %s — %s", title,
                         str(result.get("sent"))[:80])
                if self.bus:
                    self.bus.emit("autochat.sent", chat_id=chat_id,
                                  title=title, text=str(result.get("sent"))[:200])
            else:
                log.warning("автодиалог не удался: %s", result.get("error"))
        except Exception:
            log.exception("автодиалог упал")

    #: 28.10: не чаще одного автодиалога в этот интервал. Рестарт — не повод
    #: здороваться заново.
    AUTOCHAT_GAP_SEC = 12 * 3600.0

    def _autochat_recently_done(self, chat_id: int) -> bool:
        """Был ли автодиалог в этот чат недавно (переживает рестарт)."""
        stamp_file = self.data_dir / "autochat_last.json"
        try:
            if not stamp_file.exists():
                return False
            import json as _json
            data = _json.loads(stamp_file.read_text(encoding="utf-8"))
            last = float(data.get(str(chat_id)) or 0)
            return (time.time() - last) < self.AUTOCHAT_GAP_SEC
        except Exception:
            log.debug("отметка автодиалога не прочиталась", exc_info=True)
            return False

    def _mark_autochat_done(self, chat_id: int) -> None:
        stamp_file = self.data_dir / "autochat_last.json"
        try:
            import json as _json
            data = {}
            if stamp_file.exists():
                data = _json.loads(stamp_file.read_text(encoding="utf-8"))
            data[str(chat_id)] = time.time()
            stamp_file.write_text(_json.dumps(data, ensure_ascii=False),
                                  encoding="utf-8")
        except Exception:
            log.debug("отметка автодиалога не записалась", exc_info=True)

    async def send_humanlike(self, chat_id: int, text: str, *,
                             reply_to: int = 0, quote: bool | None = None,
                             chat_type: str = "") -> int:
        """Отправить текст так, как это делает человек: 1-3 реплики.

        28.10: раньше дробление было ТОЛЬКО в _full_reply (ответ на
        входящее). Все остальные пути — proactive_send (она пишет первой),
        утренний разбор очереди, dashboard — шли целым куском, и полотно
        в три абзаца уезжало одним сообщением. На скриншотах владельца
        ровно это: 15:33 она написала САМА, и пришло 3 абзаца подряд.

        Возвращает id последнего отправленного сообщения.
        """
        text = _splitter.strip_leading_dots(text or "")
        if not text.strip():
            return 0
        parts = _splitter.split_reply(text) or [text]

        cfg = self.config.current
        cps = getattr(cfg.telegram, "typing_speed_cps", 3.0)
        if quote is None:
            quote = _splitter.should_quote(chat_type or "")
        human = self.telegram.human
        pause = self._human_pause() and human is not None

        last_id = 0
        for idx, part in enumerate(parts):
            if idx and pause:
                await asyncio.sleep(_splitter.pause_between_parts())
            if pause:
                await human.before_reply(chat_id, len(part))
                wait = _splitter.typing_seconds(part, cps=cps)
                await human.set_typing(chat_id, "chatActionTyping", active=True)
                await asyncio.sleep(min(wait, 20.0))
                await human.set_typing(chat_id, "chatActionCancel", active=False)
            try:
                last_id = await self.telegram.send_text(
                    chat_id, part,
                    reply_to=reply_to if (quote and idx == 0) else 0)
            except Exception:
                log.warning("часть %d/%d не ушла (чат %s)",
                            idx + 1, len(parts), chat_id, exc_info=True)
                break
            self._last_sent_ts = time.time()
        return last_id

    async def proactive_send(self, chat_id: int, brief: str, source: str = "manual") -> dict:
        """Она пишет человеку первой (без входящего). brief — её желание, не текст."""
        info = self.telegram.chats.get(chat_id, {})
        title = info.get("title") or str(chat_id)
        try:
            associations = await self.diary.flash(
                brief or f"человек {title}", self.emotions.state, k=3)
        except Exception:
            associations = []
        system = self.persona.build(
            chat_title=title, chat_type=info.get("type", ""),
            associations=associations, working_memory=self.working.content(),
            thoughts=self.consume_thoughts(),
            extra="You want to write to this person FIRST, on your own. Nobody asked "
                  "you — it's your own impulse, and it feels like yours.",
            interlocutor_name=(self.telegram.chats.get(chat_id) or {}).get("title", ""),
        )
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": brief or "Напиши ему что-нибудь от себя, прямо сейчас."},
        ]
        try:
            reply, _ = await self._chat_with_tools(messages, chat_label=title)
        except Exception as e:
            # пустой str(e) у части исключений (TimeoutError и т.п.) — без типа
            # в логе оставалось «модель не ответила: » без единой зацепки
            log.warning("proactive_send: модель не ответила (%s): %s",
                        type(e).__name__, str(e)[:300] or "<пустое сообщение>")
            return {"ok": False,
                    "error": f"модель не ответила ({type(e).__name__}): {e}"}
        reply = (reply or "").strip()
        # 28.10: вычистить служебные команды ПЕРЕД отправкой. Раньше
        # proactive_send слал reply как есть, и в чат ушло
        # «…\n\n#remember Написала первой боту УзбекГ…». Обычный путь
        # (_full_reply) чистит через _absorb_*, этот — нет.
        reply = self._REMEMBER_RE.sub("", reply).strip()
        reply = self._REACT_RE.sub("", reply).strip()
        if not reply:
            # пустой ответ — это не повод отправлять многоточие человеку
            log.info("proactive_send: модель промолчала — не пишу")
            return {"ok": False, "error": "пустой ответ модели", "chat_id": chat_id}
        # 28.10: через общий метод — иначе «написала первой» уходило
        # полотном (дробление было только в _full_reply).
        await self.send_humanlike(chat_id, reply, chat_type="private")

        # инициатива = сильнейший якорь ожидания: она ждёт, что ответят
        if self.affect is not None:
            try:
                self.affect.commitments.on_sent(
                    chat_id, text=reply, proactive=True,
                    w_rel=self.affect.relation_weight(chat_id),
                    affect_high=self.emotions.state.intensity >= 0.55)
            except Exception:
                log.debug("проактивное обязательство не записалось",
                          exc_info=True)

        asyncio.ensure_future(self._assess_emotion(chat_id, "(ты написала первой)", reply))
        return {"ok": True, "sent": reply, "chat_id": chat_id, "source": source}

    def inject_thought(self, text: str) -> dict[str, Any]:
        """Вселить мысль: она примет её за собственную (учтётся в следующем ответе)."""
        text = text.strip()
        if not text:
            raise ValueError("пустая мысль")
        thought = {"text": text, "ts": time.time(), "used": False}
        self.injected_thoughts.append(thought)
        self.injected_thoughts = self.injected_thoughts[-10:]   # держим последние 10
        self.bus.emit("thought.injected", text=text, ts=thought["ts"])
        log.info("мысль вселена: %s", text[:80])
        return thought

    def consume_thoughts(self) -> str:
        """Забрать вселённые мысли для промпта (помечает использованными)."""
        pending = [t for t in self.injected_thoughts if not t["used"]]
        for t in pending:
            t["used"] = True
        return "\n".join(f"- {t['text']}" for t in pending)

    async def _describe_sticker(self, msg: IncomingMessage, fallback: str = "") -> str:
        """Прочитать стикер как сообщение: эмоция, смысл, уместность.

        Использует prompts/sticker_to_text.md через vision-модель. Если
        vision недоступен — возвращает пустую строку, и разговор идёт дальше
        с пометкой «[стикер]».
        """
        cfg = self.config.current
        if not msg.sticker_file_id or not cfg.prompts.sticker_vision:
            return ""
        if not cfg.llm.vision_model:
            return ""
        path = await self.telegram.download_file(
            msg.sticker_file_id, msg.chat_id, msg.message_id)
        if not path:
            return ""
        try:
            import base64
            data = path.read_bytes()
            from .core.media import is_supported_image, sniff_image_mime, sticker_frame
            mime = sniff_image_mime(path, data[:32])
            if not is_supported_image(mime):
                # видео-стикер (webm) или tgs: vision не примет контейнер —
                # достаём первый кадр PNG и работаем уже с ним
                frame = sticker_frame(path)
                if frame is None:
                    log.warning("формат стикера не поддерживается: %s — пропускаю", mime)
                    return ""
                path = frame
                data = path.read_bytes()
                mime = sniff_image_mime(path, data[:32])
                if not is_supported_image(mime):
                    log.warning("кадр стикера не распознан: %s — пропускаю", mime)
                    return ""
            b64 = base64.b64encode(data).decode()
            instruction = self.prompts.get("sticker_to_text") or (
                "Опиши стикер для понимания в чате: эмоция, смысл, уместность. Кратко."
            )
            result = await self.vision_llm.chat(
                [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": instruction},
                        {"type": "image_url",
                         "image_url": {"url": f"data:{mime};base64,{b64}"}},
                    ],
                }],
                model=self.vision_llm.model,
                chat_label=str(msg.chat_id),
                function_label="sticker",
                temperature=0.2,
            )
            return result.content.strip()[:600]
        except Exception as e:
            log.warning("стикер не распознан: %s", str(e)[:120])
            return ""

    async def _describe_photo(self, msg: IncomingMessage) -> str:
        """Распознаёт фото через vision_model (gemini-flash и т.п.).

        Формат OpenAI vision: content-массив с image_url/data-URL.
        При ошибке возвращает пустую строку — разговор продолжается без фото.
        """
        cfg = self.config.current
        vision_model = cfg.llm.vision_model
        if not vision_model:
            return ""
        path = await self.telegram.download_file(msg.photo_file_id, msg.chat_id, msg.message_id)
        if not path:
            return ""
        try:
            data = path.read_bytes()
            # MIME по содержимому, а не по расширению: TDLib часто сохраняет
            # webp/heic под именем .jpg, и провайдер отвечал 400.
            from .core.media import is_supported_image, sniff_image_mime
            mime = sniff_image_mime(path, data[:32])
            if not is_supported_image(mime):
                log.warning("формат фото не поддерживается: %s (%s) — пропускаю",
                            mime, path.name)
                return ""
            b64 = base64.b64encode(data).decode()
            caption_instruction = self.prompts.get("photo_to_text") or (
                "Опиши подробно, что на фото: кто/что изображено, детали, настроение. "
                "Если есть текст на картинке — прочитай. По-русски."
            )
            result = await self.vision_llm.chat(
                [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": caption_instruction},
                        {"type": "image_url",
                         "image_url": {"url": f"data:{mime};base64,{b64}"}},
                    ],
                }],
                model=self.vision_llm.model,
                chat_label=str(msg.chat_id),
                function_label="vision",
                temperature=(self.config.current.llm.vision.temperature
                             if self.config.current.llm.vision.temperature >= 0 else 0.3),
            )
            return result.content.strip()
        except Exception as e:
            log.warning("vision (%s) не смог распознать фото: %s", vision_model, e)
            return ""

    async def _chat_with_tools(self, messages: list[dict], chat_label: str) -> tuple[str, list[str]]:
        """Цикл function-calling; возвращает (текст, пути артефактов).

        Модели без tool-calling (например Gemma free) ретраятся без инструментов:
        память тогда идёт через текстовую команду #remember (как в оригинале).
        """
        artifacts: list[str] = []
        tools = self.skills.openai_tools()
        text_command_memory = False
        # 4 итерации хватало на «поставь реакцию», но не на работу с кодом:
        # чтение файла + правка + тесты — это уже три шага без ответа.
        max_steps = self.config.current.harness.max_tool_steps
        for _ in range(max_steps):
            try:
                result = await self._chat_resilient(
                    messages, tools=tools, chat_label=chat_label,
                )
            except LLMError as e:
                if tools and ("tool-calling" in str(e) or "tool_call" in str(e)):
                    log.warning("модель без tool-calling — переключаюсь на #remember-режим")
                    tools = ()
                    text_command_memory = True
                    result = await self._chat_resilient(messages, tools=(), chat_label=chat_label)
                else:
                    raise
            if not result.tool_calls:
                reply = result.content
                if text_command_memory:
                    reply = await self._absorb_text_memory(reply, chat_label)
                return reply, artifacts
            messages.append({
                "role": "assistant", "content": result.content or None,
                "tool_calls": [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.name,
                                  "arguments": json.dumps(tc.arguments, ensure_ascii=False)}}
                    for tc in result.tool_calls
                ],
            })
            for tc in result.tool_calls:
                # предохранитель: аффективный пик не должен дотянуться
                # до необратимых инструментов (self_edit, канал, профиль)
                if self.affect is not None:
                    decision = self.affect.check_action(tc.name, tc.arguments)
                    if not decision.allowed:
                        log.warning("инструмент %s заблокирован (%s)",
                                    tc.name, decision.zone)
                        messages.append({
                            "role": "tool", "tool_call_id": tc.id,
                            "content": (f"инструмент недоступен сейчас ({decision.zone}); "
                                        f"попробуй позже или другим способом"),
                        })
                        continue
                outcome = await self.skills.dispatch(tc.name, tc.arguments)
                if outcome.artifact_path:
                    artifacts.append(outcome.artifact_path)
                messages.append({"role": "tool", "tool_call_id": tc.id,
                                 "content": outcome.output[:16000]})
        # цикл выродился в повторные вызовы — просим ответить без инструментов
        final = await self._chat_resilient(messages, chat_label=chat_label)
        reply = final.content
        if self.config.current.prompts.anti_repeat and self._looks_repetitive(reply, chat_label):
            log.info("ответ похож на повтор — включаю anti_repeat")
            system = self.persona.build(
                chat_title=chat_label, repeated=True,
                working_memory=self.working.content(),
            )
            retry = await self._chat_resilient(
                [system_msg(system)] + messages[-6:], chat_label=chat_label)
            if retry.content.strip():
                reply = retry.content
        return reply, artifacts

    def _looks_repetitive(self, reply: str, chat_label: str) -> bool:
        """Грубая детекция повтора: тот же текст уже был в истории чата."""
        text = (reply or "").strip()
        if len(text) < 12:
            return False
        for chat_history in self.histories.values():
            for item in chat_history:
                if item.get("role") == "assistant" and text == (item.get("content") or "").strip():
                    return True
        return False

    _REMEMBER_RE = re.compile(r"#remember\s+(.+)", re.IGNORECASE)
    _REACT_RE = re.compile(r"#react\s+([\U0001F300-\U0001FAFF\u2600-\u27BF\uFE0F]+)")
    #: 28.10: «лучше промолчать» — команда из prompts/anti_repeat.md,
    #: которой не было в коде.
    _WAIT_RE = re.compile(r"#wait\b", re.IGNORECASE)

    async def _absorb_reactions(self, reply: str, msg: IncomingMessage) -> str:
        """Вырезает #react-команды и ставит реакции на сообщение собеседника."""
        emojis = self._REACT_RE.findall(reply)
        if not emojis:
            return reply
        clean = self._REACT_RE.sub("", reply).strip()
        for emoji in emojis[:2]:
            emoji = emoji.replace("\ufe0f", "") or "👍"
            try:
                ok = await self.telegram.add_reaction(msg.chat_id, msg.message_id, emoji)
                log.info("реакция %s на %s/%s: %s", emoji, msg.chat_id,
                         msg.message_id, "ok" if ok else "fail")
            except Exception:
                log.exception("реакция не встала")
        # пустой остаток — нормально: реакция и была всем ответом.
        # Раньше возвращался исходный текст и «#react 👀» утекало в чат.
        return clean

    async def _absorb_text_memory(self, reply: str, chat_label: str) -> str:
        """Вырезает #remember-команды из текста и кладёт их в дневник."""
        matches = self._REMEMBER_RE.findall(reply)
        if not matches:
            return reply
        clean = self._REMEMBER_RE.sub("", reply).strip()
        for note in matches:
            note = note.strip().strip("#").strip()
            if not note:
                continue
            try:
                await self.diary.add(note, tags=["remember"], source="chat")
                log.info("#remember усвоено: %s", note[:80])
            except Exception:
                log.exception("#remember не записался")
        # заметка ушла в дневник; если текста больше нет — молчим
        return clean

    async def _send_voice_reply(self, chat_id: int, text: str) -> None:
        """Синтез голоса с очисткой скрипта (промпт record_audio_speech)."""
        cfg = self.config.current
        if not self.voice.can_speak:
            return
        prompt = ""
        if cfg.prompts.voice_speech:
            prompt = self.prompts.get("record_audio_speech",
                                      name=cfg.general.character_name) or ""
        try:
            out = self.data_dir / "voice" / f"reply_{int(time.time())}.wav"
            path = await self.voice.speak(text, out, prompt=prompt, llm=self.llm)
            await self.telegram.send_voice_file(
                chat_id, path, self.voice.wav_duration(path))
        except Exception as e:
            log.warning("голосовой ответ не вышел: %s", str(e)[:120])

    def _social_payload(self, chat_id: int, chat_title: str = "") -> dict:
        """Социальное состояние чата для промпта и лога."""
        if self.affect is None:
            return {}
        try:
            state = self.affect.social_state(chat_id)
            payload = state.to_dict()
            payload["chat_title"] = chat_title
            # выгнали или замутили — разные вещи, она должна их
            # различать, а не только чувствовать тяжесть
            payload["kicked"] = state.last_event in ("kicked", "banned")
            payload["muted"] = bool(state.is_muted)
            rum = self.affect.rumination(chat_id)
            if rum is not None:
                payload["rumination_charge"] = round(rum.current_charge, 3)
                payload["rumination_mode"] = rum.mode_name()
            return payload
        except Exception:
            log.debug("социальное состояние недоступно", exc_info=True)
            return {}

    def _after_appraisal(self, msg: IncomingMessage, user_text: str,
                         witnesses: int) -> None:
        """После оценки: удар по социальным потребностям и след обиды.

        Разделение важно: appraisal отвечает на «насколько это плохо для
        меня сейчас», а социальная боль — «что это сделало со мной как
        с человеком в этом чате». Обида от близкого бьёт по самооценке и
        принадлежности, мут — по контролю и значимости, и это разные
        состояния с разными последствиями.
        """
        vec = getattr(self.affect, "_last_appraisal", None)
        if vec is None:
            return
        pain = None
        if vec.congruence <= -0.35:
            pain = self.affect.apply_social_event(
                msg.chat_id, "insult", vec, witnesses=witnesses)
        elif vec.congruence >= 0.55:
            self.affect.apply_social_event(
                msg.chat_id, "apology", vec, witnesses=witnesses)

        # руминация: рождается, когда событие того стоит
        self.affect.spawn_rumination(
            msg.chat_id, vec,
            social_pain=pain.pain_index if pain else 0.0,
            summary=user_text[:250],
            author_id=msg.sender_user_id,
            message_id=msg.message_id,
        )

    def _somatic_payload(self, chat_id: int) -> dict[str, float]:
        """Тело для согласования эмоции: PAD + социальная боль."""
        if self.affect is None:
            return {}
        try:
            snap = self.affect.snapshot
            pain = 0.0
            try:
                pain = self.affect.social_state(chat_id).pain_index
            except Exception:
                pass
            return {"p": snap.pad.p, "a": snap.pad.a,
                    "dom": snap.pad.dom, "pain": pain}
        except Exception:
            return {}

    def _mono_dict(self) -> dict[str, float]:
        """Текущие моноамины в виде словаря — для журнала причин."""
        if self.affect is None:
            return {}
        try:
            mono = self.affect.snapshot().mono
            return {"s": round(mono.s, 4), "n": round(mono.n, 4),
                    "d": round(mono.d, 4)}
        except Exception:
            return {}

    async def _assess_emotion(self, chat_id: int, user_text: str, reply_text: str) -> None:
        cfg = self.config.current
        instruction = self.prompts.get(
            "emotions",
            emotion=self.emotions.state.describe(),
        )
        try:
            data = await self.emotion_llm.chat_json(
                [
                    {"role": "system", "content": instruction or DEFAULT_EMOTION_PROMPT},
                    {"role": "user", "content": f"Собеседник: {user_text[:500]}\n\nТвой ответ: {reply_text[:500]}"},
                ],
                chat_label=str(chat_id), function_label="emotion",
            )
        except Exception as e:
            log.warning("эмоциональная оценка не прошла: %s", str(e)[:120])
            return
        before_state = asdict(self.emotions.state)
        # Замер ДО применения: моноамины живут здесь, а не в эмоциях.
        before_mono = self._mono_dict()
        # Тело для согласования: PAD + социальная боль этого чата.
        somatic = self._somatic_payload(chat_id)
        if self.config.current.affect.variant_a_reconcile:
            state = self.emotions.apply_assessment(data, chat_id,
                                                   somatic=somatic)
        else:
            state = self.emotions.apply_assessment(data, chat_id)
        try:
            self.emotion_log.record(
                chat_id=chat_id,
                message_id=self._last_message_ids.get(chat_id, 0),
                source="emotion",
                event="assessment",
                appraisal={"delta_valence": data.get("valence"),
                           "threat": data.get("threat"),
                           "intensity": data.get("intensity")},
                mono_before=before_mono,
                mono_after=self._mono_dict(),
                emotion_before={"emotion": before_state.get("emotion"),
                                "intensity": before_state.get("intensity")},
                emotion_after={"emotion": state.emotion,
                               "intensity": state.intensity},
                reason=state.reason,
            )
        except Exception:
            log.debug("лог эмоции не записался", exc_info=True)
        note = str(data.get("diary_note") or "").strip()
        if note:
            # запись в дневник: важность/эмоция берём из оценки, а не выдумываем
            salience = {"importance": 0.5, "surprise": 0.3,
                        "emotion": float(state.intensity or 0.0)}
            if cfg.memory.use_llm_salience:
                try:
                    salience = await estimate_importance(
                        self.llm, f"{user_text[:400]} / {reply_text[:400]}",
                        emotion_intensity=float(state.intensity or 0.0))
                except Exception:
                    log.warning("оценка важности не удалась — беру значения по умолчанию")
            try:
                await self.diary.add(
                    note, tags=["эмоция"], source="emotion", chat_id=chat_id,
                    importance=salience.get("importance", 0.5),
                    surprise=salience.get("surprise", 0.3),
                    emotion=salience.get("emotion", float(state.intensity or 0.0)),
                    provenance=f"emotion:{chat_id}",
                )
            except Exception:
                log.exception("эмоциональная запись в дневник не удалась")

    # ------------------------------------------------------ echo-диагностика

    async def _on_social(self, event: dict) -> None:
        """Социальный отказ: исключили из группы или лишили права писать.

        Зачем отдельный путь: раньше такие события не доходили до эмоций
        вообще — исключение отбрасывалось как служебное сообщение, а 403
        при отправке улетал наверх. Она не знала, что её отвергли, и мут
        в двух группах прошёл без следа.

        Вес события выше обычной реплики: это не «мне сказали неприятное»,
        а «меня здесь больше нет». Поэтому threat максимальный, valence
        предельно отрицательная, а RPE считаем от ожидания, что в этом
        чате её принимают.
        """
        kind = str(event.get("kind") or "")
        chat_id = int(event.get("chat_id") or 0)
        title = event.get("chat_title") or str(chat_id)
        actor = int(event.get("actor_id") or 0)
        message_id = int(event.get("message_id") or 0)

        # 28.10: одно и то же отвержение приходит МНОГОКРАТНО. Каждая
        # неудачная попытка что-то отправить в замьюченный чат порождает
        # новое событие "muted" — и она трижды писала хозяину одно и то
        # же про «клуб». Дедупликация по (чат, тип) с окном 6 часов:
        # состояние уже учтено, повтор ничего не добавляет.
        key = (chat_id, kind)
        now_ts = time.time()
        last = self._social_seen.get(key, 0.0)
        if now_ts - last < 6 * 3600:
            log.debug("повторное %s в чате %s — пропускаю", kind, chat_id)
            return
        self._social_seen[key] = now_ts

        # насколько это неожиданно: в близком чате больнее
        try:
            relation = self.affect.relation_weight(chat_id) if self.affect else 1.0
        except Exception:
            relation = 1.0

        text_for_impulse = {
            "kicked": "меня выгнали из группы, я здесь больше не нужна",
            "muted": "мне запретили писать в этом чате",
        }.get(kind, "меня отвергли")

        try:
            # AppraisalImpulse — frozen dataclass. Раньше здесь шло
            # присваивание полей, каждое бросало FrozenInstanceError,
            # исключение глоталось except-ом, и мут не доходил до эмоций
            # вообще. Собираем вектор через appraisal и идём общим путём.
            from .affect.appraisal import FastAppraisalExtractor
            vec = FastAppraisalExtractor().extract_from_system_event(
                kind, w_rel=max(1.0, relation * 1.5),
                witnesses=int(event.get("witnesses") or 0))
            self.affect.apply_appraisal(
                vec, chat_id=chat_id, user_id=actor, source=f"social.{kind}")
            # и в когнитивный автомат социальной боли — там стадии
            self.affect.apply_social_event(
                chat_id, "muted" if kind == "muted" else "kicked", vec,
                witnesses=int(event.get("witnesses") or 0))
            self.affect.spawn_rumination(
                chat_id, vec,
                social_pain=self.affect.social_state(chat_id).pain_index,
                summary=text_for_impulse, author_id=actor,
                message_id=message_id)
        except Exception:
            log.exception("социальное событие не легло в аффект")

        # Явная запись в лог: по ней видно, что она знала о муте,
        # а не просто «состояние изменилось».
        try:
            state = self.affect.social_state(chat_id)
            log.warning(
                "СОЦИАЛЬНЫЙ ОТКАЗ | %s | чат %s «%s» | автор %s | msg %s | "
                "боль=%.2f стадия=%s кластер=%s | muted=%s | "
                "потребности bel=%.2f est=%.2f ctrl=%.2f mean=%.2f",
                kind, chat_id, title, actor, message_id,
                state.pain_index, state.stage, state.cluster(),
                state.is_muted, state.need_belong, state.need_esteem,
                state.need_control, state.need_meaning)
        except Exception:
            log.warning("социальный отказ: %s в %s (автор %s)",
                        kind, title, actor)
        if self.bus is not None:
            self.bus.emit("social.rejected", kind=kind, chat_id=chat_id,
                          title=title, actor_id=actor, message_id=message_id)

        # записать в память как значимый эпизод: обида должна помниться
        try:
            await self.diary.add(
                f"Меня {('выгнали из группы' if kind == 'kicked' else 'лишили права писать')} "
                f"«{title}». Это было неприятно.",
                tags=["отвержение", kind, title],
                source=f"social.{kind}")
        except Exception:
            log.debug("эпизод отвержения не записался", exc_info=True)

        # сказать хозяину: такое она не должна переживать молча
        owner = self.config.current.general.papik_chat_id
        if owner:
            try:
                await self.telegram.send_text(
                    owner,
                    f"меня {('выгнали из «' + str(title) + '»') if kind == 'kicked' else ('лишили права писать в «' + str(title) + '»')}. "
                    f"неприятно, если честно.")
            except Exception:
                pass

    async def _is_stale(self, msg: IncomingMessage) -> bool:
        """Сообщение из бэклога TDLib, а не живой разговор.

        28.10: нашёл критик. После рестарта накопленные апдейты приходят
        пачкой — в логе 74 записи с одинаковым ts в интервале 0.14 с,
        39 записей за 0.019 с. Она честно отвечала на каждое, включая
        сообщения часовой давности: «...у меня тоже. От твоих лекций.»
        на реплику из прошлой сессии.

        Отвечать на такое бессмысленно: человек давно ушёл. Читаем для
        контекста, но не отвечаем.
        """
        mt = float(getattr(msg, "message_ts", 0) or 0)
        if not mt:
            return False                 # TDLib не дал дату — не гадаем
        age = time.time() - mt
        return age > self.STALE_MESSAGE_SEC

    async def _echo_reply(self, msg: IncomingMessage) -> None:
        """Диагностика конвейера — только хозяину, никогда в чужие чаты.

        Почему так: при недоступном LLM она отвечала этой диагностикой на
        любое сообщение, включая группы. Люди видели "🩺 echo-режим (LLM
        НЕ доступен), модули: app=…" и двух чатах замутили её за это.
        Служебный текст — не реплика персонажа: в чужие чаты он не идёт
        вообще.
        """
        owner = self.config.current.general.papik_chat_id
        human = self.telegram.human
        if human:
            await human.mark_read(msg.chat_id, [msg.message_id], delay=False)

        # 28.10: белый список, а не чёрный. Раньше условие было
        # `if owner and chat_id != owner` — при пустом papik_chat_id оно
        # ЛОЖНО, и диагностика уходила во все чаты включая группы
        # (167 таких сообщений 26.10). Теперь без известного хозяина
        # не шлём вообще.
        if not owner or msg.chat_id != owner:
            log.info("echo-режим: чат %s не хозяйский — молчу", msg.chat_id)
            return

        llm_ok = self.modules.get("llm") == "ok"
        lines = [
            f"🩺 echo-режим (LLM {'доступен, но включён echo' if llm_ok else 'НЕ доступен'})",
            f"принято: {msg.text[:80]!r}",
            f"чат: {msg.chat_title} (id={msg.chat_id}, тип={msg.chat_type}, "
            f"pinned={msg.is_pinned}, muted={msg.is_muted})",
            f"модули: " + ", ".join(f"{k}={v}" for k, v in self.modules.items()),
            f"дневник: {self.diary_store.count()} записей; "
            f"промптов: {len(self.prompts._cache) if self.prompts else 0} в кэше",
        ]
        if msg.is_voice:
            lines.append(f"войс file_id={msg.voice_file_id} (whisper: "
                         f"{'да' if self.voice.can_transcribe else 'нет'})")
        text = "\n".join(lines)
        if human:
            await human.typing_session(msg.chat_id, text)
        await self.telegram.send_text(msg.chat_id, text)

    async def _harness_progress(self, line: str) -> None:
        """Прогресс кодинг-задачи в чат хозяина — чтобы не выглядело мёртвым."""
        owner = self.config.current.general.papik_chat_id
        if not owner:
            return
        # не спамим: только содержательные строки и не чаще раза в 5с
        now = time.time()
        if now - getattr(self, "_last_progress_ts", 0.0) < 5.0:
            return
        if len(line.strip()) < 4:
            return
        self._last_progress_ts = now
        try:
            await self.telegram.send_text(owner, line[:300])
        except Exception:
            pass

    async def _on_callback(self, chat_id: int, message_id: int,
                           sender_user_id: int, data: str) -> None:
        """Нажатие inline-кнопки: только владелец, только свои запросы."""
        owner = self.config.current.general.papik_chat_id
        if owner and chat_id != owner:
            log.warning("кнопка из чужого чата %s — игнорирую", chat_id)
            return
        verdict = self.approvals.resolve(chat_id, data, sender_user_id)
        if verdict and message_id:
            await self.telegram.edit_message_text(chat_id, message_id, verdict)

    async def _owner_command(self, msg: IncomingMessage) -> bool:
        """Служебные команды создателя (papik). Возвращает True если обработано."""
        text = (msg.text or "").strip().lower()
        if not text.startswith("/"):
            return False
        cmd = text.split()[0]
        if cmd == "/status":
            info = [f"⏱ аптайм {int(self.telegram.uptime())}с",
                    f"модули: {self.modules}",
                    f"дневник: {self.diary_store.count()} записей",
                    f"последний сон: {self.sleep_cycle.last_stats.to_dict() if self.sleep_cycle.last_stats else 'ещё не спала'}"]
            await self.telegram.send_text(msg.chat_id, "\n".join(info))
            return True
        if cmd == "/diag":
            await self._echo_reply(msg)
            return True
        if cmd == "/sleep":
            asyncio.ensure_future(self._do_sleep())
            await self.telegram.send_text(msg.chat_id, "легла спать 💤")
            return True
        if cmd == "/chatid":
            await self.telegram.send_text(msg.chat_id, f"chat_id = {msg.chat_id}")
            return True
        if cmd in ("/trauma", "/sim"):
            # Травмировать её намеренно, по полному боевому контуру.
            # Нужно, чтобы проверить переживание, не устраивая травлю
            # в реальных чатах. Откат — /rollback.
            args_text = text.split(maxsplit=1)[1].strip() if len(text.split()) > 1 else ""
            if not args_text or args_text in ("help", "?"):
                scenarios = ", ".join(sorted(
                    getattr(self.affect, "SIM_SCENARIOS", {}) or {}))
                await self.telegram.send_text(
                    msg.chat_id,
                    f"травма: /trauma <сценарий>\nесть: {scenarios}\n"
                    f"откат: /rollback")
                return True
            scenario = args_text.split()[0].lower()
            try:
                result = self.affect.simulate_social_event(
                    scenario, chat_id=msg.chat_id,
                    actor_id=msg.sender_user_id, actor_name="papik")
            except ValueError as e:
                await self.telegram.send_text(msg.chat_id, f"нет такого: {e}")
                return True
            except Exception as e:
                log.exception("симуляция травмы не прошла")
                await self.telegram.send_text(
                    msg.chat_id, f"симуляция упала: {str(e)[:160]}")
                return True
            d = result["mono_delta"]
            rum = result.get("rumination")
            await self.telegram.send_text(
                msg.chat_id,
                (f"🩹 {result['scenario']}\n"
                 f"боль: {result['pain_before']:.2f} → {result['pain_after']:.2f}\n"
                 f"стадия: {result['stage']}, кластер: {result['cluster']}\n"
                 f"S/N/D: {d['ds']:+.2f} / {d['dn']:+.2f} / {d['dd']:+.2f}\n"
                 f"руминация: " + (f"{rum['charge']:.2f} ({rum['mode']})"
                                  if rum else "нет")))
            return True
        if cmd in ("/rollback", "/undo"):
            # Вернуть состояние из последнего снимка. Требует, чтобы
            # снимок был снят заранее: tools/snapshot.py save
            import subprocess
            try:
                proc = subprocess.run(
                    [sys.executable, str(self.root / "tools" / "snapshot.py"),
                     "restore"],
                    capture_output=True, text=True, timeout=60,
                    encoding="utf-8", errors="replace")
                out = (proc.stdout or proc.stderr or "").strip()
                self.affect._load_state()
                self.affect._load_social_states()
                self.affect._load_rumination()
                await self.telegram.send_text(
                    msg.chat_id,
                    f"↩️ откат выполнен\n{out[:400]}\n"
                    f"состояние перечитано из базы")
            except Exception as e:
                await self.telegram.send_text(
                    msg.chat_id, f"откат не прошёл: {str(e)[:200]}")
            return True
        if cmd in ("/memory", "/background", "/sleepmode"):
            arg = text.split(maxsplit=1)[1] if len(text.split()) > 1 else ""
            bcfg = self.config.current.background
            if arg in ("sleep", "сон", "night"):
                self.background.set_mode("sleep")
                await self.telegram.send_text(
                    msg.chat_id,
                    f"память: ночной режим ʹ сон в {self.config.current.diary.consolidation_hour}:00")
                return True
            if arg in ("background", "фон", "bg", "on"):
                self.background.set_mode("background")
                await self.telegram.send_text(
                    msg.chat_id,
                    f"память: фоновый режим ʹ в сети всегда, тик {bcfg.interval_minutes:.0f} мин")
                return True
            snap = self.background.snapshot()
            await self.telegram.send_text(
                msg.chat_id,
                (f"память: {snap['mode']}\n"
                 f"проходов: {snap['passes']}, ошибок: {snap['errors']}\n"
                 f"эпизодов: {self.diary_store.count()}\n"
                 f"сменить: /memory sleep | /memory background"))
            return True
        return False

    # ------------------------------------------------------------ фоновое

    async def _do_sleep(self) -> None:
        """Ночной сон: консолидация памяти + восстановление аффекта.

        27.10: раньше здесь была только консолидация, а `affect.on_wake()`
        не вызывался ниоткуда, кроме тестов. Из-за этого усталость
        накапливалась линейно (1.0 за 8 часов бодрствования) и
        застревала на максимуме навсегда: снять её было нечем.
        Теперь сон делает обе вещи, в правильном порядке — сначала
        память (пока состояние ещё «ночное»), потом пробуждение.
        """
        cfg = self.config.current
        log.info("пора спать: консолидация памяти")
        stats = await self.sleep_cycle.run(
            similarity_threshold=cfg.memory.similarity_threshold,
            lookback_days=cfg.memory.lookback_days,
        )
        log.info("сон завершён: %s", stats.to_dict())

        if self.affect is not None:
            try:
                self.affect.on_wake()
                log.info("пробуждение: усталость снята, моноамины к базе")
            except Exception:
                log.exception("on_wake не отработал")

    async def _diary_save(self) -> None:
        """Фоновая дневниковая запись (промпт diary_save).

        Раз в несколько часов она перечитывает последние реплики и пишет
        от себя короткие самодостаточные куски — то, что не случилось
        по ходу разговора.
        """
        cfg = self.config.current
        if not cfg.prompts.diary_save:
            return
        instruction = self.prompts.get(
            "diary_save", name=cfg.general.character_name) or ""
        if not instruction:
            return

        # 27.10: раньше здесь склеивались ВСЕ чаты в одну кучу, и запись
        # уходила в память без chat_id и без автора. Из-за этого 23% памяти
        # не знало, откуда оно, а «люблю кофе» от владельца и от чужого
        # бота сливались в один факт. Теперь — по чатам, с атрибуцией.
        for chat_id, chat_history in list(self.histories.items()):
            recent = [item for item in chat_history
                      if item.get("role") in ("user", "assistant")]
            if len(recent) < 4:
                continue          # слишком мало, чтобы что-то запоминать

            tail = "\n".join(
                f"[{item.get('speaker') or '?'}]: {item.get('content') or ''}"
                for item in recent[-40:]
            )
            # кто в этом чате главный собеседник — по последней реплике
            last_user = next((i for i in reversed(recent)
                              if i.get("role") == "user"), {})
            speaker_id = int(last_user.get("speaker_id") or 0)
            speaker_name = str(last_user.get("speaker") or "")
            chat_title = str(last_user.get("chat_title") or "")

            try:
                data = await self._chat_resilient_json(
                    [{"role": "system", "content": instruction},
                     {"role": "user", "content": tail}],
                    function_label="diary_save",
                )
            except Exception as e:
                log.warning("дневниковая запись не удалась (чат %s): %s",
                            chat_id, str(e)[:120])
                continue

            text = str(data.get("diary") or data.get("text") or "").strip()
            if not text:
                continue
            # diary_save требует самодостаточных кусков через ---
            for piece in [p.strip() for p in text.split("---") if p.strip()]:
                try:
                    await self.diary.add(
                        piece, source="diary_save",
                        chat_id=chat_id,
                        speaker_id=speaker_id,
                        speaker_name=speaker_name,
                        chat_title=chat_title,
                        importance=float(data.get("importance") or 0.5),
                        provenance="diary_save",
                    )
                except Exception:
                    log.exception("кусок дневника не записался (чат %s)", chat_id)

    async def _news_digest(self) -> None:
        cfg = self.config.current
        instruction = self.prompts.get("news_digest")
        for channel in cfg.news.channels:
            chat_id = await self.telegram.search_public_chat(channel)
            if not chat_id:
                continue
            try:
                messages = await self.telegram.get_chat_history(chat_id, limit=25)
            except Exception:
                log.exception("не смогла прочитать канал %s", channel)
                continue
            texts = []
            for m in messages:
                content = m.get("content", {})
                if content.get("@type") == "messageText":
                    t = (content.get("text") or {}).get("text", "")
                    if t:
                        texts.append(t[:300])
            if not texts:
                continue
            try:
                data = await self.llm.chat_json([
                    {"role": "system", "content": instruction or DEFAULT_NEWS_PROMPT},
                    {"role": "user", "content": "\n---\n".join(texts[:20])},
                ], function_label="news")
                summary = str(data.get("summary") or "").strip()
                if summary:
                    await self.diary.add(summary, tags=["новости", channel], source="news")
            except LLMError:
                log.exception("дайджест %s не собрался", channel)

    async def _offline_check(self) -> None:
        if self.telegram.human:
            await self.telegram.human.maybe_go_offline()
        # утро: если уже не ночь, а в очереди есть ночные сообщения — разбираем
        if self.night_queue.pending_count():
            if not (self.telegram.human and self.telegram.human.is_night_now()):
                await self._flush_night_queue()

    # ---------------------------------------------------- режим сна (28.10)

    async def _sleep_tick(self) -> None:
        """Шаг автомата сна. Опрашивается раз в 30 секунд.

        Побочных эффектов у самого SleepManager нет — он только считает
        давление и гейт. Всё, что меняет поведение, делается здесь: смена
        фазы на сон отправляет прощальную реплику и глушит онлайн; выход
        из сна дёргает консолидацию памяти и восстановление аффекта.
        """
        if self.affect is None:
            return
        try:
            mono = self.affect.snapshot.mono
            homeo = self.affect.snapshot.homeostasis
            mental = self.affect.mental.snapshot
            prev_phase = self.affect.sleep.snapshot.phase

            # жив ли какой-то чат и ждёт ли кто ответа
            chat_p = self.affect.commitments.dialogue_pressure(
                lambda cid: self.affect.relation_weight(cid))
            wait_p, wait_chat = self.affect.commitments.waiting_pressure()
            rumination_max = max(
                (st.current_charge for st in self.affect._rumination.values()
                 if getattr(st, "current_charge", 0)), default=0.0)

            snap = self.affect.sleep.tick(
                fatigue=homeo.fatigue,
                load=mental.load,
                depletion=mental.depletion,
                sleep_debt=mental.sleep_debt,
                rumination=rumination_max,
                noradrenaline=mono.n,
                chat_pressure=chat_p,
                wait_pressure=wait_p,
            )
        except Exception:
            log.exception("тик сна упал")
            return

        # --- переходы
        if prev_phase != "wind_down" and snap.phase == "wind_down":
            log.info("хочет спать: drive=%.2f, держит=%s (%.2f)",
                     snap.drive, snap.delay_reason, snap.delay_gate)
            if self.bus is not None:
                self.bus.emit("sleep.intent", drive=round(snap.drive, 3),
                              delay_gate=round(snap.delay_gate, 3),
                              reason=snap.delay_reason, chat_id=wait_chat)

        if prev_phase == "wind_down" and snap.phase in ("nrem_sws", "rem"):
            await self._enter_sleep(prev_phase)

        elif prev_phase in ("nrem_sws", "rem") and snap.phase == "wake_inertia":
            log.info("пробуждение: качество сна %.2f", snap.quality)
            await self._finish_sleep(snap.quality)

        elif prev_phase == "wake_inertia" and snap.phase == "awake":
            log.info("проснулась окончательно")
            if self.night_queue.pending_count():
                await self._flush_night_queue()

    async def _enter_sleep(self, prev_phase: str) -> None:
        """Уйти в сон: сказать «спокойной ночи» и выключить онлайн."""
        snap = self.affect.sleep.snapshot
        # если только что был живой диалог — предупредить одним сообщением
        active_chat, active_p = 0, 0.0
        for chat_id, st in self.affect.commitments._chats.items():
            p = self.affect.commitments.dialogue_pressure(
                lambda cid: self.affect.relation_weight(cid))
            if p > active_p:
                active_chat, active_p = chat_id, p

        if active_chat and active_p >= 0.5:
            try:
                await self.proactive_send(
                    active_chat,
                    "Ты вырубаешься прямо посреди разговора, мана на нуле, "
                    "глаза закрываются. Скажи одной короткой фразой без "
                    "пафоса, что всё, ты спать, и отключись.",
                    source="sleep")
            except Exception:
                log.debug("прощальное сообщение не ушло", exc_info=True)

        if self.telegram.human is not None:
            try:
                await self.telegram.human.maybe_go_offline()
            except Exception:
                log.debug("офлайн не встал", exc_info=True)

        if self.bus is not None:
            self.bus.emit("sleep.phase", phase=snap.phase, prev=prev_phase,
                          quality=round(snap.quality, 3))

    async def _finish_sleep(self, quality: float) -> None:
        """Пробуждение: память, аффект, менталка."""
        cfg = self.config.current
        log.info("пора спать: консолидация памяти")
        try:
            stats = await self.sleep_cycle.run(
                similarity_threshold=cfg.memory.similarity_threshold,
                lookback_days=cfg.memory.lookback_days,
            )
            log.info("сон завершён: %s", stats.to_dict())
        except Exception:
            log.exception("консолидация памяти не прошла")

        try:
            self.affect.on_wake(quality=quality)
        except Exception:
            log.exception("on_wake не отработал")

        if self.bus is not None:
            self.bus.emit("sleep.phase", phase="wake_inertia", prev="rem",
                          quality=round(quality, 3))

    def _should_queue_message(self, msg: Any) -> bool:
        """Копить ли сообщение до утра.

        Заменяет статическую проверку часов: автомат сна знает фазу,
        а не только время суток. Владелец может разбудить настойчивостью.
        """
        if self.affect is None:
            return False
        try:
            owner = self.config.current.general.papik_chat_id or 0
            burst = False
            if owner and msg.chat_id == owner:
                # два сообщения подряд в течение минуты — это зов, не спам
                key = ("burst", owner)
                last = self._chat_activity.get(owner, {}).get("last_ts", 0.0)
                burst = (time.time() - float(last)) < 60.0
                self._chat_activity.setdefault(owner, {})["last_ts"] = time.time()

            if not self.affect.sleep.should_queue_message(
                    chat_id=msg.chat_id, owner_id=owner, burst=burst):
                return False

            # если разбудили — фиксируем прерванный сон
            if owner and msg.chat_id == owner and burst:
                self.affect.sleep.awaken()
                try:
                    self.affect.mental.register_awakening()
                except Exception:
                    log.debug("менталка: пробуждение не учтено", exc_info=True)
                if self.bus is not None:
                    self.bus.emit("sleep.interrupted",
                                  chat_id=owner,
                                  awakenings=self.affect.sleep.snapshot.awakenings,
                                  quality_after=round(
                                      self.affect.sleep.snapshot.quality, 3))
            return True
        except Exception:
            log.debug("решение об очереди упало", exc_info=True)
            return False

    def _burned_wait(self, chat_id: int) -> None:
        """Ждала ответа, не дождалась. Это остракизм, не «просто тишина»."""
        if self.affect is None:
            return
        try:
            from .affect.appraisal import FastAppraisalExtractor
            vec = FastAppraisalExtractor().extract_from_system_event(
                "ignored", w_rel=self.affect.relation_weight(chat_id))
            self.affect.apply_social_event(chat_id, "ignored", vec)
            log.info("напрасное ожидание в чате %s -> социальная боль",
                     chat_id)
        except Exception:
            log.debug("напрасное ожидание не учтено", exc_info=True)

    async def _flush_night_queue(self) -> None:
        """Утренний разбор: одно ответное сообщение на всю ночную переписку.

        Смысл — в возврате контакта, а не в поштучном отчёте. Она пишет
        «видела, что ты там ночью» и реагирует на суть, а не на каждую строку.
        """
        if self.modules.get("llm") != "ok":
            return
        grouped = self.night_queue.by_chat()
        for chat_id, messages in grouped.items():
            if not messages:
                continue
            # последние 12 реплик достаточно: остальные — тот же разговор
            tail = messages[-12:]
            transcript = "\n".join(
                f"[{clock.strftime('%H:%M', m.ts)}] {m.text}"
                for m in tail if m.text.strip()
            )
            if not transcript.strip():
                self.night_queue.mark_handled(chat_id)
                continue
            title = tail[-1].chat_title or str(chat_id)
            system = self.persona.build(
                chat_title=title, chat_type=tail[-1].chat_type,
                working_memory=self.working.content(),
                extra=(
                    "Эти сообщения пришли ночью, пока ты спала. Сейчас утро. "
                    "Ответь ОДНИМ коротким сообщением на всю переписку: "
                    "признай, что видела (без извинений в стиле ассистента), "
                    "отреагируй на суть последнего. Не пересказывай по пунктам."
                ),
            )
            messages_payload = [
                {"role": "system", "content": system},
                {"role": "user", "content": f"Ночная переписка:\n{transcript}"},
            ]
            try:
                reply, _ = await self._chat_with_tools(messages_payload, chat_label=title)
            except Exception as e:
                log.warning("утренний разбор не удался (чат %s): %s", chat_id, str(e)[:120])
                continue
            reply = (reply or "").strip()
            if not reply:
                self.night_queue.mark_handled(chat_id)
                continue
            reply = await self._absorb_reactions(reply, tail[-1])
            reply = await self._absorb_text_memory(reply, title)
            try:
                await self.send_humanlike(chat_id, reply)
                self.night_queue.mark_handled(chat_id)
                self.bus.emit("night.flushed", chat_id=chat_id, messages=len(tail))
                log.info("утренний разбор: чат %s, %d сообщений", chat_id, len(tail))
            except Exception:
                log.exception("утренний ответ не ушёл (чат %s)", chat_id)
        self.night_queue.purge()

    async def _reload_config(self) -> None:
        if self.config.maybe_reload():
            cfg = self.config.current
            self.prompts.directory = (self.root / cfg.general.prompts_dir).resolve()
            # основные поля обновляем у всех клиентов (секции наследуют [llm])
            for client in (self.llm, self.vision_llm, self.embed_llm,
                           self.emotion_llm, self.sleep_llm):
                client.base_url = cfg.llm.base_url.rstrip("/")
                client.api_key = cfg.llm.api_key
            # затем секции переопределяют то, что задано явно
            self._apply_endpoint(self.vision_llm, cfg.llm.vision, cfg.llm.vision_model or cfg.llm.model)
            self._apply_endpoint(self.embed_llm, cfg.llm.embedding, cfg.llm.embed_model or cfg.llm.model)
            self._apply_endpoint(self.emotion_llm, cfg.llm.emotion, cfg.llm.model)
            self._apply_endpoint(self.sleep_llm, cfg.llm.sleep, cfg.llm.model)
            self.llm.model = cfg.llm.model
            self.llm.embed_model = cfg.llm.embed_model

    @staticmethod
    def _apply_endpoint(client: LLMClient, section: Any, fallback_model: str) -> None:
        if section.base_url:
            client.base_url = section.base_url.rstrip("/")
        if section.api_key:
            client.api_key = section.api_key
        if section.model:
            client.model = section.model
        else:
            client.model = fallback_model

    @staticmethod
    def _trim_history(history: deque, soft_cap: int) -> None:
        while sum(len(m.get("content") or "") for m in history) > soft_cap and len(history) > 2:
            history.popleft()


def system_msg(content: str) -> dict:
    return {"role": "system", "content": content}


DEFAULT_EMOTION_PROMPT = (
    "Оцени свою эмоциональную реакцию на обмен репликами. Ответь СТРОГО JSON: "
    '{"emotion": "слово-эмоция по-русски", "valence": -1..1, "arousal": 0..1, '
    '"reason": "краткая причина по-русски", "diary_note": "если обмен значимый — '
    'дневниковая запись от первого лица, иначе пустая строка"}'
)

DEFAULT_NEWS_PROMPT = (
    "Тебе дают посты из телеграм-канала. Составь краткую выжимку того, что произошло. "
    "Политику пропускай. Ответь СТРОГО JSON: {\"summary\": \"3-6 предложений от первого лица, "
    "как если бы ты сама это прочитала и запомнила\"}."
)
