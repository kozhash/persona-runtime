# План: идентичность собеседника в памяти

Основа: вариант 2 (внешний архитектор) с правками владельца.
Дата: 27.10.2026.

---

## Решения владельца

| Пункт | Решение |
|---|---|
| Пассивное слушание групп | **Отклонено.** Вместо — peek по требованию |
| Peek до входа в обсуждение | **20 сообщений назад** |
| Peek после закрытия | **20 сообщений вперёд, через 7 минут тишины** |
| Две корзины в промпте | **Принято** |
| Атрибуция при кластеризации в сне | **Принято** |
| Квота слотов | **Принято** |

---

## Что проверено в коде (факты)

| Факт | Где |
|---|---|
| `sender_name` в `IncomingMessage` отсутствует | `client.py:62-81` |
| `getUser` не реализован — только `getChat` | `client.py:238` |
| `getChatHistory` принимает только `limit` | `client.py:553` |
| `tdjson.request` принимает любой dict → offset добавить легко | `tdjson.py:179` |
| `history.append` после проверки «адресовано» → группа не пишется | `app.py:459-463`, `548` |
| `speaker` пустой у всех 142 эпизодов | база |
| 33 эпизода (23%) с `chat_id=0` | база |
| `_cluster` группирует только по cosine текста | `consolidation.py:182` |
| `entities` = первое слово текста (не автор) | `consolidation.py:235` |
| `relation_weight` только по `chat_id` | `service.py:585` |
| `Node` не несёт `chat_id`/`speaker` | `retrieval.py:59` |
| `SCHEMA_VERSION = 4`, миграции есть | `schema.py:21` |

---

## Шаг 0. Имена людей

**Проблема:** `sender_user_id` есть, имени нет вообще.

**Что делаем:**

1. `IncomingMessage.sender_name: str = ""`
2. В `client.py` кэш `self._users: dict[int, dict]`, метод:

```python
async def get_user_name(self, user_id: int) -> str:
    """Имя из TDLib с кэшем. Не ходит в сеть повторно."""
    if user_id in self._users:
        return self._users[user_id]
    try:
        u = await self.request({"@type": "getUser", "user_id": user_id})
    except TdError:
        return ""
    name = " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x).strip()
    if not name:
        name = (u.get("usernames") or {}).get("active_username") or str(user_id)
    self._users[user_id] = name
    return name
```

3. В `_process_new_message` — заполнять `sender_name` (await, но кэш делает это дешёвым).

**Тесты:** имя из кэша, имя из TDLib, фолбэк на user_id.

---

## Шаг 1. Peek: контекст до и после

**Не пассивное слушание.** Подгружаем только когда нужно.

### 1a. Расширить `get_chat_history`

```python
async def get_chat_history(self, chat_id: int, limit: int = 20,
                           from_message_id: int = 0,
                           offset: int = 0) -> list[dict]:
    body = {"@type": "getChatHistory", "chat_id": chat_id,
            "limit": limit, "offset": offset, "only_local": False}
    if from_message_id:
        body["from_message_id"] = from_message_id
    result = await self.request(body)
    return result.get("messages", [])
```

TDLib: `offset = -20` — 20 сообщений **до** `from_message_id`,
`offset = +1` — **после**.

### 1b. Peek при входе в обсуждение

Когда `_should_reply_in_chat` вернул True (её позвали), перед сборкой
промпта:

```python
async def _peek_context(self, msg: IncomingMessage, before: int = 20) -> list[dict]:
    """20 сообщений до того, на которое отвечаем."""
    raw = await self.telegram.get_chat_history(
        msg.chat_id, limit=before, from_message_id=msg.message_id, offset=-before)
    return self._to_dialogue(raw)   # отсортировать по ts, вытащить авторов
```

Результат — в `history` **перед** её репликой, с именами:

```
[собеседник]: а я говорю что кофе лучше
[УзбекГПТ]: нет, чай
[собеседник]: персонаж, скажи ему
```

