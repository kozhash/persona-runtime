"""Тесты разбивки ответа на сообщения (28.10).

Замеры, которые эти правила воплощают:
    она    средняя длина 94 симв, 39% ответов с пустой строкой
    люди   средняя длина 39 симв,  3% с пустой строкой
"""
from __future__ import annotations

import random

from kuni.telegram.splitter import (
    MAX_PART_CHARS,
    pause_between_parts,
    should_quote,
    split_reply,
    strip_leading_dots,
    typing_seconds,
)


# ------------------------------------------------------------- разбивка
def test_empty_reply_gives_no_parts():
    assert split_reply("") == []
    assert split_reply("   \n  ") == []


def test_short_reply_stays_one_message():
    """Короткое человек отправляет одним сообщением."""
    parts = split_reply("Ха! Ты серьёзно?")
    assert parts == ["Ха! Ты серьёзно?"]


def test_paragraphs_become_separate_messages():
    """Абзацы модели = её паузы. Человек шлёт их отдельно."""
    rng = random.Random(1)
    text = "Ха! Ты? Забанить персонаж?!\n\nМеня лишили права писать — и что?"
    parts = split_reply(text, rng=rng)
    assert 1 <= len(parts) <= 3
    joined = " ".join(parts)
    assert "Забанить" in joined and "лишили" in joined


def test_no_part_is_a_wall_of_text():
    """Ни одна часть не должна быть полотном.

    Норма из ресерча (3.1 млн сообщений): 96.6% сообщений — до 20 слов,
    меньше 4% длиннее. Поэтому проверяем не «не бесконечно», а реальный
    порог в словах.
    """
    rng = random.Random(7)
    text = ("Это очень длинное сообщение, которое человек точно не отправил бы "
            "одним куском. " * 12)
    parts = split_reply(text, rng=rng)
    assert parts
    for p in parts:
        words = len(p.split())
        assert words <= 30, f"{words} слов — это полотно: {p[:60]!r}"


def test_max_three_parts():
    """Больше четырёх сообщений подряд люди не шлют (ресерч)."""
    rng = random.Random(3)
    text = "\n\n".join(f"Кусок номер {i} с текстом." for i in range(9))
    parts = split_reply(text, rng=rng)
    assert len(parts) <= 4


def test_tiny_tail_is_glued_back():
    """Огрызок в 2 символа отдельным сообщением выглядит поломкой."""
    rng = random.Random(5)
    text = "Нормальный текст про ману и усталость.\n\n.."
    parts = split_reply(text, rng=rng)
    assert all(len(p) >= 12 for p in parts), parts


def test_content_preserved():
    """Ничего не теряем при разбивке."""
    rng = random.Random(11)
    text = "Первая мысль.\n\nВторая мысль."
    parts = split_reply(text, rng=rng)
    combined = " ".join(parts)
    assert "Первая мысль" in combined
    assert "Вторая мысль" in combined


# -------------------------------------------------------------- скорость
def test_typing_speed_is_human():
    """14 cps (840 симв/мин) — так не печатает никто. 2.5-4 — человек."""
    rng = random.Random(1)
    secs = typing_seconds("а" * 100, cps=3.0, rng=rng)
    # 100 символов при 3 cps = 33 секунды, с разбросом
    assert 20 < secs < 50, secs


def test_typing_never_instant():
    rng = random.Random(1)
    assert typing_seconds("ок", cps=3.0, rng=rng) >= 0.8


def test_pause_between_parts_reasonable():
    """Между своими сообщениями человек делает паузу, но небольшую."""
    for seed in range(20):
        p = pause_between_parts(random.Random(seed))
        assert 1.0 <= p <= 5.0


# ---------------------------------------------------------------- цитата
def test_private_quotes_rarely():
    """В личке на последнее сообщение цитату почти не ставят."""
    quoted = sum(should_quote("private", rng=random.Random(i)) for i in range(200))
    assert quoted / 200 < 0.2, quoted


def test_group_quotes_sometimes():
    quoted = sum(should_quote("supergroup", rng=random.Random(i)) for i in range(200))
    assert 0.1 < quoted / 200 < 0.5


def test_quote_not_always():
    """Раньше было 100% — ровно это выглядело механическим."""
    for ct in ("private", "supergroup"):
        vals = [should_quote(ct, rng=random.Random(i)) for i in range(50)]
        assert not all(vals)


# ------------------------------------------------------------- многоточие
def test_leading_dots_stripped():
    """24% сообщений начинались с «...» — это тик, а не речь."""
    out = strip_leading_dots("...ладно, я поняла.", rng=random.Random(0))
    assert not out.startswith("...")


def test_leading_dots_kept_sometimes():
    """Изредка многоточие уместно — не вычищаем наглухо."""
    kept = sum(1 for i in range(200)
               if strip_leading_dots("...ладно.", rng=random.Random(i))
               .startswith("..."))
    assert 0 < kept < 100, kept


def test_dots_in_middle_untouched():
    text = "Ну... не знаю."
    assert strip_leading_dots(text, rng=random.Random(0)) == text


def test_no_dots_unchanged():
    assert strip_leading_dots("Ха!", rng=random.Random(0)) == "Ха!"
