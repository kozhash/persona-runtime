"""Монитор поведения: что она писала, как отвечала, где косячит.

Читает data/message_log.jsonl, сравнивает с водяным знаком (state file),
и выдаёт отчёт по поведенческим метрикам:

  * ответила ли (или промолчала) и через сколько секунд;
  * использовала ли reply на сообщение собеседника;
  * ставила ли реакции;
  * длина ответов, есть ли повторяющиеся фразы;
  * не отвечает ли в группе на всё подряд.

Запуск: python scripts/behavior_report.py [--minutes 15]
Выход: markdown-отчёт в stdout. Тихо, если новых сообщений нет.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG = ROOT / "data" / "message_log.jsonl"
STATE = ROOT / "data" / "behavior_watch.json"


def load_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    with path.open(encoding="utf-8") as fp:
        for line in fp:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def load_state(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return {"last_ts": 0.0}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def build_report(rows: list[dict], since: float, window_min: int) -> str | None:
    fresh = [r for r in rows if float(r.get("ts") or 0) > since]
    if not fresh:
        return None

    inbound = [r for r in fresh if r.get("topic") == "telegram.message"]
    outbound = [r for r in fresh if r.get("topic") == "telegram.sent"]

    # --- время ответа: для каждого входящего ищем первый исходящий после него
    latencies: list[float] = []
    answered = 0
    for i, inc in enumerate(inbound):
        start = float(inc.get("ts") or 0)
        nxt = None
        for cand in fresh:
            if cand.get("topic") != "telegram.sent":
                continue
            ts = float(cand.get("ts") or 0)
            if ts >= start and (nxt is None or ts < nxt):
                nxt = ts
        if nxt is not None and (nxt - start) <= 600:
            latencies.append(nxt - start)
            answered += 1

    # --- длина ответов
    lengths = [len(str(r.get("text") or "")) for r in outbound]
    avg_len = sum(lengths) / len(lengths) if lengths else 0

    # --- повторы: одинаковые ответы
    texts = [str(r.get("text") or "").strip() for r in outbound if r.get("text")]
    dupes = [t for t, n in Counter(texts).items() if n > 1 and len(t) > 20]

    # --- служебные утечки
    leaks = [t for t in texts if "#remember" in t or "#react" in t]

    lines: list[str] = []
    lines.append(f"### Поведение за {window_min} мин")
    lines.append("")
    lines.append(f"- входящих: **{len(inbound)}**, исходящих: **{len(outbound)}**")
    silent = len(inbound) - answered
    if inbound:
        pct = round(100 * answered / len(inbound))
        lines.append(f"- ответила на {answered}/{len(inbound)} ({pct}%), "
                     f"промолчала: {silent}")
    if latencies:
        lines.append(f"- средний ответ: **{sum(latencies)/len(latencies):.0f} с** "
                     f"(макс {max(latencies):.0f} с)")
    if lengths:
        lines.append(f"- длина ответа: в среднем **{avg_len:.0f}** символов "
                     f"(макс {max(lengths)})")

    # --- проблемы
    problems: list[str] = []
    if leaks:
        problems.append(f"служебные команды утекли в чат: {len(leaks)}")
    if dupes:
        problems.append(f"повторяющиеся ответы: {len(dupes)}")
    if lengths and avg_len > 600:
        problems.append(f"пишет слишком длинно ({avg_len:.0f} симв.)")
    if inbound and answered == len(inbound) and len(inbound) >= 5:
        problems.append("отвечает на всё подряд — нет избирательности")
    if latencies and min(latencies) < 1.0:
        problems.append("отвечает мгновенно — ломает человечность")

    if problems:
        lines.append("")
        lines.append("**Проблемы:**")
        for p in problems:
            lines.append(f"- {p}")

    # --- примеры переписки
    lines.append("")
    lines.append("<details><summary>Переписка</summary>")
    lines.append("")
    for r in fresh[-20:]:
        stamp = time.strftime("%H:%M:%S", time.localtime(float(r.get("ts") or 0)))
        if r.get("topic") == "telegram.sent":
            who = "**она**"
        else:
            who = "человек"
        text = str(r.get("text") or "").replace("\n", " / ")[:200]
        lines.append(f"- `{stamp}` {who}: {text}")
    lines.append("")
    lines.append("</details>")

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--minutes", type=int, default=15)
    parser.add_argument("--all", action="store_true",
                        help="игнорировать водяной знак")
    args = parser.parse_args()

    rows = load_rows(LOG)
    state = load_state(STATE)
    since = 0.0 if args.all else float(state.get("last_ts") or 0)

    report = build_report(rows, since, args.minutes)
    if report is None:
        save_state(STATE, {"last_ts": time.time()})
        return 0

    print(report)
    save_state(STATE, {"last_ts": time.time()})
    return 0


if __name__ == "__main__":
    sys.exit(main())