### 1c. Peek после закрытия (7 минут тишины)

Новый трекер в `app.py`:

```python
self._chat_activity: dict[int, dict] = {}   # chat_id -> {"last_msg_ts", "peek_armed"}
```

Периодическая задача раз в минуту:

```python
async def _idle_peek(self) -> None:
    """Через 7 минут тишины — досмотреть, чем кончился разговор."""
    now = time.time()
    for chat_id, st in list(self._chat_activity.items()):
        if not st.get("peek_armed"):
            continue
        if now - st["last_msg_ts"] < 7 * 60:
            continue
        st["peek_armed"] = False
        try:
            after = await self.telegram.get_chat_history(
                chat_id, limit=20,
                from_message_id=st["last_seen_id"], offset=1)
            episodes = self._to_episodes(chat_id, after)
            self.diary_store.add_bulk(episodes)     # в память, не в диалог
            log.info("досмотр чата %s: %d сообщений после", chat_id, len(after))
        except Exception:
            log.debug("досмотр не удался", exc_info=True)
```

`peek_armed` ставится, когда она **участвовала** в разговоре (ответила
или её позвали). `last_msg_ts` обновляется на каждом входящем.

**Регистрация:** `self.scheduler.add_interval("idle_peek", 60.0, self._idle_peek)`

**Тесты:** peek не срабатывает раньше 7 минут; срабатывает один раз;
не срабатывает, если она не участвовала.

---

## Шаг 2. Атрибуция в памяти

### 2a. Схема (через миграции, не голым ALTER)

`schema.py`, `SCHEMA_VERSION 4 → 5`:

```sql
ALTER TABLE episodes ADD COLUMN speaker_id INTEGER NOT NULL DEFAULT 0;
ALTER TABLE episodes ADD COLUMN speaker_name TEXT NOT NULL DEFAULT '';
ALTER TABLE episodes ADD COLUMN chat_title TEXT NOT NULL DEFAULT '';
```

`speaker` (старое поле) не трогаем — используем как «роль»
(`owner`/`other`/`self`), чтобы не ломать существующие тесты.

### 2b. Запись

`diary.add(...)` получает `speaker_id`, `speaker_name`, `chat_title`.
Все вызовы в `app.py` их передают.

### 2c. `_diary_save` — по чатам, не кашей

Сейчас (`app.py:1669`) склеивает все чаты. Переписать:

```python
for chat_id, chat_history in self.histories.items():
    recent = [item for item in chat_history
              if item.get("role") in ("user", "assistant")]
    if len(recent) < 4:
        continue
    tail = "\n".join(f"[{item.get('speaker') or '?'}]: {item['content']}"
                     for item in recent[-40:])
    ...
    await self.diary.add(piece, source="diary_save",
                         chat_id=chat_id,
                         speaker_id=..., speaker_name=...)
```

### 2d. Кластеризация во сне — не сливать разных людей

`consolidation.py:182`, `_cluster`:

```python
if candidate in used or candidate == episode_id:
    continue
if by_id[candidate].speaker_id != by_id[episode_id].speaker_id:
    continue          # ← НЕ сливать разных людей
if float(sims[int(idx)]) < threshold:
    break
```

Плюс `entities` заменить с «первое слово» на реальные имена:

```python
entities = sorted({e.speaker_name for e in cluster if e.speaker_name})[:8]
```

**Тесты:** два эпизода с разными `speaker_id` не попадают в один кластер.

---

## Шаг 3. Две корзины в промпте

`persona.py`, блок ассоциаций. Вместо одного списка — два:

```
Ты сейчас говоришь с собеседником (личка).

<about_them>
- (вчера) Он сказал, что любит кофе
- (3 дня назад, в группе) Он злился из-за плова
</about_them>

<about_others note="это про других людей — не путай с собеседником">
- (вчера, в группе, говорил УзбекГПТ) «чай лучше»
- (неделя назад, личка, Максим) «я люблю чай»
</about_others>
```

