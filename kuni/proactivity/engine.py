"""Три движка проктивности — включаются по отдельности в [proactivity].

A. Импульс-петля: раз в N минут фоновая модель видит настроение/чаты/дневник
   и решает — ничего не делать (чаще всего) / написать / сфоткаться и скинуть /
   поставить реакцию на последнее сообщение.
B. Поток мыслей: внутренний монолог раз в N минут; мысли копят ЖЕЛАНИЯ
   (urge 0-100), желание перешло порог — она действует.
C. Потребности по людям: у каждого чата растёт «скучаю», общение его сбивает;
   перешёл порог — она пишет этому человеку сама.

Общие ворота: дневной лимит, кулдаун между инициативами, ночные часы,
только авторизованный телеграм. Все события летят на шину (дашборд).
"""
from __future__ import annotations

import json
import logging
import random
import time
from collections import deque
from datetime import date
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

ALLOWED_REACTIONS = ["👍", "❤️", "🔥", "🤔", "👀", "😁", "🥱", "💔", "🤡", "😮"]

DEFAULT_IMPULSE_PROMPT = (
    "You are the spontaneous impulse layer. Given mood, time, chat list and diary, decide "
    "ONE action. Strict JSON: {\"action\":\"none|write|photo|react\",\"chat_id\":<only from list>,"
    "\"brief\":\"your impulse in her voice (Russian)\",\"note\":\"why, short\"}. "
    "Most of the time the answer is none — she has her own life."
)
DEFAULT_THOUGHT_PROMPT = (
    "You are her inner monologue. Produce one small thought (Russian, first person) and "
    "optionally desires. Strict JSON: {\"thought\":\"...\",\"new_desires\":[{\"want\":"
    "\"write|photo|react\",\"who\":<chat_id>,\"text\":\"impulse\",\"urge\":0-100}]}"
)
DEFAULT_SELFIE_PROMPT = (
    "Produce a photorealistic selfie plan. Strict JSON: {\"sd_prompt\":\"...\","
    "\"caption\":\"casual russian lowercase caption\"}"
)


