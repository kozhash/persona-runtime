#!/usr/bin/env bash
# Подтягивает модели для Kuni в Ollama.
set -euo pipefail

OLLAMA_HOST="${OLLAMA_HOST:-http://localhost:11434}"

echo "==> Проверяю Ollama на ${OLLAMA_HOST}"
curl -fsS "${OLLAMA_HOST}/api/tags" >/dev/null || {
  echo "Ollama не отвечает. Сначала: docker compose up -d ollama"
  exit 1
}

echo "==> Основная модель (нужен контекст ~32K)"
ollama pull qwen2.5:14b || ollama pull qwen2.5:7b

echo "==> Модель эмбеддингов для дневника"
ollama pull nomic-embed-text

echo "Готово. Пропиши выбранные модели в config.toml -> [llm]"