### Квота слотов

`flash()` получает `interlocutor_id: int`. Из `k` слотов:

- минимум **2** — про текущего собеседника (если есть)
- остальные — кто угодно
- если про собеседника меньше 2 в памяти — отдаём что есть, не добиваем

Обоснование квоты: сейчас 85 эпизодов УзбекГПТ против 15 владельца —
без квоты его воспоминания вытеснят твои.

### Формулировка для модели

Ключевая строка в `<about_others>`:

> «Это воспоминания о других людях. Не приписывай их собеседнику
> и не путай, кто что сказал.»

**Не** перечислять в промпте «это сказал X, это сказал Y» отдельными
инструкциями — только в разметке блоков. Иначе модель начнёт
декларировать источники в ответе.

---

## Шаг 4. `Node` и поиск

`retrieval.py:59`, добавить поля:

```python
chat_id: int = 0
speaker_id: int = 0
speaker_name: str = ""
chat_title: str = ""
```

Проставлять при сборке узлов из эпизода. Без этого информация
теряется на выходе поиска, даже если в базе есть.

`to_dict()` — включить их в выдачу.

---

## Шаг 5. Сироты (33 эпизода)

Не удалять, не восстанавливать эвристиками.

- пометить `speaker_name = ''`, `chat_id = 0` → в промпте попадают
  только в `<about_others>` с пометкой «источник неизвестен»
- **не** подмешивать в `<about_them>` — там нужен точный `speaker_id`

Одна строка в фильтре корзин.

---

## Порядок внедрения

| # | Что | Риск | Тесты |
|---|---|---|---|
| 0 | Имена людей (`getUser` + кэш) | низкий | 3 |
| 1a | Расширить `get_chat_history` | низкий | 2 |
| 1b | Peek 20 назад | низкий | 2 |
| 2a-2b | Схема v5 + запись атрибуции | средний (миграция) | 4 |
| 2c | `_diary_save` по чатам | средний | 3 |
| 2d | Кластеризация без смешивания | средний | 2 |
| 3 | Две корзины + квота | средний | 4 |
| 1c | Peek после 7 минут | средний (новый цикл) | 3 |
| 4 | `Node` поля | низкий | 2 |
| 5 | Сироты | низкий | 1 |

**Всего:** ~26 новых тестов. Существующие 248 должны остаться зелёными.

---

## Риски

| Риск | Защита |
|---|---|
| `getUser` на каждое сообщение → задержка | кэш в памяти, TDLib отвечает из локальной БД |
| Peek 20 назад раздувает промпт | только при входе в обсуждение, не на каждое |
| Peek после 7 минут тратит запросы | только если участвовала; раз в минуту проверка |
| `[Имя]: текст` в истории → модель копирует формат | формат только в peek-вставке, не в основной истории |
| Миграция v5 ломает базу | миграции уже есть, тест на пустой базе |
| Квота вытесняет полезное | минимум 2, не жёстко 2 |

---

## UNVERIFIED

| Что | Статус |
|---|---|
| TDLib `offset=-20` даёт ровно 20 до | **не проверено на живом TDLib**, только по документации |
| `offset=+1` даёт сообщения после | то же |
| 7 минут — верный порог тишины | выбрано владельцем, не измерено |
| Квота 2 слота достаточна | оценка, не измерено |
| `getUser` не тормозит | не замерял |

**Проверить в первую очередь:** `offset` на живом аккаунте. Если
TDLib ведёт себя иначе — вся схема peek'а меняется.

---

## Чего НЕ делаем

- Не изолируем память по чатам (требование владельца)
- Не пишем все сообщения групп в историю (отклонено)
- Не тащим таблицы `persons` / `chat_meta` — имя прямо в эпизоде
- Не используем ContextVar — передаём явно
- Не восстанавливаем сирот эвристиками по времени
