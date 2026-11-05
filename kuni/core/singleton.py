"""Защита от второго экземпляра: один процесс на проект.

Зачем: в логах видно, как два `kuni run` работали одновременно. Оба
poll-или доску задач, оба отвечали в чат и оба отчитывались об одной
задаче. Пользователь получал два разных ответа на одно сообщение.

Механика: lock-файл в data/kuni.lock с PID внутри. Открытие эксклюзивное
(O_CREAT|O_EXCL). Если файл есть, проверяем, жив ли процесс из него:
  * жив  -> отказ, второй экземпляр не стартует;
  * мёртв -> файл устарел, перезаписываем.

Работает на Windows и POSIX без внешних зависимостей.
"""
from __future__ import annotations

import atexit
import logging
import os
import sys
from pathlib import Path

log = logging.getLogger(__name__)


def _pid_alive(pid: int) -> bool:
    """Проверить, существует ли процесс с таким PID."""
    if pid <= 0:
        return False
    if os.name == "nt":
        # os.kill(pid, 0) на Windows не проверяет существование,
        # поэтому спрашиваем систему напрямую.
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            if not ok:
                return False
            STILL_ACTIVE = 259
            return code.value == STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class SingleInstance:
    """Контекстный менеджер: держит lock на время работы процесса."""

    def __init__(self, lock_path: Path) -> None:
        self.path = Path(lock_path)
        self.held = False
        self._created = False

    def acquire(self) -> bool:
        """True — блокировка взята; False — уже работает другой экземпляр."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if self._clear_stale():
                    continue          # устаревший lock убрали — пробуем ещё раз
                return False
            else:
                with os.fdopen(fd, "w", encoding="utf-8") as fp:
                    fp.write(str(os.getpid()))
                self.held = True
                self._created = True
                atexit.register(self.release)
                log.info("lock взят: %s (pid %s)", self.path, os.getpid())
                return True
        return False

    def _clear_stale(self) -> bool:
        """Убрать lock мёртвого процесса. True — можно пробовать снова."""
        try:
            content = self.path.read_text(encoding="utf-8").strip()
            pid = int(content) if content else 0
        except (OSError, ValueError):
            pid = 0
        if pid and _pid_alive(pid):
            log.error("уже запущен другой экземпляр (pid %s): %s", pid, self.path)
            return False
        log.warning("устаревший lock (pid %s) — перезаписываю", pid or "?")
        try:
            self.path.unlink()
        except OSError:
            pass
        return True

    def release(self) -> None:
        if self._created and self.held:
            try:
                self.path.unlink()
            except OSError:
                pass
        self.held = False
        self._created = False

    def __enter__(self) -> "SingleInstance":
        if not self.acquire():
            raise RuntimeError(
                "kuni уже запущена. Второй экземпляр получил бы те же сообщения "
                "и ответил бы дважды. Останови первый процесс или удали "
                f"{self.path}, если он остался от упавшего запуска."
            )
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


def lock_for(data_dir: Path) -> SingleInstance:
    return SingleInstance(Path(data_dir) / "kuni.lock")
