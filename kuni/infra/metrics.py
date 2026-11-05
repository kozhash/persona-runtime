"""Prometheus-метрики: llm_usage_input/output/cache_hit/cache_miss
с метками model, chat, function — как в оригинале (порт 9464).
"""
from __future__ import annotations

import logging
from typing import Any

from prometheus_client import CollectorRegistry, Counter, start_http_server

log = logging.getLogger(__name__)


class Metrics:
    def __init__(self, bus: Any, port: int = 9464, enabled: bool = True) -> None:
        self.bus = bus
        self.port = port
        self.enabled = enabled
        self.registry = CollectorRegistry(auto_describe=True)
        self.usage_input = Counter(
            "llm_usage_input", "Входные токены LLM", ["model", "chat", "function"], registry=self.registry
        )
        self.usage_output = Counter(
            "llm_usage_output", "Выходные токены LLM", ["model", "chat", "function"], registry=self.registry
        )
        self.cache_hit = Counter(
            "llm_usage_cache_hit", "Попадания в кэш промпта", ["model", "chat", "function"], registry=self.registry
        )
        self.cache_miss = Counter(
            "llm_usage_cache_miss", "Промахи кэша промпта", ["model", "chat", "function"], registry=self.registry
        )
        self.messages_in = Counter(
            "telegram_messages_in", "Входящие сообщения", ["chat"], registry=self.registry
        )
        self.messages_out = Counter(
            "telegram_messages_out", "Исходящие сообщения", ["chat"], registry=self.registry
        )
        if enabled:
            start_http_server(port, registry=self.registry)
            log.info("prometheus-метрики на :%d", port)
        bus.subscribe("llm.call", self._on_llm_call)
        bus.subscribe("telegram.message", lambda p: self.messages_in.labels(str(p.get("chat_id"))).inc())
        bus.subscribe("telegram.sent", lambda p: self.messages_out.labels(str(p.get("chat_id"))).inc())

    def _on_llm_call(self, payload: dict) -> None:
        model = str(payload.get("model", "-"))
        chat = str(payload.get("chat", "-"))
        function = str(payload.get("function", "-"))
        self.usage_input.labels(model, chat, function).inc(payload.get("input_tokens", 0) or 0)
        self.usage_output.labels(model, chat, function).inc(payload.get("output_tokens", 0) or 0)
        cached = payload.get("cached_input_tokens", 0) or 0
        inp = payload.get("input_tokens", 0) or 0
        (self.cache_hit if cached > 0 else self.cache_miss).labels(model, chat, function).inc()
