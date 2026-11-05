"""Загрузка `.env` и разрешение ссылок на переменные окружения.

Зачем: секреты не должны лежать в config.toml (он может попасть в git,
в скриншот, в чужой контекст). Схема простая:

    api_key = "env:OPENROUTER_API_KEY"

`env:` в начале значения означает «возьми из окружения». Порядок поиска:
1) настоящая переменная окружения,
2) файл `.env` рядом с config.toml (или в корне проекта),
3) файл `data/.env`.

Файл `.env` в git не попадает (см. .gitignore).
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

ENV_PREFIX = "env:"
_loaded_files: list[Path] = []


def _parse_dotenv(text: str) -> dict[str, str]:
    """Минимальный парсер dotenv: KEY=VALUE, комментарии, кавычки."""
    result: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            quote = value[0]
            value = value[1:-1]
            if quote == '"':
                value = (value.replace("\\n", "\n").replace("\\t", "\t")
                              .replace('\\"', '"').replace("\\\\", "\\"))
        elif " #" in value:
            value = value.split(" #", 1)[0].strip()
        if key:
            result[key] = value
    return result


def load_dotenv(*candidates: Path) -> list[Path]:
    """Подхватывает .env-файлы. Настоящее окружение всегда приоритетнее."""
    loaded: list[Path] = []
    for path in candidates:
        if path is None:
            continue
        try:
            path = Path(path)
            if not path.is_file():
                continue
            pairs = _parse_dotenv(path.read_text(encoding="utf-8"))
        except OSError:
            log.warning("не смог прочитать %s", path)
            continue
        for key, value in pairs.items():
            if key not in os.environ and value:
                os.environ[key] = value
        loaded.append(path)
        if path not in _loaded_files:
            _loaded_files.append(path)
    if loaded:
        log.debug(".env загружен: %s", ", ".join(str(p) for p in loaded))
    return loaded


def bootstrap_env(config_path: Path | None = None) -> list[Path]:
    """Стандартный набор мест для .env вокруг конфига проекта."""
    candidates: list[Path] = []
    if config_path is not None:
        root = Path(config_path).resolve().parent
        candidates += [root / ".env", root / "data" / ".env"]
    candidates.append(Path.cwd() / ".env")
    return load_dotenv(*candidates)


def resolve_secret(value: str | None) -> str:
    """`env:NAME` -> значение переменной; всё остальное возвращается как есть."""
    if not value:
        return ""
    text = str(value)
    if not text.startswith(ENV_PREFIX):
        return text
    name = text[len(ENV_PREFIX):].strip()
    if not name:
        return ""
    resolved = os.environ.get(name, "")
    if not resolved:
        log.warning("переменная %s не задана (проверь .env)", name)
    return resolved


def env_name_of(value: str | None) -> str:
    """Имя переменной, если значение — ссылка `env:NAME`, иначе пустая строка."""
    if value and str(value).startswith(ENV_PREFIX):
        return str(value)[len(ENV_PREFIX):].strip()
    return ""
