# Архитектура persona-runtime

Документ описывает, **как проект устроен сейчас** и почему именно так.
Если меняешь модуль — обнови раздел. Актуально на 27.10.2026,
коммит `2ee8f8d`.

---

## 0. В одном экране

```
Telegram (TDLib, юзер-аккаунт)
        │
        ▼
   TelegramModule ──► Bus (pub/sub по glob)  ◄── dashboard :10435
        │                        │                proxy     :10434
        ▼                        │                metrics   :9464
   KuniApp.handle_message        │
        │                        │
        ├─ команды владельца     │
        ├─ ночь / тишина         │
        ▼                        │
   _full_reply ──────────────────┤
        ├── aффект: receive_message (S/N/D, социальная боль)
        ├── память: diary.flash (2 пути + ассоциации)
        ├── персона: persona.build (8 слоёв + якорь)
        ├── LLM: _chat_with_tools (function-calling)
        ├── человек: задержки, набор
        ├── отправка
        └── _assess_emotion (после отправки, async)
```

72 модуля, 15 537 строк, 24 промпта, 20 тест-файлов, 248 тестов.
63 топика шины.

---

## 1. Форма проекта

**Модульный монолит**: одно приложение, много изолированных модулей,
один процесс. Не микросервисы — но и не «одна большая функция».

```
                       ┌──────────────────────────┐
                       │      kuni/app.py         │
                       │  сборка + конвейер       │
                       └────────────┬─────────────┘
                                    │ создаёт и связывает
    ┌─────────┬─────────┬───────────┼──────────┬──────────┬─────────┐
    ▼         ▼         ▼           ▼          ▼          ▼         ▼
character  memory     affect       llm     telegram    skills   harness
(кто она) (что помнит)(что чувств.)(мозг)  (канал)    (руки)   (код/шелл)
    │         │         │           │          │          │         │
    └─────────┴─────────┴───────────┼──────────┴──────────┴─────────┘
                                    ▼
                        ┌───────────────────────┐
                        │   core/bus.py         │
                        │  pub/sub по glob      │
                        └───────────┬───────────┘
                                    │ слушают сбоку
                  ┌─────────────────┼─────────────────┐
                  ▼                 ▼                 ▼
            dashboard          proxy.server       metrics
            (:10435)           (:10434)           (:9464)
```

Ключевое свойство: **модули не импортируют друг друга**. Они публикуют
события и подписываются на шаблоны топиков. `telegram` не знает про
`llm`, `memory` не знает про `character`, `metrics` не знает ни про кого.

Проверка: из 72 модулей **36 не имеют внутренних импортов вообще**.

---

## 2. Пакеты и кто за что отвечает

| Пакет | Файлов | Ответственность | Не знает про |
|---|---|---|---|
| `core/` | 8 | шина, конфиг, hot-reload, singleton, env | модули |
| `character/` | 5 | персона, эмоции, промпты, стиль | транспорт, LLM-провайдер |
| `affect/` | 8 | моноамины, appraisal, соц. боль, руминация, модуляция | промпты, телеграм |
| `memory/` | 12 | эпизоды, граф, факты, поиск, забывание, сон, дневник | транспорт |
| `llm/` | 2 | OpenAI-совместимый HTTP-клиент | память, телеграм |
| `telegram/` | 4 | TDLib-мост, апдейты, отправка, человеческие задержки | LLM |
| `skills/` | 10 | реестр инструментов и сами скиллы | ядро |
| `harness/` | 5 | code_task (Codex CLI), shell, fs | телеграм |
| `proactivity/` | 2 | импульсы, мысли, нужды | LLM-провайдер |
| `assistant/` | 2 | доска задач, режим ассистента | память |
| `devbrain/` | 2 | мост к внешнему dev-агенту | — |
| `dashboard/` | 2 | веб-панель, живой лог | все |
| `proxy/` | 2 | OpenAI-совместимый внешний API | все |
| `infra/` | 3 | планировщик, метрики, логи | модули |
| `console/` | 2 | чат в терминале | телеграм |

