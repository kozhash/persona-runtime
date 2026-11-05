"""Единый часовой пояс проекта: фиксированный UTC+5.

Зачем отдельный модуль, а не `time.localtime()`:

`localtime` берёт пояс у операционной системы. У владельца система в
UTC+5, поэтому всё работало «случайно правильно». При переносе на
машину с другим поясом поехало бы всё сразу: ночной режим, час
консолидации памяти, время в дневнике и в мыслях, отметки в промпте.

Здесь пояс задан числом и не зависит ни от ОС, ни от переменных
окружения. Если однажды понадобится переехать — правится одна строка.

Важно про epoch: `time.time()` всегда UTC-абсолютен, его не трогаем.
Меняется только то, как момент показывается человеку и как
«локальные» часы переводятся в epoch.
"""
from __future__ import annotations

import calendar
import time

#: Смещение в часах от UTC. У владельца и у персонажа одно и то же время.
TZ_OFFSET_HOURS = 5
TZ_OFFSET_SEC = TZ_OFFSET_HOURS * 3600

#: Человеческое имя пояса — для логов и промпта.
TZ_LABEL = f"UTC+{TZ_OFFSET_HOURS}"


def now() -> float:
    """Текущий момент в epoch. Тот же, что time.time() — просто явно."""
    return time.time()


def localtime(ts: float | None = None) -> time.struct_time:
    """Момент как struct_time в поясе проекта.

    Реализация: сдвигаем epoch на смещение и читаем как UTC. Так не
    зависит от системного пояса вообще.
    """
    stamp = time.time() if ts is None else float(ts)
    return time.gmtime(stamp + TZ_OFFSET_SEC)


def to_epoch(ts: float | None = None) -> float:
    """Обратное к localtime: struct_time пояса проекта -> epoch.

    Нужно там, где мы строим «сегодня в 4:00» и хотим получить
    абсолютный момент.
    """
    st = localtime(ts)
    # calendar.timegm трактует кортеж как UTC, поэтому вычитаем смещение
    return calendar.timegm(st) - TZ_OFFSET_SEC


def strftime(fmt: str, ts: float | None = None) -> str:
    """Форматирование времени в поясе проекта."""
    return time.strftime(fmt, localtime(ts))


def hour(ts: float | None = None) -> int:
    """Час суток (0..23) в поясе проекта."""
    return localtime(ts).tm_hour


def wall_seconds(hour_value: int, *, ts: float | None = None) -> float:
    """Epoch ближайшего момента `hour_value:00` в поясе проекта.

    Если этот час сегодня уже прошёл — берётся завтрашний. Именно так
    планировщик решает, когда следующий раз запускать суточную задачу.
    """
    st = localtime(ts)
    target = calendar.timegm(
        (st.tm_year, st.tm_mon, st.tm_mday, int(hour_value), 0, 0, 0, 0, 0)
    ) - TZ_OFFSET_SEC
    current = time.time() if ts is None else float(ts)
    if target <= current:
        target += 86400
    return target


def parts(ts: float | None = None) -> tuple[int, int, int, int, int]:
    """(год, месяц, день, час, минута) — короткая выжимка для логов."""
    st = localtime(ts)
    return st.tm_year, st.tm_mon, st.tm_mday, st.tm_hour, st.tm_min


def describe() -> str:
    """Одна строка про пояс — для стартового лога."""
    return f"часовой пояс: {TZ_LABEL} (фиксированный, не зависит от ОС)"
