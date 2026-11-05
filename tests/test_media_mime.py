"""Тесты определения MIME по сигнатуре.

Баг из живого прогона: TDLib сохранял webp под именем .jpg, мы отправляли
image/jpeg, провайдер отвечал 400 «media format does not match».
"""
from __future__ import annotations

from pathlib import Path

import pytest

from kuni.core.media import is_supported_image, sniff_image_mime


def test_jpeg_signature(tmp_path: Path):
    path = tmp_path / "photo.jpg"
    path.write_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 20)
    assert sniff_image_mime(path) == "image/jpeg"


def test_png_signature(tmp_path: Path):
    path = tmp_path / "photo.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20)
    assert sniff_image_mime(path) == "image/png"


def test_webp_disguised_as_jpg(tmp_path: Path):
    """Главный случай: webp с расширением .jpg."""
    path = tmp_path / "photo.jpg"
    path.write_bytes(b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 20)
    assert sniff_image_mime(path) == "image/webp"


def test_gif_signature(tmp_path: Path):
    path = tmp_path / "a.gif"
    path.write_bytes(b"GIF89a" + b"\x00" * 20)
    assert sniff_image_mime(path) == "image/gif"


def test_heic_signature(tmp_path: Path):
    path = tmp_path / "photo.jpg"     # тоже маскируется
    path.write_bytes(b"\x00\x00\x00\x18ftypheic" + b"\x00" * 20)
    assert sniff_image_mime(path) == "image/heic"


def test_bmp_signature(tmp_path: Path):
    path = tmp_path / "a.bmp"
    path.write_bytes(b"BM" + b"\x00" * 30)
    assert sniff_image_mime(path) == "image/bmp"


def test_tiff_signature(tmp_path: Path):
    path = tmp_path / "a.tif"
    path.write_bytes(b"II*\x00" + b"\x00" * 20)
    assert sniff_image_mime(path) == "image/tiff"


def test_unknown_returns_octet_stream(tmp_path: Path):
    path = tmp_path / "weird.bin"
    path.write_bytes(b"\x01\x02\x03\x04" * 8)
    assert sniff_image_mime(path) == "application/octet-stream"


def test_fallback_by_suffix_when_signature_unknown(tmp_path: Path):
    """Если сигнатура не распознана, но расширение знакомо — берём его."""
    path = tmp_path / "a.jpg"
    path.write_bytes(b"\x00" * 32)
    assert sniff_image_mime(path) == "image/jpeg"


def test_supported_set():
    assert is_supported_image("image/jpeg")
    assert is_supported_image("image/png")
    assert is_supported_image("image/webp")
    assert is_supported_image("image/gif")
    # эти провайдер не принимает — надо отказаться заранее
    assert not is_supported_image("image/heic")
    assert not is_supported_image("application/octet-stream")


def test_head_bytes_override(tmp_path: Path):
    """Можно передать уже прочитанные байты — без второго чтения файла."""
    path = tmp_path / "x.bin"
    assert sniff_image_mime(path, b"\xff\xd8\xff\xe0") == "image/jpeg"
