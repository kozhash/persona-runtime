"""Запуск kuni через WMI: процесс не принадлежит дереву сессии агента.

Почему WMI: `subprocess.Popen` и `Start-Process` создают потомков, которых
OpenClaw убивает при завершении сессии (Job Object). Процесс, созданный
через Win32_Process.Create, принадлежит WMI-сервису и живёт сам.

ВАЖНО про PID: WMI запускает `cmd.exe /c ...`, который порождает цепочку
cmd -> python-launcher -> python. В `data/kuni.pid` надо писать НАСТОЯЩИЙ
python-процесс, иначе остановка по этому файлу убьёт только обёртку,
а kuni продолжит работать. Поэтому после старта ищем потомка по дереву.

Запуск:
    python scripts/start_wmi.py
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
LOG = ROOT / "data" / "run_latest.log"
PID_FILE = ROOT / "data" / "kuni.pid"


def ports_up() -> int:
    ready = 0
    for port in (10434, 10435, 9464):
        with socket.socket() as s:
            s.settimeout(0.3)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                ready += 1
    return ready


def _ps(command: str) -> str:
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True, text=True, timeout=60,
    )
    return (result.stdout or "").strip()


def _pid_alive(pid: str) -> bool:
    """Жив ли процесс с таким PID."""
    if not pid.isdigit():
        return False
    out = _ps(f"Get-Process -Id {pid} -ErrorAction SilentlyContinue | "
              "Select-Object -First 1 | ForEach-Object { $_.Id }")
    return out.strip() == pid


def find_real_python(launcher_pid: str) -> str:
    """Найти настоящий python-процесс в дереве, начиная с launcher_pid.

    Цепочка бывает: cmd -> python(launcher) -> python(real).
    Берём самого младшего потомка, чья командная строка содержит `kuni run`.
    """
    if not launcher_pid.isdigit():
        return ""
    query = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.CommandLine -like '*-m kuni run*' } | "
        "Select-Object ProcessId,ParentProcessId | ConvertTo-Json -Compress"
    )
    raw = _ps(query)
    if not raw:
        return ""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return ""
    if isinstance(data, dict):
        data = [data]
    procs = {str(p["ProcessId"]): str(p.get("ParentProcessId") or "")
             for p in data if p}
    if not procs:
        return ""

    # ищем потомков launcher_pid на любую глубину
    children = {pid: parent for pid, parent in procs.items()}
    frontier = [launcher_pid]
    visited: list[str] = []
    while frontier:
        current = frontier.pop()
        for pid, parent in children.items():
            if parent == current and pid not in visited:
                visited.append(pid)
                frontier.append(pid)
    # самый глубокий = реальный процесс
    return visited[-1] if visited else (list(procs)[-1] if procs else "")


def main() -> int:
    if not PYTHON.exists():
        print("нет интерпретатора:", PYTHON)
        return 1

    LOG.parent.mkdir(parents=True, exist_ok=True)
    command = (
        f'cmd.exe /c cd /d "{ROOT}" && '
        f'"{PYTHON}" -m kuni run >> "{LOG}" 2>&1'
    )
    ps = (
        "$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
        f"-Arguments @{{CommandLine='{command}'}}; "
        "Write-Output $r.ProcessId"
    )
    out = _ps(ps)
    launcher = (out.splitlines()[-1].strip() if out else "")
    print("WMI launcher pid:", launcher or "(пусто)")

    for i in range(60):
        time.sleep(0.5)
        if ports_up() == 3:
            real = find_real_python(launcher)
            # проверяем, что найденный PID действительно жив: второй экземпляр
            # упирается в singleton, умирает, но порты уже подняты старым —
            # записывать его PID нельзя
            if real and _pid_alive(real):
                PID_FILE.write_text(real, encoding="utf-8")
                print(f"порты подняты за {(i + 1) * 0.5:.1f} с")
                print("launcher pid:", launcher, "-> реальный pid:", real)
                print("записан в", PID_FILE)
            else:
                print("порты подняты, но наш экземпляр не выжил (singleton занят)")
                print("работает уже существующий процесс — pid в kuni.pid не меняю")
            return 0
    print("порты не поднялись за 30 с (готово: %d/3)" % ports_up())
    return 1


if __name__ == "__main__":
    sys.exit(main())