### Модули внутри ключевых пакетов

| Файл | Что делает |
|---|---|
| `affect/engine.py` | S/N/D, RK4, клиренс, потолок всплеска |
| `affect/appraisal.py` | 8 измерений (Scherer/Lazarus/EMA), быстрый + кортикальный |
| `affect/social.py` | 4 потребности (Уильямс), 5 профилей удара, руминация |
| `affect/modulation.py` | состояние → температура/max_tokens/top_p + телесная директива |
| `affect/service.py` | фасад: приём сообщения, привязанность, снапшоты |
| `character/emotions.py` | 24 эмоции, PAD, mood, привыкание, `style_directive` |
| `character/persona.py` | сборка системного промпта из 8 слоёв |
| `llm/client.py` | HTTP, `reasoning_effort`, tool-calls, usage-события |
| `harness/code_task.py` | Codex CLI отдельным процессом, JSONL-прогресс |
| `harness/shell_skill.py` | allowlist-шелл, каждая команда через кнопку |

---

## 3. Шина событий

`Bus` — pub/sub с glob-шаблонами и журналом для дашборда.
`emit()` безопасен из любого потока: событие из receive-потока tdjson
переносится через `call_soon_threadsafe`.

**63 топика.** Основные:

| Топик | Кто публикует | Кто слушает |
|---|---|---|
| `telegram.message` / `.sent` | `telegram.client` | metrics, dashboard, proactivity |
| `telegram.human.*` | `telegram.human` | dashboard |
| `llm.call` / `llm.reasoning` | `llm.client` | metrics, dashboard |
| `emotion.changed` / `.logged` | `character.emotions` | dashboard |
| `social.pain` / `rumination.spawned` | `affect.social` | dashboard |
| `affect.impulse` / `.state` / `.burst` | `affect.service` | dashboard |
| `affect.circuit_breaker` | `affect.safety` | dashboard |
| `diary.written` / `memory.fact` | `memory.diary` | dashboard |
| `sleep.started` / `.finished` | `memory.consolidation` | dashboard |
| `tool.called` / `tool.result` | `skills` | dashboard |
| `harness.code_started` / `.code_finished` / `.shell_run` | `harness` | dashboard |
| `task.*` | `assistant.board` | dashboard |
| `proactivity.*` (acted/idle/blocked/reacted) | `proactivity.engine` | dashboard |
| `desire.armed` / `.updated` | `proactivity.engine` | dashboard |
| `approval.resolved` / `.timeout` | `harness` | dashboard |
| `self_edit.applied` | `skills.self_edit` | dashboard |
| `config.changed` | `core.config` | — |
| `*` | — | dashboard (лента) |

---

## 4. Аффект

Три моноамина в [0,1], решаются ОДУ методом RK4 в минуту:

- **S** серотонин (база 0.62) — стабильность
- **N** норадреналин (база 0.38) — возбуждение
- **D** дофамин (база 0.60) — мотивация, доминирование

Проекция в PAD:

```
P   = 1.15·(S−0.5) − 0.65·(N−0.5) + 0.75·(D−0.5)
A   = −0.35·(S−0.5) + 1.35·(N−0.5) + 0.55·(D−0.5)
Dom = 0.45·(S−0.5) + 0.35·(N−0.5) + 1.10·(D−0.5)
```

**Оценка** — 8 измерений: relevance, congruence, novelty, rpe, agency,
coping, norm, ego. Из них три индекса: `I_threat`, `I_agon`, `I_loss`.
`coping` — переключатель: может ответить → злость; не может → беспомощность.

**Социальная боль** — 4 потребности (belong/esteem/control/meaning),
5 профилей удара (muted, kicked, ignored, insult, humiliation).
Стадии: reflexive → reflective → resignation.

**Руминация** — заряд R, режим пережёвывание/рефлексия, растягивает
жизнь эмоции в `(1 + 4.5·R)` раз.

