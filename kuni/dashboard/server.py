"""Live-дашборд: состояние персонажа в реальном времени.

DashboardState копит события шины в удобные структуры (текущий чат,
«что набирает», эмоция с причиной, последние LLM-вызовы, сон),
отдаёт снапшот по REST и пушит изменения по WebSocket.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, PlainTextResponse

log = logging.getLogger(__name__)


class DashboardState:
    def __init__(self, bus: Any, diary: Any, working: Any, emotions: Any,
                 store: Any, app_ref: Any = None) -> None:
        self.bus = bus
        self.diary = diary
        self.working = working
        self.emotions = emotions
        self.store = store
        self.app_ref = app_ref     # kuni.app.KuniApp, для статусов модулей

        self.emotion: dict[str, Any] = {}
        self.active_chats: dict[int, dict[str, Any]] = {}
        self.typing: dict[int, dict[str, Any]] = {}
        self.recent_llm: deque[dict[str, Any]] = deque(maxlen=60)
        self.recent_messages: deque[dict[str, Any]] = deque(maxlen=80)
        self.recent_thoughts: deque[dict[str, Any]] = deque(maxlen=30)   # вселённые мысли
        self.recent_reasoning: deque[dict[str, Any]] = deque(maxlen=30)  # размышления модели
        self.recent_tools: deque[dict[str, Any]] = deque(maxlen=40)      # вызванные тулы
        self.terminal: deque[dict[str, Any]] = deque(maxlen=120)         # её консоль
        self.live_log: deque[dict[str, Any]] = deque(maxlen=400)         # всё подряд, для лога
        self.sleep_stats: dict[str, Any] = {"sleeping": False}
        self.human_status: dict[str, Any] = {"online": False}
        self.token_totals: dict[str, int] = {"input": 0, "output": 0}

        bus.subscribe("*", self._on_event)
        # начальное состояние эмоции
        state = getattr(self.emotions, "state", None)
        if state:
            import dataclasses
            self.emotion = dataclasses.asdict(state)
            self.emotion["intensity_pct"] = state.intensity_pct
            self.emotion["emoji"] = state.emoji()

    # ------------------------------------------------------------ события

    def _on_event(self, payload: dict) -> None:
        event = self.bus.history[-1] if self.bus.history else None
        topic = event.topic if event else "?"
        # живой лог: сохраняем ВСЁ, без обрезки — аргументы важны целиком
        try:
            self.live_log.append({
                "ts": time.time(), "topic": topic,
                "payload": json.loads(json.dumps(payload, ensure_ascii=False, default=str)),
            })
        except Exception:
            self.live_log.append({"ts": time.time(), "topic": topic,
                                  "payload": {"_unserializable": str(payload)[:500]}})
        if topic == "emotion.changed":
            self.emotion = dict(payload)
        elif topic == "telegram.message":
            chat_id = payload.get("chat_id", 0)
            self.active_chats.setdefault(chat_id, {"chat_id": chat_id, "title": "", "last": "", "ts": 0})
            chat = self.active_chats[chat_id]
            chat.update({"last": payload.get("text", ""), "ts": time.time(),
                         "title": payload.get("chat_title") or chat.get("title") or str(chat_id),
                         "dir": "in"})
            self.recent_messages.append({**payload, "dir": "in", "ts": time.time()})
        elif topic == "telegram.sent":
            chat_id = payload.get("chat_id", 0)
            self.active_chats.setdefault(chat_id, {"chat_id": chat_id, "title": str(chat_id), "ts": 0})
            self.active_chats[chat_id].update({"last": payload.get("text", ""), "ts": time.time(), "dir": "out"})
            self.recent_messages.append({**payload, "dir": "out", "ts": time.time()})
        elif topic == "telegram.human.typing":
            chat_id = payload.get("chat_id", 0)
            if payload.get("typing"):
                self.typing[chat_id] = {**payload, "since": time.time()}
            else:
                self.typing.pop(chat_id, None)
        elif topic == "telegram.human.online":
            self.human_status["online"] = bool(payload.get("online"))
        elif topic == "telegram.human.read":
            pass
        elif topic == "llm.call":
            self.recent_llm.appendleft({**payload, "ts": time.time()})
            self.token_totals["input"] += payload.get("input_tokens", 0) or 0
            self.token_totals["output"] += payload.get("output_tokens", 0) or 0
        elif topic == "llm.reasoning":
            self.recent_reasoning.appendleft({**payload, "ts": time.time()})
        elif topic == "tool.called":
            self.recent_tools.appendleft({"ts": time.time(), "phase": "call",
                                          **{k: payload.get(k) for k in ("name", "arguments")}})
        elif topic == "tool.result":
            self.recent_tools.appendleft({"ts": time.time(), "phase": "result",
                                          **{k: payload.get(k) for k in ("name", "ok", "output", "artifact")}})
        elif topic == "thought.injected":
            self.recent_thoughts.appendleft({**payload, "ts": time.time()})
        elif topic == "terminal.line":
            self.terminal.append({**payload, "ts": time.time()})
        elif topic == "sleep.started":
            self.sleep_stats = {"sleeping": True}
        elif topic == "sleep.finished":
            self.sleep_stats = {**payload, "sleeping": False}

    # ------------------------------------------------------------ снапшот

    def snapshot(self) -> dict[str, Any]:
        app = self.app_ref
        modules = {}
        if app is not None:
            modules = {
                name: getattr(mod, "status", "?")
                for name, mod in getattr(app, "modules", {}).items()
            }
        try:
            diary_stats = self.diary.stats()
        except Exception:
            diary_stats = {"entries": 0, "embedded": 0}
        emotion_history = self.emotions.history(limit=40) if self.emotions else []
        return {
            "ts": time.time(),
            "character": {
                "name": app.config.current.general.character_name if app else "Kuni",
                "emotion": self.emotion,
                "online": self.human_status.get("online", False),
            },
            "modules": modules,
            "active_chats": sorted(
                self.active_chats.values(), key=lambda c: -(c.get("ts") or 0)
            )[:12],
            "typing": list(self.typing.values()),
            "emotion_history": emotion_history,
            "llm_calls": list(self.recent_llm)[:25],
            "token_totals": self.token_totals,
            "messages": list(self.recent_messages)[-40:],
            "sleep": self.sleep_stats,
            "diary": diary_stats,
            "working_memory": self.working.content() if self.working else "",
            "thoughts": list(self.recent_thoughts),
            "reasoning": list(self.recent_reasoning)[:10],
            "tools": list(self.recent_tools)[:20],
            "log": list(self.live_log)[-120:],
            "mind_stream": list(getattr(getattr(self.app_ref, "proactivity", None),
                                        "stream", []))[:10],
            "desires": list(getattr(getattr(self.app_ref, "proactivity", None),
                                    "desires", [])),
            "terminal": list(self.terminal)[-80:],
            "assistant": {
                "mode": getattr(getattr(self.app_ref, "assistant", None), "mode",
                                lambda: "off")(),
                "tasks": getattr(getattr(self.app_ref, "assistant", None),
                                 "snapshot", lambda: [])(),
            },
        }


_LOCAL_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", ""})
_COOKIE_NAME = "kuni_dash"


def _is_local(host: str | None) -> bool:
    """Запрос с этой же машины? Тогда токен не нужен."""
    return (host or "") in _LOCAL_HOSTS


def _client_token(request: Request) -> str:
    """Токен из заголовка, query или cookie — что найдётся.

    27.10: токеном может быть номер телефона вида +77006632135. В
    query-строке "+" декодируется как пробел, поэтому пробелы
    возвращаем обратно в плюсы — иначе токен с плюсом не совпадёт.
    """
    raw = (request.headers.get("x-dashboard-token")
           or request.query_params.get("t")
           or request.cookies.get(_COOKIE_NAME) or "")
    return raw.replace(" ", "+")


_LOGIN_HTML = """<!doctype html><meta charset="utf-8">
<title>kuni dashboard</title>
<style>body{font-family:system-ui;background:#111;color:#eee;display:grid;
place-items:center;height:100vh;margin:0}form{text-align:center}
input{padding:10px;font-size:16px;border-radius:8px;border:1px solid #444;
background:#222;color:#eee;width:280px}h1{font-weight:500;font-size:20px}
button{margin-top:12px;padding:10px 20px;border-radius:8px;border:0;
background:#3b6;color:#111;font-size:16px;cursor:pointer}</style>
<form method="get" onsubmit="location='/?t='+encodeURIComponent(this.t.value);return false">
<h1>kuni dashboard</h1>
<p>нужен токен доступа</p>
<input name="t" placeholder="токен" autofocus>
<br><button>войти</button></form>"""


def create_dashboard_app(state: DashboardState, static_dir: Path,
                         token: str = "") -> FastAPI:
    app = FastAPI(title="kuni dashboard", docs_url=None, redoc_url=None)
    clients: set[WebSocket] = set()

    @app.middleware("http")
    async def _guard(request: Request, call_next):
        """Пускать без токена только с этой машины.

        27.10: дашборд открыли в локальную сеть, а у него нет авторизации
        и есть POST /api/config (перезаписывает config.toml) и
        /api/assistant/run (запускает задачи). Без этого шлюза любой
        в сети мог бы править конфиг.
        """
        client_host = request.client.host if request.client else ""
        if _is_local(client_host) or not token:
            return await call_next(request)

        if _client_token(request) != token:
            # токен в query — запомним в cookie, чтобы не таскать в URL
            return PlainTextResponse("нужен токен доступа", status_code=401)

        response = await call_next(request)
        if request.query_params.get("t"):
            response.set_cookie(_COOKIE_NAME, token, httponly=True,
                                samesite="lax", max_age=30 * 24 * 3600)
        return response

    async def broadcast(topic: str, payload: dict) -> None:
        message = json.dumps({"topic": topic, "payload": payload}, ensure_ascii=False, default=str)
        dead: list[WebSocket] = []
        for ws in clients:
            try:
                await ws.send_text(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            clients.discard(ws)

    def _bridge(bus_payload: dict) -> None:
        event = state.bus.history[-1] if state.bus.history else None
        if event is None or not clients:
            return
        asyncio.ensure_future(broadcast(event.topic, event.payload))

    state.bus.subscribe("*", _bridge)

    @app.get("/")
    async def index() -> HTMLResponse:
        html = (static_dir / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(html)

    @app.get("/pro")
    async def index_pro() -> HTMLResponse:
        html = (static_dir / "index-pro.html").read_text(encoding="utf-8")
        return HTMLResponse(html)

    @app.get("/api/state")
    async def api_state() -> dict:
        return state.snapshot()

    @app.get("/api/history")
    async def api_history(chat_id: int, limit: int = 50) -> list[dict]:
        """История чата из tdlib (то, что ещё живо на аккаунте)."""
        app = state.app_ref
        if app is None:
            return []
        try:
            messages = await app.telegram.get_chat_history(chat_id, limit=limit)
        except Exception as e:
            return [{"error": str(e)}]
        out = []
        for m in messages:
            content = m.get("content", {})
            ctype = content.get("@type", "")
            text = ""
            if ctype == "messageText":
                text = (content.get("text") or {}).get("text", "")
            elif ctype.startswith("message"):
                text = f"[{ctype.replace('message', '').lower()}]"
            sender = "out" if m.get("is_outgoing") else "in"
            out.append({
                "ts": m.get("date", 0),
                "dir": sender,
                "sender_id": (m.get("sender_id") or {}).get("user_id"),
                "text": text[:500],
                "id": m.get("id"),
            })
        return out

    @app.post("/api/think")
    async def api_think(request: Request) -> dict:
        """Вселить мысль: она примет её за собственную в следующем ответе."""
        body = await request.json()
        text = str(body.get("text") or "").strip()
        if not text:
            return {"ok": False, "error": "пустая мысль"}
        app = state.app_ref
        if app is None:
            return {"ok": False, "error": "приложение недоступно"}
        try:
            thought = app.inject_thought(text)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "thought": thought}

    @app.get("/api/diary")
    async def api_diary(q: str = "", limit: int = 30) -> list[dict]:
        if q:
            return [e.to_dict() for e in state.store.search_text(q, limit=limit)]
        return [e.to_dict() for e in state.store.recent(limit=limit)]

    @app.post("/api/proactive")
    async def api_proactive(request: Request) -> dict:
        """Она пишет человеку первой (без входящего сообщения)."""
        import asyncio as _asyncio
        body = await request.json()
        chat_id = int(body.get("chat_id") or 0)
        brief = str(body.get("text") or "").strip()
        app = state.app_ref
        if app is None or not chat_id:
            return {"ok": False, "error": "нужен chat_id и живое приложение"}
        result = await app.proactive_send(chat_id, brief, source="dashboard")
        return result

    @app.get("/api/config")
    async def api_config() -> dict:
        from ..core.config import config_to_dict
        app = state.app_ref
        if app is None:
            return {"ok": False, "error": "приложение недоступно"}
        return {"ok": True, "config": config_to_dict(app.config.current)}

    @app.post("/api/config")
    async def api_config_save(request: Request) -> dict:
        """Полное сохранение конфига из веб-редактора (валидация -> config.toml -> hot-reload)."""
        from ..core.config import validate_config_dict
        app = state.app_ref
        if app is None:
            return {"ok": False, "error": "приложение недоступно"}
        body = await request.json()
        data = body.get("config") or body
        if not isinstance(data, dict) or not data:
            return {"ok": False, "error": "пустой конфиг"}
        cfg, text = validate_config_dict(data)
        if cfg is None:
            return {"ok": False, "error": f"конфиг не прошёл валидацию: {text[:200]}"}
        app.config.path.write_text(text, encoding="utf-8")
        app.config.maybe_reload(force=True)
        return {"ok": True, "saved": True}

    @app.get("/api/assistant")
    async def api_assistant_get() -> dict:
        app = state.app_ref
        if app is None:
            return {"ok": False}
        cfg = app.config.current.assistant
        return {"ok": True, "mode": app.assistant.mode(),
                "channel_id": cfg.channel_id,
                "tasks": app.assistant.snapshot()}

    @app.post("/api/assistant")
    async def api_assistant_set(request: Request) -> dict:
        """Переключить режим: off | companion | assistant."""
        app = state.app_ref
        if app is None:
            return {"ok": False, "error": "приложение недоступно"}
        body = await request.json()
        try:
            app.assistant.set_mode(str(body.get("mode") or ""))
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "mode": app.assistant.mode()}

    @app.get("/api/background")
    async def api_background_get() -> dict:
        app = state.app_ref
        if app is None:
            return {"ok": False}
        return {"ok": True, **app.background.snapshot(),
                "episodes": app.diary_store.count()}

    @app.post("/api/background")
    async def api_background_set(request: Request) -> dict:
        """Переключить режим памяти: sleep | background."""
        app = state.app_ref
        if app is None:
            return {"ok": False, "error": "приложение недоступно"}
        body = await request.json()
        mode = str(body.get("mode") or "").strip()
        try:
            applied = app.background.set_mode(mode)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "mode": applied}

    @app.post("/api/assistant/run")
    async def api_assistant_run() -> dict:
        """Исполнить все наступившие задачи прямо сейчас."""
        app = state.app_ref
        if app is None:
            return {"ok": False}
        await app.assistant.run_due()
        return {"ok": True}

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket) -> None:
        # middleware на websocket не действует — проверяем руками
        if token:
            client_host = ws.client.host if ws.client else ""
            given = (ws.query_params.get("t")
                     or ws.cookies.get(_COOKIE_NAME)
                     or ws.headers.get("x-dashboard-token") or "").replace(" ", "+")
            if not _is_local(client_host) and given != token:
                await ws.close(code=1008)   # policy violation
                return
        await ws.accept()
        clients.add(ws)
        try:
            await ws.send_text(json.dumps(
                {"topic": "snapshot", "payload": state.snapshot()}, ensure_ascii=False, default=str))
            while True:
                # пинг-понг: клиент шлёт "ping", мы держим соединение
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            clients.discard(ws)

    return app
