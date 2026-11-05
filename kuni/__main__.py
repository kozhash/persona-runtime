"""CLI: kuni run | login | diagnose | send | init

  run      — основной режим (все модули)
  login    — интерактивный вход в Telegram (один раз)
  diagnose — проверка всех компонентов до подключения LLM
  send     — отправить сообщение от её имени (ручной режим)
  init     — создать config.toml из примера
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from . import __version__
from .core.config import write_default_config
from .core.logging import setup_logging


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


async def cmd_run(args: argparse.Namespace) -> int:
    from .app import KuniApp
    import logging
    # логи раньше конструктора: иначе сообщения инициализации (руки, архив,
    # политика путей) теряются — они пишутся до появления хендлера
    setup_logging(args.verbose)
    app = KuniApp(project_root() / "config.toml")
    log = logging.getLogger("kuni.run")

    # Сетевые обрывы Telegram (ConnectionResetError, Ping timeout) не должны
    # убивать процесс: TDlib сам переподключается, а asyncio-ошибка в
    # колбэке сокета роняла весь loop и вместе с ним бота.
    loop = asyncio.get_running_loop()

    def _loop_exception_handler(loop_: Any, context: dict) -> None:
        exc = context.get("exception")
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError,
                            BrokenPipeError, TimeoutError)):
            log.debug("сетевой обрыв проигнорирован: %s", exc)
            return
        message = context.get("message") or "неизвестная ошибка loop"
        log.error("необработанная ошибка в loop: %s", message, exc_info=exc)

    loop.set_exception_handler(_loop_exception_handler)

    if getattr(args, "brain", "openai") == "bridge":
        from .devbrain import FileBridgeLLM
        bridge = FileBridgeLLM(project_root() / args.bridge_dir)
        bridge.bind_bus(app.bus)
        app.llm = bridge
        app.config.current.proxy.enabled = False   # мост не стримит
        print(f"🧠 мост: {bridge.dir / 'outbox'} <- сюда пишешь ответы за модель")
    stop = asyncio.Event()
    try:
        import signal

        def _sig(*_: object) -> None:
            stop.set()

        loop = asyncio.get_running_loop()
        for sig_name in ("SIGINT", "SIGTERM"):
            sig = getattr(signal, sig_name, None)
            if sig is not None:
                try:
                    loop.add_signal_handler(sig, _sig)
                except NotImplementedError:
                    signal.signal(sig, _sig)  # windows
    except ImportError:
        pass

    await app.run()
    try:
        await stop.wait()
    finally:
        try:
            await app.stop()
        except Exception:
            log.exception("остановка прошла с ошибкой")
    return 0


async def cmd_login(args: argparse.Namespace) -> int:
    """Интерактивная авторизация: tdlib сам спросит телефон/код/2FA."""
    from .app import KuniApp
    setup_logging()
    # с --phone логин можно вести в фоне: вопросы/ответы через data/auth/*.txt
    app = KuniApp(project_root() / "config.toml", interactive=not args.phone)
    if args.phone:
        app.telegram.phone_hint = args.phone
    if not await app.telegram.start():
        return 1
    # _ensure_authorized запустится по updateAuthorizationState; подождём
    for _ in range(600):
        await asyncio.sleep(1.0)
        if app.telegram.authorized:
            print("\n✅ Готово: аккаунт привязан. Теперь `kuni run`")
            app.telegram.stop()
            return 0
    print("не дождались авторизации — попробуй ещё раз")
    app.telegram.stop()
    return 1


async def cmd_diagnose(args: argparse.Namespace) -> int:
    """Проверка всего железа и сервисов до запуска."""
    setup_logging()
    root = project_root()
    cfg_path = root / "config.toml"
    print("persona-runtime diagnose")
    print("=" * 46)
    ok_all = True

    def check(name: str, ok: bool, detail: str = "") -> None:
        nonlocal ok_all
        ok_all = ok_all and ok
        mark = "✅" if ok else "❌"
        print(f"{mark} {name:24} {detail}")

    # конфиг
    if not cfg_path.exists():
        print(f"⚠️  config.toml не найден — создаю из примера, заполни и повтори")
        write_default_config(cfg_path)
        return 1
    from .core.config import load_config
    cfg = load_config(cfg_path)
    check("config.toml", True, str(cfg_path))
    check("api_id/api_hash", bool(cfg.general.telegram_api_id and cfg.general.telegram_api_hash),
          "заполнены" if cfg.general.telegram_api_id else "ПУСТО (my.telegram.org)")

    # tdjson
    from .telegram.tdjson import find_tdjson
    lib = find_tdjson(cfg.telegram.tdjson_path)
    check("tdjson", lib is not None, lib or "не найден (см. README: установка tdlib)")

    # llm
    import httpx
    async with httpx.AsyncClient(timeout=8.0) as http:
        try:
            resp = await http.get(cfg.llm.base_url.rstrip("/") + "/models",
                                  headers={"Authorization": f"Bearer {cfg.llm.api_key}"})
            models = [m.get("id") for m in resp.json().get("data", [])]
            has_model = cfg.llm.model in models
            check("LLM-эндпоинт", True, f"{cfg.llm.base_url} ({len(models)} моделей)")
            check("модель в конфиге", has_model,
                  cfg.llm.model if has_model else f"нет {cfg.llm.model}; есть: {models[:5]}")
        except Exception as e:
            check("LLM-эндпоинт", False, f"{cfg.llm.base_url} недоступен: {type(e).__name__}")

        # эмбеддинги
        try:
            resp = await http.post(
                cfg.llm.base_url.rstrip("/") + "/embeddings",
                headers={"Authorization": f"Bearer {cfg.llm.api_key}"},
                json={"model": cfg.llm.embed_model, "input": ["проверка"]},)
            dim = len(resp.json()["data"][0]["embedding"])
            check("эмбеддинги", True, f"{cfg.llm.embed_model} (dim={dim})")
        except Exception as e:
            check("эмбеддинги", False, f"{cfg.llm.embed_model}: {type(e).__name__}")

        # stable diffusion
        try:
            await http.get(cfg.images.url.rstrip("/") + "/sdapi/v1/options", timeout=5.0)
            check("Stable Diffusion", True, cfg.images.url)
        except Exception:
            check("Stable Diffusion", False, f"{cfg.images.url} недоступен (опционально)")

        # whisper
        if cfg.voice.whisper_url:
            try:
                await http.get(cfg.voice.whisper_url.rsplit("/", 1)[0] + "/models", timeout=5.0)
                check("Whisper", True, cfg.voice.whisper_url)
            except Exception:
                check("Whisper", False, "недоступен (войсы не расшифруются)")
        else:
            print("➖ Whisper не настроен (необязательно)")

    # промпты
    prompts_dir = root / cfg.general.prompts_dir
    required = ["character_base", "character_appearance", "emotions", "diary_consolidation"]
    for name in required:
        check(f"промпт {name}.md", (prompts_dir / f"{name}.md").exists())

    print("=" * 46)
    print("всё готово ✅" if ok_all else "есть проблемы ❌ — см. выше")
    return 0 if ok_all else 1


async def cmd_send(args: argparse.Namespace) -> int:
    """Ручная отправка сообщения от её имени: kuni send @user|chat_id текст"""
    from .app import KuniApp
    setup_logging()
    app = KuniApp(project_root() / "config.toml", interactive=False)
    if not await app.telegram.start():
        return 1
    try:
        target = args.to
        chat_id: int | None = None
        if target.lstrip("-").isdigit():
            chat_id = int(target)
        else:
            chat_id = await app.telegram.search_public_chat(target)
        if chat_id is None:
            print(f"чат {target} не найден")
            return 1
        message_id = await app.telegram.send_text(chat_id, args.text)
        print(f"отправлено в {chat_id} (message_id={message_id})")
        return 0
    finally:
        app.telegram.stop()


def cmd_init(args: argparse.Namespace) -> int:
    cfg_path = project_root() / "config.toml"
    if cfg_path.exists():
        print("config.toml уже существует")
        return 0
    write_default_config(cfg_path)
    print(f"config.toml создан: {cfg_path}")
    return 0


async def cmd_chat(args: argparse.Namespace) -> int:
    from .console.chat import run_chat
    return await run_chat(args)


def cmd_rollback(args: argparse.Namespace) -> int:
    """Откат её самоправок: kuni rollback --last 3 (0 = все)."""
    root = project_root()
    edits_dir = root / "data" / "self_edits"
    backups = sorted(edits_dir.glob("*.bak")) if edits_dir.exists() else []
    if not backups:
        print("бэкапов нет — правок ещё не было")
        return 0
    count = args.last if args.last > 0 else len(backups)
    for backup in backups[-count:]:
        # имя: <stamp>__<путь__с__двойными>__.bak -> исходный путь
        stamp = backup.name.split("__", 1)[0]
        rel = backup.name[len(stamp) + 2:-4]
        target = root / rel.replace("__", "/")
        if target.suffix == ".bak" or not target.parent.exists():
            continue
        original_missing = backup.stat().st_size == 0 and not target.exists()
        if original_missing:
            print(f"пропуск (файла и не было): {rel}")
            continue
        target.write_text(backup.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"откачено: {rel} <- {backup.name}")
    print("готово. перезапусти `kuni run`, если она работает")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="kuni", description="persona-runtime — Telegram-персонаж")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command")

    p_run = sub.add_parser("run", help="основной режим")
    p_run.add_argument("-v", "--verbose", action="store_true")
    p_run.add_argument("--brain", choices=["bridge", "openai"], default="openai",
                       help="bridge = модель отвечаешь ты, файлами (data/devbrain)")
    p_run.add_argument("--bridge-dir", default="data/devbrain")
    p_run.set_defaults(func=cmd_run)

    p_login = sub.add_parser("login", help="интерактивный вход в Telegram")
    p_login.add_argument("--phone", default="", help="номер телефона (+7...), чтобы не спрашивал")
    p_login.set_defaults(func=cmd_login)

    sub.add_parser("diagnose", help="проверка компонентов").set_defaults(func=cmd_diagnose)

    p_chat = sub.add_parser("chat", help="чат в консоли через весь конвейер")
    p_chat.add_argument("--brain", choices=["bridge", "openai"], default="openai",
                        help="bridge = модель отвечаешь ты, файлами (data/devbrain)")
    p_chat.add_argument("--bridge-dir", default="data/devbrain")
    p_chat.add_argument("--once", default=None, metavar="TEXT",
                        help="одно сообщение через конвейер и выход")
    p_chat.set_defaults(func=cmd_chat)

    p_send = sub.add_parser("send", help="отправить сообщение от её имени")
    p_send.add_argument("to", help="@username или chat_id")
    p_send.add_argument("text", help="текст сообщения")
    p_send.set_defaults(func=cmd_send)

    p_rb = sub.add_parser("rollback", help="откат её самоправок")
    p_rb.add_argument("--last", type=int, default=1, help="сколько правок откатить (0=все)")
    p_rb.set_defaults(func=cmd_rollback)

    sub.add_parser("init", help="создать config.toml").set_defaults(func=cmd_init)

    args = parser.parse_args()
    if not getattr(args, "command", None):
        parser.print_help()
        return 2
    result = args.func(args)
    if asyncio.iscoroutine(result):
        try:
            return asyncio.run(result)
        except KeyboardInterrupt:
            return 130
    return result


if __name__ == "__main__":
    sys.exit(main())