class ProactivityEngine:
    def __init__(self, app: Any) -> None:
        self.app = app
        self.bus = app.bus
        self.actions_today = 0
        self._day = date.today()
        self.last_action_ts = 0.0
        self.desires: list[dict[str, Any]] = []
        self.needs: dict[int, dict[str, float]] = {}
        self.stream: deque[dict[str, Any]] = deque(maxlen=40)
        self._unsub = self.bus.subscribe("telegram.*", self._on_exchange)

    # ----------------------------------------------------------- общие ворота

    def _on_exchange(self, payload: dict) -> None:
        """Обмен в чате сбивает «скучаю» и фиксирует время контакта."""
        topic = self.bus.history[-1].topic if self.bus.history else ""
        if topic not in ("telegram.message", "telegram.sent"):
            return
        cid = payload.get("chat_id")
        if not cid:
            return
        need = self.needs.setdefault(cid, {"miss": 0.0, "last_seen": 0.0})
        need["last_seen"] = time.time()
        need["miss"] *= 0.4

    def _gate(self) -> tuple[bool, str]:
        cfg = self.app.config.current.proactivity
        if not cfg.enabled:
            return False, "выключено"
        today = date.today()
        if today != self._day:
            self._day, self.actions_today = today, 0
        if self.actions_today >= cfg.max_actions_per_day:
            return False, "дневной лимит"
        if time.time() - self.last_action_ts < cfg.action_cooldown_minutes * 60:
            return False, "кулдаун"
        tg = self.app.telegram
        if not getattr(tg, "authorized", False):
            return False, "телеграм не готов"
        human = getattr(tg, "human", None)
        if human is not None and human.is_night_now():
            return False, "ночь"
        return True, ""

    async def _act(self, kind: str, chat_id: int, brief: str) -> dict[str, Any] | None:
        ok, why = self._gate()
        if not ok or not chat_id:
            self.bus.emit("proactivity.blocked", kind=kind, reason=why, chat_id=chat_id)
            return None
        if chat_id not in self._known_ids():
            self.bus.emit("proactivity.blocked", kind=kind, reason="незнакомый чат",
                          chat_id=chat_id)
            return None
        try:
            if kind == "write":
                result = await self.app.proactive_send(chat_id, brief, source="proactivity")
            elif kind == "photo":
                result = await self._photo(chat_id, brief)
            elif kind == "react":
                result = await self._react(chat_id)
            else:
                return None
        except Exception:
            log.exception("проктивное действие %s упало", kind)
            return None
        if result and result.get("ok"):
            self.actions_today += 1
            self.last_action_ts = time.time()
            self.bus.emit("proactivity.acted", kind=kind, chat_id=chat_id,
                          brief=brief[:120])
        return result

    # ------------------------------------------------------------- исполнители

    async def _photo(self, chat_id: int, brief: str) -> dict[str, Any]:
        """«Сфоткаться» (SD по её внешности) и скинуть с подписью."""
        app, cfg = self.app, self.app.config.current
        if not cfg.images.enabled:
            return {"ok": False, "error": "stable diffusion выключен"}
        appearance = app.prompts.get("character_appearance", name=cfg.general.character_name)
        instruction = app.prompts.get("selfie", name=cfg.general.character_name)
        try:
            data = await app._chat_resilient_json(
                [
                    {"role": "system", "content": instruction or DEFAULT_SELFIE_PROMPT},
                    {"role": "user", "content":
                        f"Appearance:\n{appearance}\n\nMood:\n{app.emotions.state.describe()}\n\n"
                        f"Impulse: {brief}"},
                ],
                function_label="selfie",
            )
            sd_prompt = str(data.get("sd_prompt") or "").strip()
            caption = str(data.get("caption") or "").strip()
            if not sd_prompt:
                return {"ok": False, "error": "модель не дала sd_prompt"}
            outcome = await app.skills.dispatch("draw", {"prompt": sd_prompt})
            if not outcome.ok or not outcome.artifact_path:
                return {"ok": False, "error": outcome.output[:120]}
            path = Path(outcome.artifact_path)
            if app.telegram.human:
                await app.telegram.human.before_reply(chat_id, len(caption) + 20)
            await app.telegram.send_photo_file(chat_id, path, caption)
            self.bus.emit("telegram.sent", chat_id=chat_id,
                          text=f"[фото] {caption}"[:120])
            return {"ok": True, "sent": caption or "(фото)", "artifact": str(path)}
        except Exception as e:
            return {"ok": False, "error": str(e)[:140]}

    async def _react(self, chat_id: int) -> dict[str, Any]:
        """Реакция-эмодзи на последнее сообщение в чате."""
        try:
            messages = await self.app.telegram.get_chat_history(chat_id, limit=1)
            if not messages:
                return {"ok": False, "error": "чат пуст"}
            emoji = random.choice(ALLOWED_REACTIONS)
            await self.app.telegram.request({
                "@type": "addMessageReaction",
                "chat_id": chat_id,
                "message_id": messages[0].get("id"),
                "reaction_type": {"@type": "reactionTypeEmoji", "emoji": emoji},
            })
            self.bus.emit("proactivity.reacted", chat_id=chat_id, emoji=emoji)
            return {"ok": True, "emoji": emoji}
        except Exception as e:
            return {"ok": False, "error": str(e)[:140]}

    # ------------------------------------------------------- A: импульс-петля

    def _chat_menu(self) -> list[dict[str, Any]]:
        menu = []
        for cid, info in list(self.app.telegram.chats.items())[:12]:
            if info.get("muted"):
                continue
            menu.append({"chat_id": cid, "title": info.get("title") or str(cid),
                         "pinned": bool(info.get("pinned"))})
        return menu

    def _known_ids(self) -> set[int]:
        return set(self.app.telegram.chats.keys())

    async def run_impulse(self) -> None:
        ok, why = self._gate()
        if not ok:
            return
        app, cfg = self.app, self.app.config.current
        chats = self._chat_menu()
        if not chats:
            return
        instruction = app.prompts.get("proactivity_impulse", name=cfg.general.character_name)
        context = {
            "mood": app.emotions.state.describe(),
            "time": time.strftime("%H:%M"),
            "chats": chats,
            "recent_diary": [e.text for e in app.diary_store.recent(3)],
        }
        try:
            data = await app._chat_resilient_json(
                [{"role": "system", "content": instruction or DEFAULT_IMPULSE_PROMPT},
                 {"role": "user", "content": json.dumps(context, ensure_ascii=False)}],
                function_label="impulse",
            )
        except Exception:
            log.warning("импульс не собрался")
            return
        action = str(data.get("action") or "none").lower()
        if action not in ("write", "photo", "react"):
            self.bus.emit("proactivity.idle", note=str(data.get("note") or "")[:100])
            return
        chat_id = int(data.get("chat_id") or 0)
        if chat_id not in self._known_ids():
            self.bus.emit("proactivity.blocked", kind=action, reason="незнакомый чат")
            return
        await self._act(action, chat_id, str(data.get("brief") or ""))

    # ------------------------------------------------------ B: поток мыслей

    async def run_thought(self) -> None:
        app, cfg = self.app, self.app.config.current
        instruction = app.prompts.get("proactivity_thought", name=cfg.general.character_name)
        context = {
            "mood": app.emotions.state.describe(),
            "time": time.strftime("%H:%M"),
            "chats": self._chat_menu(),
            "recent_diary": [e.text for e in app.diary_store.recent(3)],
            "existing_desires": self.desires[-5:],
        }
        try:
            data = await app._chat_resilient_json(
                [{"role": "system", "content": instruction or DEFAULT_THOUGHT_PROMPT},
                 {"role": "user", "content": json.dumps(context, ensure_ascii=False)}],
                function_label="thought",
            )
        except Exception:
            return
        thought = str(data.get("thought") or "").strip()
        if thought:
            self.stream.appendleft({"ts": time.time(), "text": thought[:300]})
            self.bus.emit("thought.stream", text=thought[:300])
        for desire in data.get("new_desires") or []:
            self._add_desire(desire)
        # вооружённое желание -> действие (одно за проход)
        for desire in list(self.desires):
            if desire["urge"] >= cfg.proactivity.desire_threshold:
                kind = desire.get("want") if desire.get("want") in ("write", "photo", "react") else "write"
                result = await self._act(kind, int(desire.get("who") or 0),
                                         str(desire.get("text") or ""))
                if result and result.get("ok"):
                    self.desires.remove(desire)
                break

    def _add_desire(self, data: dict[str, Any]) -> None:
        want = str(data.get("want") or "write")
        who = int(data.get("who") or 0)
        if who not in self._known_ids():
            return
        try:
            urge = float(data.get("urge") or 30)
        except (TypeError, ValueError):
            urge = 30.0
        for existing in self.desires:
            if existing["want"] == want and existing["who"] == who:
                existing["urge"] = min(100.0, existing["urge"] + max(10.0, urge * 0.5))
                if data.get("text"):
                    existing["text"] = str(data["text"])
                self.bus.emit("desire.updated", want=want, who=who,
                              urge=existing["urge"], text=existing["text"][:100])
                return
        entry = {"want": want, "who": who, "text": str(data.get("text") or ""),
                 "urge": min(100.0, urge), "ts": time.time()}
        self.desires.append(entry)
        self.desires = self.desires[-8:]
        self.bus.emit("desire.armed", **{k: entry[k] for k in ("want", "who", "text", "urge")})

    # ------------------------------------------------------ C: потребности

    async def run_needs(self) -> None:
        cfg = self.app.config.current.proactivity
        interval_h = cfg.needs_check_minutes / 60.0
        for cid, info in list(self.app.telegram.chats.items()):
            if info.get("muted"):
                continue
            need = self.needs.setdefault(cid, {"miss": 0.0, "last_seen": 0.0})
            need["miss"] = min(100.0, need["miss"] + cfg.miss_rate_per_hour * interval_h)
            if need["miss"] < cfg.needs_threshold:
                continue
            ok, why = self._gate()
            if not ok:
                continue
            silent_hours = (time.time() - need["last_seen"]) / 3600 if need["last_seen"] else 24.0
            brief = (f"Тебя тянет к этому человеку: вы не общались уже "
                     f"{int(silent_hours)} ч. Хочется написать самой, по своему настроению.")
            result = await self._act("write", cid, brief)
            if result and result.get("ok"):
                need["miss"] *= 0.3
