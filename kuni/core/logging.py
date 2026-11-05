"""Единый формат логов: время — модуль — уровень — сообщение."""
from __future__ import annotations

import logging
import sys


def _force_utf8_stream() -> None:
    """Переключить stdout/stderr на utf-8, чтобы эмодзи не роняли логи.

    На Windows stdout перенаправлен в файл и кодируется cp1251. Любая
    реплика с эмодзи вызывала UnicodeEncodeError внутри logging — в лог
    вместо строки падал трейсбек, и сообщение терялось.
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
                continue
            except (ValueError, OSError):
                pass
        buffer = getattr(stream, "buffer", None)
        if buffer is None:
            continue
        try:
            import io
            setattr(sys, name, io.TextIOWrapper(
                buffer, encoding="utf-8", errors="replace", line_buffering=True))
        except Exception:
            pass


def setup_logging(verbose: bool = False) -> None:
    _force_utf8_stream()
    level = logging.DEBUG if verbose else logging.INFO
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)-28s %(message)s", datefmt="%H:%M:%S"
    ))
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(handler)
    # tdlib-обёртка очень болтлива на DEBUG
    logging.getLogger("kuni.telegram.tdjson").setLevel(logging.INFO)
