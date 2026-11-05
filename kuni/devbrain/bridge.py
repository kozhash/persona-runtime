"""FileBridgeLLM — LLM-«провода на столе».

Протокол (один файл = один вызов):
  inbox/<id>.json   {"id", "ts", "function_label", "chat_label",
                     "messages": [...], "tools": [...]}
  outbox/<id>.json  {"content": "текст ответа"}
                  или {"tool_calls": [{"name": "...", "arguments": {...}}]}
  outbox/<id>.err   {"error": "текст"}  — ответить ошибкой

Пока outbox-файла нет — конвейер честно ждёт (poll). Токены оцениваются
как len/4, чтобы метрики и дашборд жили как с настоящей моделью.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Sequence

from ..llm.client import ChatResult, LLMError, ToolCall, extract_json

log = logging.getLogger(__name__)

EMBED_DIM = 64


class FileBridgeLLM:
    def __init__(self, bridge_dir: Path, poll_interval: float = 0.4,
                 timeout: float = 600.0) -> None:
        self.dir = Path(bridge_dir)
        (self.dir / "inbox").mkdir(parents=True, exist_ok=True)
        (self.dir / "outbox").mkdir(parents=True, exist_ok=True)
        self.poll_interval = poll_interval
        self.timeout = timeout
        # app._reload_config присваивает их — обязаны существовать
        self.base_url = "file-bridge://" + str(self.dir)
        self.model = "glm-as-kuni"
        self.embed_model = "hash-embed"
        self.bus: Any = None

    def bind_bus(self, bus: Any) -> None:
        self.bus = bus

    # ------------------------------------------------------------ lifecycle

    async def healthy(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None

    async def list_models(self) -> list[str]:
        return [self.model]

    # ---------------------------------------------------------------- вызовы

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        tools: Sequence[dict[str, Any]] = (),
        temperature: float = 0.8,
        chat_label: str = "",
        function_label: str = "chat",
        max_tokens: int | None = None,
    ) -> ChatResult:
        req_id = uuid.uuid4().hex[:10]
        request = {
            "id": req_id,
            "ts": time.time(),
            "function_label": function_label,
            "chat_label": chat_label or "-",
            "messages": messages,
            "tools": list(tools) or [],
            "hint": "ответь outbox/" + req_id + ".json: "
                    '{"content": "..."} или {"tool_calls": [{"name": "...", "arguments": {}}]}',
        }
        inbox_file = self.dir / "inbox" / f"{req_id}.json"
        outbox_file = self.dir / "outbox" / f"{req_id}.json"
        err_file = self.dir / "outbox" / f"{req_id}.err"
        inbox_file.write_text(json.dumps(request, ensure_ascii=False, indent=1), encoding="utf-8")
        log.info("bridge: запрос %s (%s) ждёт ответа: %s", req_id, function_label, outbox_file)
        print(f"\n[bridge] {function_label}: {inbox_file} — жду ответ в {outbox_file}", flush=True)

        deadline = time.time() + self.timeout
        while time.time() < deadline:
            if err_file.exists():
                raise LLMError(f"bridge: {err_file.read_text(encoding='utf-8')[:200]}")
            if outbox_file.exists():
                break
            await asyncio.sleep(self.poll_interval)
        else:
            raise LLMError(f"bridge: ответ на {req_id} не пришёл за {self.timeout:.0f}с")

        raw = json.loads(outbox_file.read_text(encoding="utf-8"))
        result = ChatResult(
            content=str(raw.get("content") or ""),
            finish_reason="tool_calls" if raw.get("tool_calls") else "stop",
        )
        for tc in raw.get("tool_calls") or []:
            result.tool_calls.append(ToolCall(
                id=uuid.uuid4().hex[:8],
                name=str(tc.get("name") or ""),
                arguments=tc.get("arguments") or {},
            ))
        result.input_tokens = sum(len(str(m.get("content") or "")) for m in messages) // 4
        result.output_tokens = max(1, len(result.content) // 4)
        if self.bus:
            self.bus.emit(
                "llm.call", model=self.model, chat=chat_label or "-",
                function=function_label, input_tokens=result.input_tokens,
                output_tokens=result.output_tokens, cached_input_tokens=0,
            )
        return result

    async def chat_json(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        chat_label: str = "",
        function_label: str = "json",
        temperature: float = 0.2,
    ) -> dict[str, Any]:
        result = await self.chat(
            messages, chat_label=chat_label, function_label=function_label,
            temperature=temperature,
        )
        return extract_json(result.content)

    # ----------------------------------------------------------- эмбеддинги

    async def embed(self, texts: list[str], *, model: str | None = None) -> list[list[float]]:
        return [self._hash_embed(t) for t in texts]

    async def embed_one(self, text: str, *, model: str | None = None) -> list[float]:
        return self._hash_embed(text)

    @staticmethod
    def _hash_embed(text: str) -> list[float]:
        vec = [0.0] * EMBED_DIM
        for ch in text.lower():
            vec[ord(ch) % EMBED_DIM] += 1.0
        return vec
