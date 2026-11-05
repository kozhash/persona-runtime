"""Определение MIME изображения по сигнатуре — не по расширению.

Проблема: TDLib сохраняет файл с расширением, которое не всегда совпадает
с реальным форматом (например, `.jpg` для webp или heic). Мы отправляли
`data:image/jpeg;base64,...`, провайдер отвечал 400
«media format does not match a supported content type».

Решение: смотрим первые байты файла. Это надёжнее имени и работает для
всех форматов, которые поддерживает Telegram.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

#: Сигнатуры: (префикс байтов, mime)
_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"RIFF", "image/webp"),          # уточняется ниже: RIFF....WEBP
    (b"BM", "image/bmp"),
    (b"\x00\x00\x00", "image/heic"),   # ftyp box, уточняется ниже
    (b"\x00\x00\x00\x0c", "image/heic"),
    (b"\x00\x00\x00\x18", "image/heic"),
)

#: Расширение -> mime, если сигнатура не распознана
_BY_SUFFIX: dict[str, str] = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".jpe": "image/jpeg",
    ".png": "image/png", ".gif": "image/gif", ".webp": "image/webp",
    ".bmp": "image/bmp", ".heic": "image/heic", ".heif": "image/heif",
    ".tif": "image/tiff", ".tiff": "image/tiff",
}


def sniff_image_mime(path: Path | str, head: bytes | None = None) -> str:
    """Определить MIME картинки по содержимому.

    Возвращает `application/octet-stream`, если формат неизвестен —
    тогда вызывающий код должен отказаться от отправки в vision,
    а не гадать.
    """
    raw = head
    if raw is None:
        try:
            with open(path, "rb") as fp:
                raw = fp.read(32)
        except OSError:
            raw = b""

    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    # WebP: RIFF....WEBP
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    if raw.startswith(b"BM"):
        return "image/bmp"
    # HEIC/HEIF: ....ftypheic / ftypmif1 / ftypheix
    if len(raw) >= 12 and raw[4:8] == b"ftyp":
        brand = raw[8:12]
        if brand in (b"heic", b"heix", b"heim", b"heis", b"mif1", b"msf1"):
            return "image/heic"
        if brand in (b"avif", b"avis"):
            return "image/avif"
    # TIFF
    if raw.startswith((b"II*\x00", b"MM\x00*")):
        return "image/tiff"

    suffix = Path(path).suffix.lower() if path else ""
    return _BY_SUFFIX.get(suffix, "application/octet-stream")


def is_supported_image(mime: str) -> bool:
    """Поддерживает ли провайдер этот формат на входе."""
    return mime in ("image/jpeg", "image/png", "image/gif", "image/webp")


#: Видео-стикеры Telegram: webm (VP9) и tgs (gzip-lottie).
#: tgs — это векторная анимация, ffmpeg её не читает, поэтому её не трогаем.
_VIDEO_STICKER_SUFFIXES = (".webm", ".mp4")


def sticker_frame(path: Path | str, *, timeout: float = 20.0) -> Path | None:
    """Первый кадр видео-стикера как PNG — чтобы vision мог его прочитать.

    Зачем: «пушистые» стикеры приходят как webm, и в модель уходил
    application/octet-stream — провайдер отвечал 400, стикер оставался
    невидимым для неё. ffmpeg вытаскивает кадр, дальше это обычный PNG.
    При любой ошибке — None (стикер просто останется непрочитанным).
    """
    src = Path(path)
    if src.suffix.lower() not in _VIDEO_STICKER_SUFFIXES:
        return None
    out = src.with_name(src.stem + "_frame.png")
    if out.exists() and out.stat().st_size > 0:
        return out
    try:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(src),
             "-frames:v", "1", "-f", "image2", "-y", str(out)],
            capture_output=True, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        return None
    return out
