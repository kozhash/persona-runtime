"""Мост к настоящему TDLib через tdjson (тот же движок, что в оригинале).

Паттерн стандартный для tdjson-биндингов (как в официальном примере
tdlib, Apache-2.0): грузим shared library, дёргаем td_json_client_create /
td_json_client_send / td_json_client_receive / td_json_client_execute.

Здесь: блокирующий receive крутится в отдельном потоке и переносит
каждое событие в asyncio через loop.call_soon_threadsafe; запросы
коррелируются с ответами по @extra через future.
"""
from __future__ import annotations

import asyncio
import ctypes
import ctypes.util
import json
import logging
import os
import threading
import uuid
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)


def find_tdjson(explicit: str = "") -> str | None:
    """Ищем tdjson: явный путь -> env KUNI_TDJSON_PATH -> find_library -> типовые имена."""
    candidates: list[str] = []
    if explicit:
        candidates.append(explicit)
    env = os.environ.get("KUNI_TDJSON_PATH", "")
    if env:
        candidates.append(env)
    found = ctypes.util.find_library("tdjson")
    if found:
        candidates.append(found)
    candidates += [
        "tdjson.dll", "libtdjson.so", "libtdjson.dylib",
        str(Path.home() / "td" / "build" / "libtdjson.so"),
        "C:/td/build/Release/tdjson.dll",
        "C:/td/build/tdjson.dll",
        "/usr/local/lib/libtdjson.so",
        "/usr/lib/libtdjson.so",
    ]
    # pip install tdjson приносит готовую сборку прямо в site-packages
    try:
        import glob as _glob
        import site as _site
        for sp in getattr(_site, "getsitepackages", lambda: [])():
            candidates += _glob.glob(str(Path(sp) / "tdjson.libs" / "tdjson*.dll"))
    except Exception:
        pass
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    # голое имя — вдруг лежит в PATH поиска DLL
    for candidate in ("tdjson.dll", "libtdjson.so", "libtdjson.dylib"):
        try:
            lib = ctypes.CDLL(candidate)
            if hasattr(lib, "td_json_client_create"):
                return candidate
        except OSError:
            continue
    return None


class TdError(Exception):
    def __init__(self, code: int | None, message: str | None) -> None:
        self.code = code or 0
        self.message = message or "unknown tdlib error"
        super().__init__(f"tdlib {self.code}: {self.message}")


class TdJson:
    """Асинхронная обёртка над одним tdjson-клиентом.

    bind_loop(loop) запускает поток приёма. request() шлёт TL-объект
    (dict) и ждёт ответ; обновления (update*) уходят в on_update.
    """

    def __init__(self, library_path: str, on_update: Callable[[dict], None],
                 verbosity: int = 2) -> None:
        self._on_update = on_update
        self._pending: dict[str, asyncio.Future] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stopping = False
        self._thread: threading.Thread | None = None

        # зависимости (openssl, zlib, msvcp) лежат рядом — добавляем их в поиск
        dll_dir = str(Path(library_path).parent)
        try:
            os.add_dll_directory(dll_dir)
        except (AttributeError, OSError):
            pass
        self._lib = ctypes.CDLL(library_path)
        self._lib.td_json_client_create.restype = ctypes.c_void_p
        self._lib.td_json_client_create.argtypes = []
        self._lib.td_json_client_send.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        self._lib.td_json_client_send.restype = None
        self._lib.td_json_client_receive.argtypes = [ctypes.c_void_p, ctypes.c_double]
        self._lib.td_json_client_receive.restype = ctypes.c_char_p
        self._lib.td_json_client_execute.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        self._lib.td_json_client_execute.restype = ctypes.c_char_p
        self._lib.td_json_client_destroy.argtypes = [ctypes.c_void_p]
        self._lib.td_json_client_destroy.restype = None

        self._client: Any = self._lib.td_json_client_create()
        self.execute({"@type": "setLogVerbosityLevel", "new_verbosity_level": verbosity})
        log.info("tdjson загружен: %s", library_path)

    # ------------------------------------------------------------ lifecycle

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Привязать asyncio-loop и стартовать поток приёма."""
        if self._thread is not None:
            return
        self._loop = loop
        self._thread = threading.Thread(target=self._receive_loop, name="tdjson-receive", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stopping = True
        client, self._client = self._client, None
        if client is not None:
            try:
                self._lib.td_json_client_destroy(client)
            except Exception:
                pass

    # --------------------------------------------------------------- приём

    def _receive_loop(self) -> None:
        while not self._stopping:
            try:
                raw = self._lib.td_json_client_receive(self._client, 1.0)
            except Exception:
                if self._stopping:
                    return
                log.exception("tdjson receive упал")
                return
            if not raw:
                continue
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                log.warning("tdjson прислал не-JSON, пропускаю")
                continue
            loop = self._loop
            if loop is not None:
                loop.call_soon_threadsafe(self._dispatch, data)

    def _dispatch(self, data: dict) -> None:
        extra = data.get("@extra")
        if extra is not None and str(extra) in self._pending:
            future = self._pending.pop(str(extra))
            if not future.done():
                if data.get("@type") == "error":
                    future.set_exception(TdError(data.get("code"), data.get("message")))
                else:
                    future.set_result(data)
            return
        try:
            self._on_update(data)
        except Exception:
            log.exception("обработчик update упал: %s", data.get("@type"))

    # ------------------------------------------------------------ отправка

    def send(self, data: dict) -> None:
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self._lib.td_json_client_send(self._client, payload)

    def execute(self, data: dict) -> dict | None:
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        raw = self._lib.td_json_client_execute(self._client, payload)
        return json.loads(raw.decode("utf-8")) if raw else None

    async def request(self, data: dict, timeout: float = 60.0) -> dict:
        if self._loop is None:
            raise RuntimeError("tdjson: сначала bind_loop(asyncio.get_running_loop())")
        extra = uuid.uuid4().hex
        data = dict(data)
        data["@extra"] = extra
        future: asyncio.Future = self._loop.create_future()
        self._pending[extra] = future
        self.send(data)
        try:
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            self._pending.pop(extra, None)
            raise TdError(-1, f"таймаут запроса {data.get('@type')}") from None
