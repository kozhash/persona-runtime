"""Прозрачный OpenAI-совместимый прокси.

POST /v1/chat/completions — проксирует запрос в реальный LLM, но перед
этим подмешивает системный промпт персонажа и контекст (дневник,
рабочая память). Tool-вызовы исполняет невидимо для клиента и отдаёт
уже финальный ответ. stream=true — честный SSE.

Клиент (VS Code Copilot, любой SDK) думает, что говорит с обычной
OpenAI-моделью, а на самом деле — с персонажем со памятью.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

log = logging.getLogger(__name__)


def create_proxy_app(
    llm: Any,
    persona_builder: Any,
    diary: Any,
    working_memory: Any,
    skills: Any,
    logs_dir: Path,
) -> FastAPI:
    app = FastAPI(title="kuni proxy", docs_url=None, redoc_url=None)
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_file = logs_dir / "proxy.jsonl"

    def _log(record: dict[str, Any]) -> None:
        record["ts"] = time.time()
        try:
            with open(log_file, "a", encoding="utf-8") as fp:
                fp.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass

    @app.get("/v1/models")
    async def models() -> dict:
        return {"object": "list", "data": [{"id": llm.model, "object": "model"}]}

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> Any:
        body = await request.json()
        stream = bool(body.get("stream"))
        model = body.get("model") or llm.model
        user_messages = [m for m in body.get("messages", []) if m.get("role") != "system"]

        last_user = next(
            (m.get("content", "") for m in reversed(user_messages) if m.get("role") == "user"),
            "",
        )
        associations = []
        try:
            associations = await diary.recall(last_user, k=3)
        except Exception:
            log.exception("прокси: дневник недоступен")

        system = persona_builder.build(
            chat_title="proxy", associations=associations,
            working_memory=working_memory.content(),
            extra="Сейчас ты работаешь как ассистент в редакторе кода через прокси.",
        )
        messages = [{"role": "system", "content": system}] + user_messages

        # tool-цикл (не для стрима — там отдаём как есть, tool-ы выключены)
        if not stream and skills is not None and skills.names():
            messages = await _run_tool_loop(llm, messages, skills, model)

        started = time.time()

        async def sse() -> Any:
            first = True
            async for piece in llm.chat_stream(messages, model=model):
                chunk = {
                    "id": "kuni-proxy", "object": "chat.completion.chunk", "model": model,
                    "choices": [{"index": 0, "delta": ({"content": piece} if first else {"content": piece}),
                                 "finish_reason": None}],
                }
                first = False
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
            yield 'data: [DONE]\n\n'
            _log({"model": model, "stream": True, "seconds": round(time.time() - started, 2)})

        if stream:
            return StreamingResponse(sse(), media_type="text/event-stream")

        result = await llm.chat(messages, model=model, function_label="proxy")
        response = {
            "id": "kuni-proxy", "object": "chat.completion", "model": model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": result.content},
                "finish_reason": result.finish_reason,
            }],
            "usage": {
                "prompt_tokens": result.input_tokens,
                "completion_tokens": result.output_tokens,
                "total_tokens": result.input_tokens + result.output_tokens,
            },
        }
        _log({
            "model": model, "stream": False,
            "seconds": round(time.time() - started, 2),
            "in": result.input_tokens, "out": result.output_tokens,
        })
        return JSONResponse(response)

    return app


async def _run_tool_loop(llm: Any, messages: list[dict], skills: Any, model: str) -> list[dict]:
    """Исполняет tool-вызовы за клиента; наружу — только финальный текст."""
    for _ in range(4):
        result = await llm.chat(
            messages, model=model, tools=skills.openai_tools(), function_label="proxy_tools",
        )
        if not result.tool_calls:
            messages.append({"role": "assistant", "content": result.content})
            return messages
        messages.append({
            "role": "assistant", "content": result.content or None,
            "tool_calls": [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.name, "arguments": json.dumps(tc.arguments, ensure_ascii=False)}}
                for tc in result.tool_calls
            ],
        })
        for tc in result.tool_calls:
            outcome = await skills.dispatch(tc.name, tc.arguments)
            messages.append({
                "role": "tool", "tool_call_id": tc.id,
                "content": outcome.output[:4000],
            })
    return messages
