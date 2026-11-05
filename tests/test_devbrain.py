"""Тесты file-моста: запрос -> inbox, ответ -> outbox, разбор tool_calls."""
import asyncio
import json
from pathlib import Path

import pytest

from kuni.devbrain import FileBridgeLLM


async def _auto_responder(bridge_dir: Path, reply: dict, count: int = 1) -> None:
    """Играет роль «человека за рулём модели»: отвечает на первые count запросов."""
    outbox = bridge_dir / "outbox"
    answered = 0
    while answered < count:
        for f in (bridge_dir / "inbox").glob("*.json"):
            out = outbox / f.name
            if not out.exists():
                out.write_text(json.dumps(reply, ensure_ascii=False), encoding="utf-8")
                answered += 1
                if answered >= count:
                    return
        await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_bridge_text_roundtrip(tmp_path: Path):
    bridge = FileBridgeLLM(tmp_path / "bridge", poll_interval=0.05, timeout=5.0)
    responder = asyncio.ensure_future(
        _auto_responder(tmp_path / "bridge", {"content": "привет, я Куни"}))
    result = await bridge.chat([{"role": "user", "content": "привет"}], function_label="chat")
    await responder
    assert result.content == "привет, я Куни"
    assert result.input_tokens > 0 and result.output_tokens > 0
    assert not result.tool_calls


@pytest.mark.asyncio
async def test_bridge_tool_calls(tmp_path: Path):
    bridge = FileBridgeLLM(tmp_path / "bridge", poll_interval=0.05, timeout=5.0)
    reply = {"tool_calls": [{"name": "remember",
                             "arguments": {"text": "Влад любит Rust"}}]}
    responder = asyncio.ensure_future(_auto_responder(tmp_path / "bridge", reply))
    result = await bridge.chat(
        [{"role": "user", "content": "запомни: Влад любит Rust"}],
        tools=[{"type": "function", "function": {"name": "remember",
                                                  "parameters": {"type": "object"}}}],
        function_label="chat",
    )
    await responder
    assert result.content == ""
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "remember"
    assert result.tool_calls[0].arguments["text"] == "Влад любит Rust"


@pytest.mark.asyncio
async def test_bridge_chat_json(tmp_path: Path):
    bridge = FileBridgeLLM(tmp_path / "bridge", poll_interval=0.05, timeout=5.0)
    emotion = {"emotion": "радость", "valence": 0.8, "arousal": 0.4, "reason": "тест"}
    responder = asyncio.ensure_future(
        _auto_responder(tmp_path / "bridge", {"content": json.dumps(emotion, ensure_ascii=False)}))
    data = await bridge.chat_json([{"role": "user", "content": "оцени"}],
                                  function_label="emotion")
    await responder
    assert data["emotion"] == "радость"


@pytest.mark.asyncio
async def test_bridge_embeddings_deterministic(tmp_path: Path):
    bridge = FileBridgeLLM(tmp_path / "bridge")
    a = await bridge.embed_one("Влад и кошки")
    b = await bridge.embed_one("Влад и кошки")
    c = await bridge.embed_one("совсем другой текст про маркетинг")
    assert a == b
    assert len(a) == 64
    assert a != c


@pytest.mark.asyncio
async def test_bridge_timeout_raises(tmp_path: Path):
    from kuni.llm.client import LLMError
    bridge = FileBridgeLLM(tmp_path / "bridge", poll_interval=0.05, timeout=0.3)
    with pytest.raises(LLMError):
        await bridge.chat([{"role": "user", "content": "эй"}])