**Модуляция** — состояние меняет `temperature`, `max_tokens`, `top_p`
и даёт телесную директиву (`somatic_directive`), включена:
`variant_b_directive = true`.

Подробно: `docs/research/01-affect/AFFECT.md`.

---

## 5. Память

Основа — нейробиологическая модель (см. `docs/research/03-memory/MEMORY.md`).

```
сообщение
   │  E-LTP: лабильный буфер + синаптический тег (TTL 2ч)
   ▼
episodes  (ничего не выбрасывается на входе)
   │  балл важности >= auto_admit
   ▼
consolidated
   │  ночной сон: NREM → REM → FORGET
   ▼
propositions  +  edges (Хебб)  +  facts (битемпоральные)
```

**Поиск** — два пути + торможение:

```
S_base(v) = 0.55·cos + 0.25·BM25 + 0.20·decay(type, age)
S(v_j)    = S_base(v_j) + β·Σ S_base(v_i)·w_ij
R_final   = Top-k(S_base) ∪ Top-m(flip по ассоциации)
```

Затем латеральное торможение: почти одинаковые скоры схлопываются.

**Забывание** по типу: EVENT 72 ч, FACT ~30 дней,
ENTITY_DESCRIPTION ~60 дней, THOUGHT 12 ч.

Подробно: `docs/research/03-memory/MEMORY.md`, `docs/research/03-memory/MEMORY_IMPLEMENTATION.md`.

---

## 6. Личность и промпты

Системный промпт собирается из **8 слоёв** (`character/persona.py`):

```
1. character_base.md       — кто она
2. anchor.md               — якорь ~145 токенов (ContextEcho)
3. behavior.md             — как себя ведёт
4. chat_world.md           — где она (чаты, группы, каналы)
5. writing_style.md        — как пишет
6. character_appearance.md — как выглядит
7. социальная осведомлённость — что с ней происходит в этом чате
8. emotions.state.describe() — текущее состояние + style_directive
   + somatic_directive (если включена)
   + messages_epilogue.md
```

Плюс ассоциации из памяти, рабочая память, мысли, эпизод-режим.

**Персонаж:** персонаж, 22, архиволшебница клана Красных демонов.
Пафос — броня: чем хуже, тем громче речь. Задели гордость — вспышка.
Задели по-настоящему — пафос исчезает. (См. `docs/research/04-character/01_ANALYSIS.md`.)

**Якорь** (`anchor.md`, 145 токенов) стоит сразу после базы. По
ContextEcho (arXiv 2605.24279): короткий якорь держит личность 20+ ходов
и восстанавливает её после компакции, которая сама персону не чинит.

**Язык:** русский по умолчанию, казахский только если собеседник сам
на нём пишет. Директива в `persona.py`, а не в конфиге.

Правка промпта — обычный `.md`, hot-reload по mtime, без рестарта.

---

## 7. LLM

`llm/client.py` — OpenAI-совместимый HTTP. Один эндпоинт, несколько
секций конфига (`[llm]`, `[llm.vision]`, `[llm.emotion]`, `[llm.sleep]`,
`[llm.embedding]`), каждая может переопределить модель.

**`reasoning_effort = "minimal"`** — обязательный параметр для
reasoning-моделей. Без него токены уходят в поле `reasoning`, а `content`
приходит пустым с `finish_reason=length`. Замер: 294 токена / 2.4 с
против 36 токенов / 0.8 с.

**Устойчивость:** основная модель + `fallback_models`. Повтор при
`finish_reason=length` с `max_tokens=2000`.

---

## 8. Harness: руки для кода

Три инструмента, все требуют подтверждения кнопкой:

| Инструмент | Что делает | Границы |
|---|---|---|
| `fs` | чтение/запись/правки/поиск | только внутри `persona-runtime`; `.env`, ключи закрыты |
| `shell` | allowlist-команды | git/python/pytest/rg/...; запрещены `rm -rf`, `git push`, `\|`, `>` |
| `code_task` | Codex CLI отдельным процессом | песочница `workspace-write`, таймаут 25 мин |

