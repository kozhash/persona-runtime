"""Запуск kuni отдельным процессом, не привязанным к сессии агента.

Проблема: `exec` в фоне привязывает процесс к своей сессии. Когда я
чищу сессии или завершается обёртка, kuni умирает вместе с ней.

Решение: создаём процесс через WMI/`subprocess` с полным отсоединением,
чтобы он жил сам по себе. PID пишем в data/kuni.pid для проверки.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
LOG = ROOT / "data" / "run_latest.log"
PID_FILE = ROOT / "data" / "kuni.pid"

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000


def main() -> int:
    if not PYTHON.exists():
        print("нет интерпретатора:", PYTHON)
        return 1

    LOG.parent.mkdir(parents=True, exist_ok=True)
    out = open(LOG, "a", encoding="utf-8", errors="replace")

    flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    proc = subprocess.Popen(
        [str(PYTHON), "-m", "kuni", "run"],
        cwd=str(ROOT),
        stdout=out,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        creationflags=flags,
        close_fds=True,
    )
    PID_FILE.write_text(str(proc.pid), encoding="utf-8")
    print("запущен detached pid:", proc.pid)

    # ждём подъёма портов
    import socket
    for _ in range(40):
        time.sleep(0.5)
        ready = 0
        for port in (10434, 10435, 9464):
            with socket.socket() as s:
                s.settimeout(0.2)
                if s.connect_ex(("127.0.0.1", port)) == 0:
                    ready += 1
        if ready == 3:
            print("порты подняты: 10434, 10435, 9464")
            return 0
        if proc.poll() is not None:
            print("процесс завершился, код:", proc.returncode)
            return 1
    print("порты не поднялись за 20с (процесс жив:", proc.poll() is None, ")")
    return 1


if __name__ == "__main__":
    sys.exit(main())
