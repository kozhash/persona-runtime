"""Разбивка ответа на сообщения так, как это делает человек.

Зачем (28.10). Владелец: «не нравится её поведение, не личность».

Замеры по её же логу против людей в тех же чатах:
    она    средняя длина 94 симв, 39% с пустой строкой
    люди   средняя длина 39 симв,  3% с пустой строкой

То есть она пишет одно длинное полотно с абзацами там, где человек
отправил бы две-три короткие реплики подряд. Это и читается как «бот».

Здесь только чистая логика без сети и без LLM — её легко тестировать.
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass

#: Абзацы длиннее этого режем: человек не отправляет стену текстом.
# 28.10 по ресерчу (3.1 млн сообщений): 96.6% сообщений — до 20 слов,
# меньше 4% длиннее. Целевой размер 3-12 слов. 20 слов по-русски —
# примерно 120-130 символов.
MAX_PART_CHARS = 130

#: Выше этого — уже не «одно сообщение», а полотно. Даже когда человек
#: шлёт не дробя, такое он разобьёт или сократит. 30 слов ≈ 190 символов.
ONE_MESSAGE_MAX_CHARS = 190

#: Меньше этого вторую часть не отправляем — приклеим к первой.
MIN_PART_CHARS = 12

_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")


@dataclass(frozen=True)
class Part:
    """Одна будущая реплика."""
    text: str
    #: секунды «набора» перед отправкой этой части
    typing_seconds: float = 0.0


def split_reply(text: str, *, rng: random.Random | None = None,
                max_parts: int = 3) -> list[str]:
    """Разбить ответ на короткие сообщения, как это сделал бы человек.

    Цель по ресерчу: 3-12 слов на сообщение, не больше 20.
    """
    rng = rng or random
    text = (text or "").strip()
    if not text:
        return []

    # Сколько частей реально нужно: длинный текст нельзя втиснуть в 3
    # части и получить короткие сообщения. Ресерч: без ответа люди шлют
    # не больше 3-4 подряд. Но если модель выдала полотно, лучше 6 коротких
    # реплик, чем одна стена в 36 слов — стену человек точно не отправит.
    # (На практике такие ответы — редкость: см. ограничение max_tokens.)
    needed = -(-len(text) // MAX_PART_CHARS)      # ceil
    budget = max(max_parts, min(6, needed))

    # 1. авторская разметка абзацами — уважаем её первой
    if "\n\n" in text:
        raw = [p.strip() for p in text.split("\n\n") if p.strip()]
    elif len(text) <= MAX_PART_CHARS:
        return [text]
    else:
        raw = _cut_by_sentences(text)

    # 2. короткие хвосты приклеиваем назад
    merged: list[str] = []
    for piece in raw:
        if merged and len(piece) < MIN_PART_CHARS:
            merged[-1] = f"{merged[-1]} {piece}".strip()
        else:
            merged.append(piece)

    # 3. раскладываем по частям РАВНОМЕРНО, а не сваливаем остаток в одну
    if len(merged) > budget:
        merged = _redistribute(merged, budget)

    # 4. человек не всегда дробит: иногда шлёт одним куском — но только
    # если это не полотно. Без проверки длины эта ветка склеивала всё
    # обратно в стену на 639 символов (поймал тест).
    if len(merged) > 1 and rng.random() < 0.25:
        joined = "\n\n".join(merged)
        if len(joined) <= ONE_MESSAGE_MAX_CHARS:
            return [joined]

    return merged


def _redistribute(parts: list[str], buckets: int) -> list[str]:
    """Разложить текст в `buckets` примерно равных кусков, сохраняя порядок.

    Две прошлые версии ошибались одинаково: последний кусок вбирал всё
    остальное (639 символов из 960). Причина — проверка «есть ли ещё
    слот» без проверки «хватит ли материала на остальные слоты».
    Здесь считаем оба числа явно.
    """
    if buckets <= 1:
        return ["\n\n".join(parts)]
    if len(parts) <= buckets:
        return list(parts)

    total = sum(len(p) for p in parts)
    target = total / buckets
    n = len(parts)
    out: list[str] = []
    buf = ""

    for i, p in enumerate(parts):
        remaining = n - i            # сколько частей осталось, включая эту
        slots_left = buckets - len(out)

        # закрываем текущий кусок, если уже набрали цель, есть куда
        # класть дальше и материала хватит на остальные слоты
        if (buf and len(buf) >= target and slots_left > 1
                and remaining > slots_left - 1):
            out.append(buf)
            buf = p
        else:
            buf = f"{buf} {p}".strip() if buf else p

    if buf:
        out.append(buf)

    return out


def _cut_by_sentences(text: str) -> list[str]:
    """Резать длинный текст по границам предложений, собирая до лимита."""
    sentences = [s for s in _SENTENCE_END.split(text) if s.strip()]
    parts: list[str] = []
    buf = ""
    for s in sentences:
        candidate = f"{buf} {s}".strip() if buf else s
        if buf and len(candidate) > MAX_PART_CHARS:
            parts.append(buf.strip())
            buf = s
        else:
            buf = candidate
    if buf.strip():
        parts.append(buf.strip())
    return parts


def typing_seconds(text: str, *, cps: float,
                   rng: random.Random | None = None) -> float:
    """Сколько человек набирал бы этот текст.

    cps — символы в секунду. Живой человек на телефоне: 2.5-4.
    Раньше в конфиге стояло 14 (840 симв/мин) — так не печатает никто,
    и «набор» проскакивал вдвое быстрее, чем нужно для правдоподобия.
    """
    rng = rng or random
    cps = max(0.5, float(cps))
    base = len(text) / cps
    # разброс: иногда печатает бодро, иногда залипает
    return max(0.8, base * rng.uniform(0.75, 1.35))


def pause_between_parts(rng: random.Random | None = None) -> float:
    """Пауза между двумя своими сообщениями подряд."""
    rng = rng or random
    return rng.uniform(1.2, 4.5)


def should_quote(chat_type: str, *, rng: random.Random | None = None) -> bool:
    """Ставить ли ответ цитатой.

    Раньше `reply_to=msg.message_id` передавался ВСЕГДА — 100% ответов
    шли цитатой. В живом Telegram цитата — исключение: она нужна, когда
    отвечаешь не на последнее сообщение (в группе) или когда их несколько.
    В личке на последнее сообщение цитату почти никогда не ставят.
    """
    rng = rng or random
    if chat_type in ("private", "secret", ""):
        # в личке — редко, только «для акцента»
        return rng.random() < 0.08
    # в группе чаще, но всё равно не всегда
    return rng.random() < 0.25


_LEADING_DOTS = re.compile(r"^\s*(?:\.{2,}|…)\s*")


def strip_leading_dots(text: str, *, rng: random.Random | None = None,
                       keep_chance: float = 0.12) -> str:
    """Убрать «...» в начале сообщения.

    Это её тик: 24% всех сообщений начинались с многоточия — и он
    повторялся подряд, что читается как шаблон, а не как живая речь.
    Оставляем изредка: многоточие уместно, когда правда тяжело.
    """
    rng = rng or random
    if not _LEADING_DOTS.match(text):
        return text
    if rng.random() < keep_chance:
        return text
    return _LEADING_DOTS.sub("", text, count=1)