Прогресс идёт в чат JSONL-ом, аудит — в лог. Ключ Codex читается из
`~/.codex/auth.json` в память, `stdin=DEVNULL`.

---

## 9. Поток одного сообщения

```
tdjson receive (поток)
        │ call_soon_threadsafe
        ▼
TelegramModule._process_new_message
        │ собирает IncomingMessage (текст/войс/фото/стикер)
        ▼
KuniApp.handle_message
        ├── команда владельца?      → ответ и выход
        ├── echo-режим (LLM нет)?   → диагностика и выход
        ├── ночь?                   → молчит, прочтёт утром
        ▼
_full_reply
        ├── mark_read → go_online
        ├── войс → Whisper → текст
        ├── фото → vision → описание
        ├── стикер → vision + sticker_to_text → смысл
        ├── affect.receive_message()   → S/N/D, соц. боль
        ├── diary.flash()              → всплывшие воспоминания
        ├── persona.build()            → системный промпт (8 слоёв)
        ├── _chat_with_tools()         → цикл function-calling
        ├── human.before_reply + typing_session
        ├── send_text / send_photo / send_voice
        └── _assess_emotion()          → ПОСЛЕ отправки, async
```

**Важно:** оценка эмоций идёт **после** отправки ответа и асинхронно.
Она влияет на следующий ответ, не на тот, который оценивает.

---

## 10. Точки расширения

| Что добавить | Куда | Что не трогать |
|---|---|---|
| Новый инструмент | `skills/new_thing.py` + регистрация в `app.__init__` | ядро, шину |
| Новый промпт-слой | `prompts/name.md` + вызов в `persona.build` | транспорт |
| Новый источник событий | публикуй в шину | остальные модули |
| Другой HTTP-эндпоинт | `[llm.<section>]` в конфиге | код |
| Другое хранилище памяти | класс с API `EpisodicStore` | `diary`, `app` |
| Новая метрика | `infra/metrics.py`, подписка на топик | модули-источники |

---

## 11. Осознанные компромиссы

| Решение | Почему так | Чем платим |
|---|---|---|
| Один процесс, не сервисы | проще отлаживать, дешевле | нельзя масштабировать модуль отдельно |
| `app.py` держит сборку | видно все связи в одном месте | файл большой, растёт |
| SQLite для памяти | ноль инфраструктуры, ACID | нет ANN-индекса, перебор векторов |
| LLM не решает конфликты фактов | детерминизм: serial побеждает | нужен аккуратный парсинг |
| Лабильный буфер вместо записи сразу | защита от потери контекста | память занимает место до сна |
| Промпты в `.md` | правятся без рестарта | нет типизации и валидации |
| Часть директив в коде | быстро, не требует файла | **ломается при смене персонажа** — см. `style_directive` |

---

## 12. Что проверено

- **248 тестов** (`pytest -q`), 20 файлов, 3 548 строк тестов.
- Импорт всех модулей без циклов.
- Сборка `KuniApp` на временной директории без сети.
- Все 24 промпта читаются библиотекой и попадают в системный текст.
- Живой прогон: автодиалог отвечает с первого запроса, по-русски,
  в голосе персонаж.
- Регрессии на реальные баги: reasoning-модель, язык, старая личность
  в коде.

```bash
pytest -q                     # всё
pytest tests -q --no-header   # без заголовков
kuni diagnose                 # проверка окружения
python scripts/start_wmi.py  # запуск
```

---

## 13. Что известно плохим

| Проблема | Где | Статус |
|---|---|---|
| Оценка эмоций после отправки | `app.py:664` | по замыслу, но мешает холодности |
| `merge_cortical_sec` мёртв | `affect/appraisal.py:330` | только тесты |
| Холодность не внедрена | — | отдана внешнему исследователю |
| Директивы в коде дублируют промпты | `character/emotions.py` | источник прошлых багов |
| Нет валидации LLM-оценок | — | не реализовано |
