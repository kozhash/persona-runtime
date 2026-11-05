"""Генератор диаграмм архитектуры kuni-py в SVG.

Пишет четыре файла в docs/diagrams/. Схема рисуется кодом, а не руками,
чтобы при изменении модулей правки были в одном месте.

Правила вёрстки, которых держится файл:
  * внутри блока заголовок на y+19, подпись на y+34, контент — с y+54;
  * между блоками минимум 12 px;
  * стрелки рисуются до блоков, чтобы линии уходили под фигуры.
"""
from __future__ import annotations

from pathlib import Path

OUT = Path(__file__).resolve().parent / "diagrams"
OUT.mkdir(parents=True, exist_ok=True)

FONT = "ui-sans-serif, system-ui, -apple-system, 'Segoe UI', sans-serif"

STYLE = {
    "bg": "#f8fafc", "fg": "#172033", "muted": "#5b6475", "line": "#64748b",
    "neutral": "#e2e8f0", "core": "#c7d2fe", "data": "#99f6e4",
    "external": "#fde68a", "channel": "#bfdbfe", "risk": "#fecaca",
    "ok": "#bbf7d0",
}

HEAD = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="100%" role="img" aria-label="{title}">
<defs>
  <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
    <path d="M 0 0 L 10 5 L 0 10 z" fill="__LINE__"/>
  </marker>
