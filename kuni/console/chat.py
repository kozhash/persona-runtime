"""Чат с Куни в консоли — весь конвейер (личность, дневник, эмоции,
скиллы), но без Telegram и без HTTP-модели.

Режимы мозга:
  openai  — как обычно, [llm].base_url (Ollama и т.п.)
  bridge  — FileBridgeLLM: ответ модели пишет человек/агент в файл
            (data/devbrain/outbox/<id>.json)

Запуск:
  kuni chat                      # обычный чат с реальным LLM
  kuni chat --brain bridge       # «за рулём» — отвечаешь файлами ты
  kuni chat --brain bridge --once "привет"   # одно сообщение и выход
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from ..app import KuniApp
from ..core.config import write_default_config
from ..devbrain import FileBridgeLLM
from ..telegram.client import IncomingMessage

log = logging.getLogger(__name__)


class StubHuman:
    """Минимальное «человеческое поведение» для консоли: без задержек."""

    def is_night_now(self) -> bool:
        return False

    async def mark_read(self, *a: Any, **k: Any) -> None: return None
    async def go_online(self) -> None: return None
    async def maybe_go_offline(self, *a: Any, **k: Any) -> None: return None
    async def before_reply(self, chat_id: int, text_len: int) -> None:
        await asyncio.sleep(0.1)
    async def typing_session(self, chat_id: int, text: str) -> None: return None


class ConsoleChat:
    """Утка-замена TelegramModule для консоли (те же методы, что использует app)."""

    def __init__(self) -> None:
        self.human = StubHuman()
        self.me_user_id = 0
        self.authorized = True
        self.chats: dict[int, dict] = {}
        self._t0 = time.time()

    async def start(self) -> bool:
        return False

    def stop(self) -> None:
        return None

    def uptime(self) -> float:
        return time.time() - self._t0

    async def send_text(self, chat_id: int, text: str, reply_to: int = 0) -> int:
        print(f"\n🎀 Kuni> {text}", flush=True)
        return 0

    async def send_photo_file(self, chat_id: int, path: Path, caption: str = "") -> int:
        print(f"\n🎀 Kuni> [картинка: {path}]", flush=True)
        return 0

    async def send_voice_file(self, chat_id: int, path: Path, duration_sec: int) -> int:
        print(f"\n🎀 Kuni> [войс {duration_sec}с: {path}]", flush=True)
        return 0

    async def download_file(self, *a: Any, **k: Any) -> Path | None:
        return None

    async def search_public_chat(self, username: str) -> int | None:
        return None

    async def get_chat_history(self, chat_id: int, limit: int = 20) -> list[dict]:
        return []


async def run_chat(args: Any) -> int:
    root = Path(__file__).resolve().parents[2]
    cfg_path = root / "config.toml"
    if not cfg_path.exists():
        print("config.toml не найден — создаю из примера, заполни и повтори")
        write_default_config(cfg_path)
        return 1

    app = KuniApp(cfg_path)
    cfg = app.config.current
    cfg.telegram.enabled = False   # консоль вместо телеграма
    cfg.proxy.enabled = False      # bridge не стримит
    app.telegram = ConsoleChat()

    bridge: FileBridgeLLM | None = None
    if getattr(args, "brain", "openai") == "bridge":
        bridge = FileBridgeLLM(root / args.bridge_dir)
        bridge.bind_bus(app.bus)
        app.llm = bridge
        print(f"🧠 мост: {bridge.dir / 'outbox'} <- сюда пишешь ответы за модель")

    await app.run()

    if getattr(args, "once", None):
        return await _once(app, str(args.once))

    mode = "ТЫ — модель (отвечай outbox-файлами)" if bridge else cfg.llm.base_url
    print(f"\nЧат с {cfg.general.character_name} начат ({mode}). Пустая строка/Ctrl+C — выход.")
    message_id = 0
    try:
        while True:
            text = (await asyncio.to_thread(input, "\nТы> ")).strip()
            if not text:
                break
            message_id += 1
            msg = IncomingMessage(
                chat_id=1, message_id=message_id, sender_user_id=999,
                text=text, chat_title="консоль", chat_type="private",
            )
            try:
                await app.handle_message(msg)
            except Exception:
                log.exception("реплика не прошла")
    except (EOFError, KeyboardInterrupt):
        pass
    finally:
        await app.stop()
    return 0


async def _once(app: KuniApp, text: str) -> int:
    """Одно сообщение через весь конвейер; печатает итоговое состояние."""
    prev_emotion_ts = app.emotions.state.ts
    msg = IncomingMessage(chat_id=1, message_id=1, sender_user_id=999,
                          text=text, chat_title="консоль", chat_type="private")
    await app.handle_message(msg)
    # дождаться эмоциональной оценки (тоже проходит через мост/LLM)
    for _ in range(240):   # до 2 минут
        if app.emotions.state.ts > prev_emotion_ts:
            break
        await asyncio.sleep(0.5)
    st = app.emotions.state
    print(f"\n— эмоция: {st.emoji} {st.emotion} (v={st.valence:+.1f}, a={st.arousal:.1f})"
          f" — {st.reason or 'без причины'}")
    entries = app.diary_store.recent(3)
    if entries:
        print("— последние записи дневника:")
        for e in reversed(entries):
            print(f"   [{e.source}] {e.text[:90]}")
    wm = app.working.content().strip()
    if wm:
        print("— рабочая память:", wm.replace("\n", " | ")[:120])
    await app.stop()
    return 0
