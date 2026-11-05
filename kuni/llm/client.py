"""Асинхронный OpenAI-совместимый LLM-клиент (Ollama, vLLM, OpenRouter...).

Модуль полностью изолирован: знает только httpx и шину событий
(для метрик llm.call). Ничего не знает про Telegram и дневник.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Sequence

import httpx

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ChatResult:
    content: str = ""
    reasoning: str = ""          # «размышления» модели, если провайдер их отдаёт
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    finish_reason: str = "stop"


class LLMClient:
    def __init__(
        self,
        base_url: str,
        api_key: str = "ollama",
        model: str = "",
        embed_model: str = "",
        bus: Any | None = None,
        timeout: float = 300.0,
        reasoning_effort: str = "minimal",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.embed_model = embed_model
        self.bus = bus
        # 27.10: glm-5.3-flash (и другие reasoning-модели) по умолчанию
        # тратят 200-600 токенов на размышление ДО первого символа ответа.
        # Замер: "привет" -> 294 completion-токена, 2.4 с. С
        # reasoning_effort="minimal" -> 36 токенов, 0.8 с, тот же смысл.
        # Для ролевого чата размышления не нужны: она должна отвечать,
        # а не рассуждать. Пустая строка отключает параметр совсем
        # (для провайдеров, которые его не знают).
        self.reasoning_effort = reasoning_effort
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(timeout, connect=10.0),
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    # ---------------------------------------------------------------- chat

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        tools: Sequence[dict[str, Any]] = (),
        temperature: float = 0.8,
        top_p: float | None = None,
        chat_label: str = "",
        function_label: str = "chat",
        max_tokens: int | None = None,
    ) -> ChatResult:
        """Один вызов chat/completions. Tool-вызовы возвращаются, не исполняются."""
        use_model = model or self.model
        body: dict[str, Any] = {
            "model": use_model,
            "messages": messages,
            "temperature": temperature,
        }
        if top_p is not None:
            body["top_p"] = top_p
        if tools:
            body["tools"] = list(tools)
        if max_tokens:
            body["max_tokens"] = max_tokens
        if self.reasoning_effort:
            body["reasoning_effort"] = self.reasoning_effort

        resp = await self._post("/chat/completions", body)
        try:
            choice = resp["choices"][0]
        except (KeyError, IndexError) as e:
            raise LLMError(f"неожиданный ответ LLM: {resp}") from e
        message = choice.get("message", {})
        usage = resp.get("usage") or {}
        result = ChatResult(
            content=message.get("content") or "",
            reasoning=message.get("reasoning") or message.get("reasoning_content") or "",
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            cached_input_tokens=(usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0),
            finish_reason=choice.get("finish_reason", "stop"),
        )
        for tc in message.get("tool_calls") or []:
            fn = tc.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {"_raw": fn.get("arguments")}
            result.tool_calls.append(ToolCall(id=tc.get("id", ""), name=fn.get("name", ""), arguments=args))
        self._emit_usage(result, use_model, chat_label, function_label)
        return result

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        temperature: float = 0.8,
    ) -> AsyncIterator[str]:
        """Стриминг текстовых кусочков (для прокси)."""
        body = {
            "model": model or self.model,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
        }
        if self.reasoning_effort:
            body["reasoning_effort"] = self.reasoning_effort
        accumulated = {"input": 0, "output": 0}
        async with self._http.stream("POST", "/chat/completions", json=body) as resp:
            self._check(resp)
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[len("data: "):].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                usage = chunk.get("usage") or {}
                accumulated["input"] = usage.get("prompt_tokens", accumulated["input"])
                accumulated["output"] = usage.get("completion_tokens", accumulated["output"])
                for choice in chunk.get("choices") or []:
                    piece = (choice.get("delta") or {}).get("content")
                    if piece:
                        yield piece
        self._emit_usage(
            ChatResult(input_tokens=accumulated["input"], output_tokens=accumulated["output"]),
            model or self.model, "proxy", "chat_stream",
        )

    async def chat_json(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        chat_label: str = "",
        function_label: str = "json",
        temperature: float = 0.2,
    ) -> dict[str, Any]:
        """Вызов с ожиданием JSON-ответа; выковыривает JSON из markdown-блоков."""
        result = await self.chat(
            messages,
            model=model,
            temperature=temperature,
            chat_label=chat_label,
            function_label=function_label,
        )
        return extract_json(result.content)

    # -------------------------------------------------------------- embeddings

    async def embed(self, texts: list[str], *, model: str | None = None) -> list[list[float]]:
        if not texts:
            return []
        # Пустая строка/пробелы — валидный вход для нас, но не для провайдера:
        # OpenRouter отвечает 400 «Prompt must not be empty». Подставляем
        # заглушку, чтобы пакет не падал целиком из-за одного пустого текста.
        prepared = []
        for text in texts:
            clean = (text or "").strip()
            prepared.append(clean[:6000] if clean else "…")
        try:
            resp = await self._post("/embeddings", {
                "model": model or self.embed_model,
                "input": prepared,
            })
        except LLMError as e:
            log.warning("embeddings: %s (модель %s, текстов %d): %s",
                        type(e).__name__, model or self.embed_model,
                        len(prepared), str(e)[:200])
            raise
        data = resp.get("data") or []
        return [item["embedding"] for item in sorted(data, key=lambda d: d.get("index", 0))]

    async def embed_one(self, text: str, *, model: str | None = None) -> list[float]:
        vectors = await self.embed([text], model=model)
        if not vectors:
            raise LLMError("пустой ответ /embeddings")
        return vectors[0]

    # ---------------------------------------------------------------- misc

    async def healthy(self) -> bool:
        try:
            resp = await self._http.get("/models")
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    async def list_models(self) -> list[str]:
        try:
            resp = await self._http.get("/models")
            resp.raise_for_status()
            return [m.get("id", "") for m in resp.json().get("data", [])]
        except (httpx.HTTPError, ValueError):
            return []

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        resp = await self._http.post(path, json=body)
        self._check(resp)
        return resp.json()

    @staticmethod
    def _check(resp: httpx.Response) -> None:
        if resp.status_code >= 400:
            snippet = resp.text[:500]
            raise LLMError(f"LLM HTTP {resp.status_code}: {snippet}")

    def _emit_usage(self, result: ChatResult, model: str, chat: str, function: str) -> None:
        if self.bus is None:
            return
        self.bus.emit(
            "llm.call",
            model=model,
            chat=chat or "-",
            function=function,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cached_input_tokens=result.cached_input_tokens,
        )
        if result.reasoning.strip():
            self.bus.emit("llm.reasoning", model=model, chat=chat or "-",
                          function=function, text=result.reasoning.strip()[:2000])


def extract_json(text: str) -> dict[str, Any]:
    """Достаёт первый JSON-объект из ответа модели (даже завёрнутого в ```json)."""
    text = text.strip()
    candidates: list[str] = []
    if text.startswith("```"):
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("```"):
                continue
            candidates.append(line)
        text = "\n".join(candidates)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # сканируем на внешний {...}
    depth, start = 0, -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    start = -1
    raise LLMError(f"в ответе нет JSON: {text[:200]!r}")