</defs>
<rect width="100%" height="100%" fill="__BG__"/>
<style>
  text { font-family: __FONT__; }
  .t { font-size: 19px; font-weight: 650; fill: __FG__; }
  .l { font-size: 13.5px; font-weight: 600; fill: __FG__; }
  .s { font-size: 11.5px; fill: __MUTED__; }
  .m { font-size: 12px; fill: __FG__; }
  .n { stroke: __LINE__; stroke-width: 1.2; }
  .e { stroke: __LINE__; stroke-width: 1.4; fill: none; marker-end: url(#arrow); }
  .ed { stroke: __LINE__; stroke-width: 1.2; fill: none; stroke-dasharray: 5 4;
        marker-end: url(#arrow); }
  .zone { fill: none; stroke: __LINE__; stroke-width: 1; stroke-dasharray: 7 5; opacity: .65; }
</style>
"""


def head(w: int, h: int, title: str) -> str:
    text = HEAD
    for key, value in (
        ("{w}", str(w)), ("{h}", str(h)), ("{title}", title),
        ("__LINE__", STYLE["line"]), ("__BG__", STYLE["bg"]),
        ("__FONT__", FONT), ("__FG__", STYLE["fg"]),
        ("__MUTED__", STYLE["muted"]),
    ):
        text = text.replace(key, value)
    return text


def box(x, y, w, h, title, sub="", fill="neutral", rx=8):
    """Прямоугольник с заголовком и необязательной подписью."""
    out = [
        f'<rect class="n" x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" '
        f'fill="{STYLE[fill]}"/>',
        f'<text class="l" x="{x + 12}" y="{y + 20}">{title}</text>',
    ]
    if sub:
        out.append(f'<text class="s" x="{x + 12}" y="{y + 36}">{sub}</text>')
    return "\n".join(out)


def panel(x, y, w, h, title, rows, fill="core", width=92):
    """Блок с заголовком и списком пояснительных строк."""
    out = [box(x, y, w, h, title, "", fill)]
    for i, row in enumerate(rows):
        out.append(f'<text class="s" x="{x + 12}" y="{y + 56 + 19 * i}">{row}</text>')
    return "\n".join(out)


def label(x, y, text, cls="s"):
    return f'<text class="{cls}" x="{x}" y="{y}">{text}</text>'


def edge(x1, y1, x2, y2, dashed=False):
    cls = "ed" if dashed else "e"
    return (f'<path class="{cls}" d="M {x1} {y1} C {x1} {(y1 + y2) / 2}, '
            f'{x2} {(y1 + y2) / 2}, {x2} {y2}"/>')


def save(name, body, w, h, title):
    (OUT / name).write_text(head(w, h, title) + body + "\n</svg>\n", encoding="utf-8")
    print("wrote", name)


# --------------------------------------------------------------- 1. слои

def diagram_layers():
    w, h = 1000, 740
    parts = [
        '<text class="t" x="30" y="38">kuni-py: слои и зависимости</text>',
        label(30, 58, "Стрелка = «знает про». Модули общаются через шину, а не напрямую."),
    ]

    # --- ядро
    parts.append('<rect class="zone" x="25" y="78" width="950" height="118" rx="10"/>')
    parts.append(label(38, 100, "ЯДРО (никого не импортирует)", "l"))
    parts.append(box(45, 112, 200, 62, "core/config.py", "схема + hot-reload", "core"))
    parts.append(box(265, 112, 200, 62, "core/env.py", ".env, env:NAME", "core"))
    parts.append(box(485, 112, 200, 62, "core/logging.py", "единый формат", "core"))
    parts.append(box(705, 112, 250, 62, "character/prompts.py", "hot-reload .md", "core"))

    # --- домены
    parts.append(box(45, 228, 190, 62, "character/", "persona, emotions", "neutral"))
    parts.append(box(255, 228, 190, 62, "llm/client.py", "OpenAI-совместимый", "external"))
    parts.append(box(700, 228, 255, 62, "telegram/", "tdjson, client, human", "channel"))

    # --- шина (ниже доменов, чтобы не касаться их)
    parts.append(edge(140, 290, 380, 330, dashed=True))
    parts.append(edge(350, 290, 450, 330, dashed=True))
    parts.append(edge(830, 290, 620, 330, dashed=True))
    parts.append(box(380, 330, 300, 66, "core/bus.py", "pub/sub по glob-шаблонам", "core"))
    parts.append(label(380, 414, "журнал 500 событий → дашборд"))

    # --- память и руки
    parts.append('<rect class="zone" x="25" y="440" width="620" height="152" rx="10"/>')
    parts.append(label(38, 462, "ПАМЯТЬ (эпизоды + граф + факты)", "l"))
    parts.append(box(45, 474, 140, 54, "schema.py", "SQL + миграции", "data"))
    parts.append(box(195, 474, 140, 54, "episodic.py", "граф, факты", "data"))
    parts.append(box(345, 474, 140, 54, "retrieval.py", "2 пути", "data"))
    parts.append(box(495, 474, 135, 54, "decay.py", "забывание", "data"))
    parts.append(box(45, 538, 140, 44, "diary.py", "фасад", "data"))
    parts.append(box(195, 538, 140, 44, "consolidation.py", "сон", "data"))
    parts.append(box(345, 538, 285, 44, "working.py", ".md на 1–3 дня", "data"))

    parts.append('<rect class="zone" x="670" y="440" width="300" height="152" rx="10"/>')
    parts.append(label(683, 462, "РУКИ (инструменты)", "l"))
    parts.append(box(690, 474, 130, 54, "skills/builtin", "память, SD", "ok"))
    parts.append(box(830, 474, 125, 54, "self_edit", "тесты + откат", "ok"))
    parts.append(box(690, 538, 130, 44, "channel / profile", "", "ok"))
    parts.append(box(830, 538, 125, 44, "voice", "", "ok"))

    # шина -> память и руки
    parts.append(edge(450, 396, 400, 474, dashed=True))
    parts.append(edge(620, 396, 760, 474, dashed=True))

    # --- сбоку
    parts.append('<rect class="zone" x="25" y="616" width="950" height="96" rx="10"/>')
    parts.append(label(38, 638, "СБОКУ (слушают шину, их никто не импортирует)", "l"))
    parts.append(box(45, 648, 215, 50, "dashboard :10435", "лента + редактор", "risk"))
    parts.append(box(280, 648, 215, 50, "proxy :10434", "OpenAI-совместимый", "risk"))
    parts.append(box(515, 648, 215, 50, "metrics :9464", "Prometheus", "risk"))
    parts.append(box(750, 648, 215, 50, "assistant/", "доска задач", "risk"))
    parts.append(edge(530, 396, 530, 648, dashed=True))

    save("01-layers.svg", "\n".join(parts), w, h, "kuni-py layers")


# --------------------------------------------------------- 2. поток сообщения

def diagram_flow():
    w, h = 1020, 880
    parts = [
        '<text class="t" x="30" y="36">Поток одного сообщения</text>',
        label(30, 56, "От апдейта TDLib до отправки ответа и записи в память."),
    ]

    steps = [
        ("tdjson receive", "отдельный поток, блокирующий вызов", "external"),
        ("_process_new_message", "текст / войс / фото / стикер", "channel"),
        ("handle_message", "команда? echo? ночь?", "core"),
        ("_full_reply", "основной конвейер", "core"),
        ("diary.flash()", "2 пути: слова + ассоциации", "data"),
        ("persona.build()", "7 слоёв промпта", "neutral"),
        ("_chat_with_tools()", "до 4 шагов function-calling", "external"),
        ("anti_repeat", "если ответ повторяет прошлый", "ok"),
        ("human typing", "задержка + набор", "channel"),
        ("send_text / photo / voice", "отправка в чат", "channel"),
        ("_assess_emotion", "PAD + запись в дневник", "data"),
    ]

    x, y = 300, 84
    previous = None
    for title, sub, fill in steps:
        parts.append(box(x, y, 420, 56, title, sub, fill))
        if previous is not None:
            parts.append(edge(x + 210, previous + 56, x + 210, y))
        previous = y
        y += 70

    # ветки в сторону
    parts.append(box(40, 84, 220, 56, "эхо-режим", "LLM недоступен", "risk"))
    parts.append(edge(300, 112, 260, 112, dashed=True))
    parts.append(box(40, 224, 220, 56, "команда /status", "владелец", "risk"))
    parts.append(edge(300, 252, 260, 252, dashed=True))
    parts.append(box(760, 364, 230, 56, "воспоминания", "в контекст", "data"))
    parts.append(edge(720, 392, 760, 392, dashed=True))
    parts.append(box(760, 504, 230, 56, "скиллы", "draw / remember", "ok"))
    parts.append(edge(720, 532, 760, 532, dashed=True))
    parts.append(box(760, 644, 230, 56, "сон (по расписанию)", "NREM → REM → forget", "data"))
    parts.append(label(760, 722, "запускает scheduler, не этот конвейер", "s"))

    save("02-message-flow.svg", "\n".join(parts), w, h, "message flow")


# --------------------------------------------------------------- 3. память

def diagram_memory():
    w, h = 1020, 760
    parts = [
        '<text class="t" x="30" y="36">Память: уровни и сон</text>',
        label(30, 56, "Слева — путь записи, в центре — граф, справа — поиск и забывание."),
    ]

    # путь записи
    chain = [
        ("сообщение", "текст / эмоция / событие", "channel", 54),
        ("episodes: labile", "E-LTP + тег, TTL 2ч", "external", 74),
        ("захват (PRP)", "балл ≥ auto_admit → сразу", "ok", 74),
        ("episodes: consolidated", "L-LTP, долговременная", "data", 74),
        ("propositions", "DG: атомарные факты", "data", 74),
        ("facts", "битемпоральные", "data", 56),
    ]
    y = 88
    previous_end = None
    for title, sub, fill, height in chain:
        parts.append(box(40, y, 260, height, title, sub, fill))
        if previous_end is not None:
            parts.append(edge(170, previous_end, 170, y))
        previous_end = y + height
        y += height + 16

    # граф
    parts.append(panel(340, 88, 300, 300, "edges (граф Хебба)", [
        "w ← (1-λ)·w + η·I(ко-активация)",
        "",
        "типы: hebbian · synonymy",
        "       temporal · causal",
        "       prevents (отрицательный)",
        "",
        "потолок веса 3.0",
        "затухание за ночь",
        "удаляется ниже 0.01",
        "",
        "prevents = аналог ГАМК:",
        "гасит нерелевантные ветви",
    ], "ok"))
    parts.append(edge(300, 125, 340, 160, dashed=True))
    parts.append(edge(300, 420, 340, 420, dashed=True))

    # поиск
    parts.append(panel(680, 88, 320, 300, "поиск (два пути)", [
        "S_base = 0.55·cos + 0.25·BM25",
        "         + 0.20·decay(type, age)",
        "",
        "S = S_base + β·Σ S_base·w_ij",
        "",
        "R = Top-k ∪ Top-m(flip)",
        "",
        "↓ латеральное торможение:",
        "почти равные скоры схлопываются",
        "",
        "находит то, что лексически",
        "не похоже на запрос",
    ], "core"))
    parts.append(edge(640, 240, 680, 240, dashed=True))

    # забывание по типам
    parts.append(panel(680, 420, 320, 232, "затухание по типу", [
        "ENTITY   ~60 дней",
        "FACT     ~30 дней",
        "EVENT    ~72 часа",
        "THOUGHT  ~12 часов",
        "",
        "практика замедляет забывание",
        "(spacing effect)",
        "",
        "ниже порога — в архив,",
        "не удаление",
    ], "neutral"))

    # сон
    parts.append('<rect class="zone" x="40" y="672" width="960" height="56" rx="10"/>')
    parts.append(label(56, 698,
                       "СОН:  NREM (захват + реплей)  →  REM (дистилляция хабов)"
                       "  →  FORGET (затухание, архив)", "l"))
    parts.append(label(56, 716, "каждая фаза — отдельный метод, сон идемпотентен"))

    save("03-memory.svg", "\n".join(parts), w, h, "memory architecture")


# ------------------------------------------------------- 4. карта модулей

def diagram_modules():
    w, h = 1020, 660
    parts = [
        '<text class="t" x="30" y="36">Карта модулей</text>',
        label(30, 56, "Точки входа, зависимости и то, что живёт сбоку."),
    ]
    groups = [
        ("Вход", ["__main__.py", "app.py"], "core", 40, 84),
        ("Канал", ["telegram/tdjson", "telegram/client", "telegram/human"],
         "channel", 40, 190),
        ("Мозг", ["llm/client", "devbrain/bridge"], "external", 40, 330),
        ("Личность", ["character/persona", "character/emotions",
                      "character/prompts"], "neutral", 370, 84),
        ("Память", ["memory/episodic", "memory/retrieval", "memory/decay",
                    "memory/consolidation", "memory/diary"], "data", 370, 200),
        ("Руки", ["skills/builtin", "skills/self_edit", "skills/channel",
                  "skills/profile", "skills/voice", "skills/antigravity"],
         "ok", 370, 420),
        ("Ритмы", ["infra/scheduler", "infra/metrics"], "neutral", 700, 84),
        ("Сбоку", ["dashboard/server", "proxy/server", "assistant/board"],
         "risk", 700, 180),
        ("Совместимость", ["memory/store", "memory/sleep"], "neutral", 700, 344),
    ]
    for title, items, fill, x, y in groups:
        height = 48 + 20 * len(items)
        parts.append(f'<rect class="zone" x="{x}" y="{y}" width="300" height="{height}" rx="10"/>')
        parts.append(label(x + 12, y + 22, title, "l"))
        for i, item in enumerate(items):
            parts.append(label(x + 16, y + 46 + 20 * i, "· " + item, "s"))
    parts.append(label(40, 560, "Все группы выше общаются только через core/bus.py.", "m"))
    parts.append(label(40, 582, "Совместимость — тонкие обёртки над новой памятью.", "m"))
    save("04-modules.svg", "\n".join(parts), w, h, "modules map")


if __name__ == "__main__":
    diagram_layers()
    diagram_flow()
    diagram_memory()
    diagram_modules()
    print("saved to", OUT)
