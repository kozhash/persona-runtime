"""Политика путей: где ей можно, а где нельзя.

Гейт держится на трёх правилах, и все три — в коде, не в промпте:

1. Всё, что не лежит внутри разрешённого корня, — отказ. Никаких
   «ну я только посмотрю»: сравнение идёт по resolved-путям, поэтому
   `..` и симлинки не помогают.
2. Секреты запрещены даже внутри корня: `.env`, `config.toml`, ключи.
   Иначе она прочитает свой же OpenRouter-ключ и Telegram-сессию.
3. Размер ограничен: чтение и запись не должны ронять процесс на
   случайном бинарнике в 2 ГБ.
"""
from __future__ import annotations

import fnmatch
import logging
from pathlib import Path

log = logging.getLogger(__name__)


class PathDenied(Exception):
    """Путь вне корней или в списке секретов."""


class PathPolicy:
    def __init__(self, roots: list[str], deny_patterns: list[str],
                 max_read_bytes: int = 200_000,
                 max_write_bytes: int = 400_000) -> None:
        self.roots = [Path(r).expanduser().resolve() for r in roots if r]
        self.deny_patterns = [p.lower() for p in deny_patterns if p]
        self.max_read_bytes = max_read_bytes
        self.max_write_bytes = max_write_bytes

    # ------------------------------------------------------------- проверка

    def resolve(self, raw: str, *, must_exist: bool = False) -> Path:
        """Проверить путь и вернуть абсолютный. Бросает PathDenied."""
        if not raw or not str(raw).strip():
            raise PathDenied("пустой путь")
        path = Path(str(raw)).expanduser()
        if not path.is_absolute():
            # относительные считаем от первого корня — предсказуемее, чем от cwd
            if not self.roots:
                raise PathDenied("нет разрешённых корней")
            path = self.roots[0] / path
        try:
            path = path.resolve()
        except OSError as e:
            raise PathDenied(f"путь не разрешается: {e}") from e

        if not any(self._inside(path, root) for root in self.roots):
            raise PathDenied(
                f"путь вне разрешённых корней: {path}\n"
                f"разрешено: {', '.join(str(r) for r in self.roots)}")
        self._check_secrets(path)
        if must_exist and not path.exists():
            raise PathDenied(f"нет такого файла: {path}")
        return path

    def _inside(self, path: Path, root: Path) -> bool:
        try:
            return path == root or path.is_relative_to(root)
        except (ValueError, OSError):
            return False

    def _check_secrets(self, path: Path) -> None:
        name = path.name.lower()
        as_posix = path.as_posix().lower()
        for pattern in self.deny_patterns:
            if fnmatch.fnmatch(name, pattern):
                raise PathDenied(f"файл в списке секретов: {path.name} (шаблон {pattern})")
            if pattern.startswith("*") and fnmatch.fnmatch(as_posix, "*" + pattern):
                raise PathDenied(f"путь в списке секретов: {path} (шаблон {pattern})")
            # .ssh, .git-секреты и прочие каталоги целиком
            if pattern in path.parts or f"/{pattern}/" in as_posix:
                raise PathDenied(f"каталог в списке секретов: {pattern}")

    # ------------------------------------------------------------- чтение

    def read_text(self, raw: str) -> tuple[Path, str, bool]:
        """Вернуть (путь, текст, обрезано ли)."""
        path = self.resolve(raw, must_exist=True)
        if not path.is_file():
            raise PathDenied(f"это не файл: {path}")
        size = path.stat().st_size
        truncated = size > self.max_read_bytes
        data = path.read_bytes()[: self.max_read_bytes]
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("utf-8", errors="replace")
            text = "[не utf-8, нечитаемые байты заменены]\n" + text
        return path, text, truncated

    def check_write_size(self, data: bytes) -> None:
        if len(data) > self.max_write_bytes:
            raise PathDenied(
                f"слишком большой файл: {len(data)} байт (лимит {self.max_write_bytes})")

    def describe(self) -> str:
        return "корни: " + ", ".join(str(r) for r in self.roots)
