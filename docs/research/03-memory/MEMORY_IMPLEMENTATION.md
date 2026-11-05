# Система памяти persona-runtime: как она реализована

Документ описывает **фактическую реализацию** в кодовой базе, а не замысел.
Каждый раздел указывает файл и строку, которые можно открыть и проверить.

Дата: 2026-10-26 · ветка `refactor/structure` · коммиты `303beed`, `e6037b8`

---

## 0. Сводка на одну страницу

| Что было | Что стало |
|---|---|
| одна таблица `diary`, плоский косинус | 5 таблиц: эпизоды, пропозиции, рёбра, факты, теги |
| запись всего подряд, без отбора | лабильный буфер + синаптический тег с TTL 2 ч |
| один путь поиска (cosine) | два пути: семантика+лексика+свежесть → граф |
| `diary_links` пуст (0 строк) | граф Хебба с затуханием и типами рёбер |
| одинаковое старение всего | затухание по типу знания (FACT 30 дней / THOUGHT 12 ч) |
| сон = один проход LLM | три фазы: NREM → REM → FORGET |
| 43 теста | 86 тестов, включая ассоциативное всплытие |

Объём нового кода памяти: **~1 900 строк** в 9 файлах плюс 411 строк тестов.

```
kuni/memory/schema.py         150 L   SQL-схема, миграции, версия 3
kuni/memory/episodic.py       549 L   эпизоды, граф, факты, теги
kuni/memory/retrieval.py      304 L   двухпутевой поиск, BM25, активация
kuni/memory/decay.py          130 L   кривые забывания (чистые функции)
kuni/memory/consolidation.py  339 L   сон: NREM / REM / FORGET
kuni/memory/diary.py          258 L   фасад: add / recall / flash / promote
kuni/memory/sleep.py           73 L   совместимость (SleepCycle)
kuni/memory/store.py           52 L   совместимость (старые имена)
kuni/memory/working.py         78 L   рабочая память в .md
```

---

## 1. Зачем переписывали

Исходная память страдала тремя дефектами, которые видны в коде и в данных:

**1.1. Семантическая слепота.** Единственный путь — косинус эмбеддингов:

```python
# было, kuni/memory/diary.py
ids, matrix = self.store.all_embeddings()
top = cosine_top_k(query_vec, matrix, ids, k)   # один путь, k ближайших
```

Косинус ловит лексическое сходство. Воспоминание, связанное с запросом
по смыслу жизни, но не по словам, не находилось никогда.

**1.2. Write-time regret.** Решение «запоминать или нет» принималось в момент
прихода реплики, когда значимость ещё неизвестна. Всё, что не попало в
дневник сразу, терялось навсегда.

**1.3. Граф не работал.** Фактическое состояние базы до рефакторинга:

```
diary:        18 записей
with_embedding: 6        ← 12 записей без векторов
diary_links:    0        ← ассоциативный граф пуст
```

Причина — эмбеддинги возвращали `HTTP 404` (провайдер `api.munito.ai`
не имеет эндпоинта `/embeddings`), а `add_links` вызывался только при
наличии вектора. То есть ассоциативная память существовала в коде,
но не в данных.

Плюс отсутствовало забывание: старое и мусорное конкурировали с актуальным
за одни и те же 5 слотов контекста.

---

## 2. Карта файлов и слоёв

```
                    ┌──────────────────────────────────────┐
   сообщение  ────► │  diary.py  (фасад)                   │
                    │  add / recall / flash / promote      │
                    └───────┬──────────────────┬───────────┘
                            │                  │
              ┌─────────────▼──────┐   ┌───────▼──────────────┐
              │ episodic.py        │   │ retrieval.py         │
              │ SQL: эпизоды,      │   │ двухпутевой поиск    │
              │ граф, факты, теги  │   │ (чистые функции)     │
              └───────┬────────────┘   └───────┬──────────────┘
                      │                        │
              ┌───────▼────────────┐   ┌───────▼──────────────┐
              │ schema.py          │   │ decay.py             │
              │ DDL + миграции     │   │ кривые забывания     │
              └────────────────────┘   └──────────────────────┘
                      ▲
              ┌───────┴────────────┐
              │ consolidation.py   │  ночной сон: NREM/REM/FORGET
              └────────────────────┘
```

Правило зависимостей: `decay` не знает ни о чём, `retrieval` знает только
`decay` и numpy, `episodic` знает только схему, `diary` связывает всё,
`consolidation` работает поверх `episodic`. Циклов нет.

Точка подключения к приложению — `kuni/app.py:42` (`KuniApp.__init__`):

```python
self.diary_store = EpisodicStore(self.data_dir / "memory.db")
self.diary = Diary(self.diary_store, self.embed_llm, self.bus)
self.sleep_cycle = Consolidator(
    self.diary_store, working=self.working, embedder=self.embed_llm,
    distill=self._build_distill(), bus=self.bus,
)
```

---

## 3. Схема данных

`kuni/memory/schema.py:SCHEMA_VERSION = 3`. DDL идемпотентен, миграция
добавляет недостающие колонки (`tags` появилась в версии 3).

### 3.1. `episodes` — гиппокампальный след

| Колонка | Тип | Смысл |
|---|---|---|
| `episode_id` | TEXT PK | `uuid4().hex[:12]` |
| `ts` | REAL | когда произошло |
| `text` | TEXT | сама сцена, дословно |
| `source` | TEXT | `chat` / `emotion` / `news` / `diary_save` |
| `speaker` | TEXT | кто сказал |
| `chat_id` | INTEGER | где |
| `stage` | TEXT | `labile` → `consolidated` → `archived` |
| `confidence` | REAL | −1..1, степень доверия |
| `importance` | REAL | 0..1 |
| `surprise` | REAL | 0..1 |
| `emotion_valence` | REAL | −1..1 |
| `emotion_arousal` | REAL | 0..1 |
| `embedding` | BLOB | float32, `tobytes()` |
| `tags` | TEXT | через запятую (совместимость) |
| `provenance` | TEXT | неизменяемая метка источника |
| `access_count` | INTEGER | сколько раз всплывал |
| `last_access` | REAL | когда последний раз |
| `checksum` | TEXT | sha1 первых 16 символов текста |

Индексы: `ts`, `(stage, ts)`, `(chat_id, ts)`.

Особенность: `provenance` и `checksum` неизменяемы при обновлении текста —
это защита от «отбеливания» источника при консолидации (в ресерче —
AuthMem-Bench, TMA-NM).

### 3.2. `propositions` — разделение паттернов (DG)

| Колонка | Смысл |
|---|---|
| `prop_id` | PK |
| `episode_id` | откуда выделено |
| `text` | атомарная формулировка |
| `kind` | `ENTITY_DESCRIPTION` / `THOUGHT` / `EVENT` / `FACT` / `OTHER` |
| `entities` | JSON-массив канонических имён |
| `confidence` | −1..1 |
| `embedding` | BLOB |

### 3.3. `edges` — ассоциативный граф (CA3)

| Колонка | Смысл |
|---|---|
| `src`, `dst` | узлы (episode_id или prop_id) |
| `kind` | `hebbian` / `synonymy` / `temporal` / `causal` / `prevents` |
| `weight` | вес 0..3.0 |
| `confidence` | уверенность в связи |
| `valid_from`, `valid_to` | когда связь верна (valid-time) |
| `txn_from`, `txn_to` | когда мы это узнали (transaction-time) |
| `last_reinforced` | последнее подкрепление |

PK = `(src, dst, kind)`, поэтому `INSERT OR REPLACE` не нужен — вес
обновляется через `SELECT` + `UPDATE`.

### 3.4. `facts` — семантическая память (неокортекс)

| Колонка | Смысл |
|---|---|
| `fact_id` | PK |
| `subject`, `predicate`, `object` | тройка |
| `serial` | монотонный счётчик версий |
| `confidence` | −1..1 |
| `origin_episode` | откуда факт |
| `valid_from`, `valid_to` | интервал истинности |
| `txn_from`, `txn_to` | интервал знания |

Ключевое: `serial` — не время, а счётчик. Конфликт решается
`max(serial)`, а не суждением модели (детерминизм).

### 3.5. `syn_tags` — синаптические теги (STC)

| Колонка | Смысл |
|---|---|
| `tag_id` | PK |
| `episode_id` | что помечено |
| `strength` | 0..1, балл допуска |
| `created_at`, `expires_at` | TTL (по умолчанию 7200 с) |
| `captured`, `captured_at` | захвачен ли (PRP) |

### 3.6. `meta`

Одна строка `schema_version` — для идемпотентной миграции.

---

## 4. Путь записи: как реплика становится воспоминанием

Реализация: `kuni/memory/diary.py:44` (`Diary.add`).

### 4.1. Последовательность

```
текст
 │
 ├─1─ эмбеддинг (мягкая деградация при ошибке)
 ├─2─ INSERT в episodes со stage='labile'
 ├─3─ _link_new: рёбра к похожим (cos ≥ 0.45, до 5)
 ├─4─ score_for_admission → syn_tags (TTL 2 ч)
 └─5─ если балл ≥ 0.62 → capture_tags сразу (PRP)
```

Код целиком (сокращённо, без логов):

```python
# diary.py:44
episode = Episode(
    episode_id=uuid.uuid4().hex[:12], ts=stamp, text=text,
    source=source, chat_id=chat_id, speaker=speaker, stage="labile",
    importance=importance if importance is not None else 0.4,
    surprise=max(0.0, min(1.0, surprise)),
    emotion_valence=0.0,
    emotion_arousal=max(0.0, min(1.0, emotion)),
    provenance=provenance or source,
)
if self.embedder is not None:
    try:
        episode.embedding = await self.embedder.embed_one(text)
    except Exception:
        episode.embedding = None          # не валим запись из-за сети
self.store.add_episode(episode)
if episode.embedding:
    self._link_new(episode)
tag_strength = decay_mod.score_for_admission(...)
self.store.tag(episode.episode_id, tag_strength, ttl_seconds=LABILE_TTL, now=stamp)
if promote is True or (promote is None and tag_strength >= AUTO_ADMIT):
    self.store.capture_tags([episode.episode_id], now=stamp)
```

### 4.2. Почему запись не решает «важно / неважно»

Это принципиальный отказ от старого поведения. Реплика **всегда**
сохраняется дословно в `episodes`. Отбор происходит позже, когда есть
контекст: ночью или по явному `promote()`.

Обоснование из ресерча: write-time regret — немедленная суммаризация
или отбрасывание теряет деталь необратимо, потому что значимость
проявляется только в связке с последующими событиями.

Цена решения: база растёт быстрее. Поэтому есть TTL тега (2 ч) и ночная
архивация лабильных эпизодов старше суток (`consolidation.py:280`).

### 4.3. Балл допуска

`kuni/memory/decay.py:111`:

```python
def score_for_admission(importance, surprise, emotion, novelty) -> float:
    return (0.40 * importance + 0.25 * surprise
            + 0.20 * emotion   + 0.15 * novelty)
```

Веса: важность доминирует, удивление и эмоция дают вклад, новизна —
поправка, чтобы не цементировать повторы.

`novelty` в `Diary.add` вычисляется грубо: `1.0`, если эмбеддинга нет
(значит, сравнить не с чем), иначе `0.6`. Это осознанное упрощение —
честная новизна требует расстояния до ближайшего соседа, что дороже.

Порог `AUTO_ADMIT = 0.62`. Значения по умолчанию при записи:
`importance=0.4`, `surprise=0.0`, `emotion=0.0` → балл 0.25, эпизод
остаётся лабильным и ждёт сна. Эмоциональная запись приходит с
`importance` от LLM-оценки и часто превышает порог.

### 4.4. Рёбра на входе

`diary.py:93` (`_link_new`): косинус нового эпизода ко всем имеющимся,
берутся соседи с `similarity ≥ 0.45`, максимум 5, вес `0.2 * similarity`.

Это дешёвая операция «на входе»: одна матричная строка. Дорогое
(кластеризация, дистилляция) вынесено в сон.

### 4.5. Формула Хебба

`kuni/memory/episodic.py:355` (`link`):

```
если ребро есть:  w ← min(3.0, w + Δw)
если ребра нет:   INSERT с весом Δw
```

Затухание (`episodic.py:401`):

```
w ← w · (1 − λ),  λ = 0.02
DELETE WHERE w ≤ 0.01
```

Обратите внимание: константа `HEBBIAN_RATE = 0.15` объявлена
(`episodic.py:33`), но в текущем `link()` не используется — вес приходит
аргументом от вызывающего. Это задел на будущую нормировку, а не рабочий
параметр. Честно помечаю, чтобы не выглядело реализованным.

---

## 5. Поиск: два пути

Реализация: `kuni/memory/retrieval.py:209` (`dual_path_retrieve`).

### 5.1. Формула

```
S_base(v) = 0.55·cos(q, e_v) + 0.25·BM25(q, v) + 0.20·decay(type, age)
S(v)      = S_base(v) + β · Σ_i S_base(v_i) · w_iv · damping
R_final   = Top-k(S_base) ∪ Top-m(только ассоциация)
```

где `β = 0.35`, `damping = 0.65`, `hops = 2`, `flip_m = 3`,
`min_score = 0.05` — значения по умолчанию из сигнатуры.

### 5.2. Три компонента базового скора

**Косинус** (`retrieval.py:85`):

```python
q_norm = np.linalg.norm(q)
norms = np.linalg.norm(matrix, axis=1)
return (matrix @ q) / (norms * q_norm)
```

Защита от нулевых векторов: при `q_norm == 0` возвращается нулевой массив,
а не `nan`. Это важно — у записей без эмбеддинга вектор нулевой.

**BM25-подобная лексика** (`retrieval.py:98`):

```python
idf = log(1 + (n - df + 0.5) / (df + 0.5))
score = Σ idf(token) / sqrt(len(query_tokens))
затем нормировка на максимум
```

Стоп-слова отсекаются в `tokenize` (`retrieval.py:52`), список `STOPWORDS`
включает русские служебные слова. Редкое слово весит больше частого —
именно это даёт устойчивость к шуму.

**Свежесть с учётом типа** (`retrieval.py:128`):

```python
def temporal_weight(ts, now, kind):
    return decay_mod.type_decay(max(0.0, now - ts), kind)
```

### 5.3. Распространение активации

`retrieval.py:149`:

```python
for _ in range(hops):
    for node, activation in frontier.items():
        for dst, kind, weight in neighbours.get(node, []):
            contribution = activation * weight * beta * damping
            if kind == "prevents":
                contribution = -abs(contribution)
            incoming[dst] += contribution
    frontier = {n: d for n, d in incoming.items() if d > 0}
```

Три свойства, которых не было раньше:

1. **Рёбра `prevents` вычитают** — аналог тормозных ГАМК-связей. Это
   позволяет памяти «не вспоминать» то, что противоречит текущей картине.
2. **Фронт только положительный** — отрицательная активация не
   распространяется дальше, иначе один запрет гасил бы полграфа.
3. **Damping на каждом шаге** — дальние узлы не забивают прямые совпадения.
   Без него на 2 шагах граф «размывается» в среднее по компоненте.

### 5.4. Что такое flip-путь

Ключевая часть, которой не было в старом коде:

```python
floor = <k-й по величине базовый скор>
flip = узлы, у которых S > floor, но S_base ≤ floor
```

То есть узлы, всплывшие **исключительно благодаря связи**, а не
собственному совпадению. Их метка `via = "потянуло по ассоциации"`,
и они попадают в выдачу отдельным слотом (`flip_m = 3`).

Тест, который это фиксирует — `tests/test_memory_v2.py::test_dual_path_finds_associative_recall`:

```python
ids   = ["ep_body", "ep_weather", "ep_garage"]
texts = ["тело закопано за гаражами, второе так и не нашли",
         "сегодня была отличная погода и мы гуляли",
         "гаражи молчат уже неделю, никто ничего не говорит"]
neighbours = {"ep_garage": [("ep_body", "hebbian", 1.2)]}
query = "что там с гаражами"
# ep_body обязан всплыть, хотя лексически далёк от запроса
```

Старый `cosine_top_k` этот тест провалил бы: у `ep_body` нет общих слов
с запросом.

### 5.5. Латеральное торможение

`retrieval.py:187`:

```python
for node, score in sorted(...):          # от сильного к слабому
    if любой уже принятый скор отличается меньше чем на 15%:
        подавить
    иначе принять
```

Зачем: без этого пять почти одинаковых воспоминаний об одном событии
занимают весь бюджет контекста, вытесняя то, что реально нужно.
`radius = 0.15` — доля от скора, `strength = 0.30` объявлен, но
не используется (задел на мягкое подавление вместо жёсткого).

### 5.6. Почему приближение, а не полный PageRank

Полный Personalized PageRank требует итераций по всей матрице смежности.
При 10⁴ эпизодов это секунды на запрос — неприемлемо в конвейере ответа.
Реализовано 2 шага по окрестности с `damping`, что даёт тот же эффект
локально за миллисекунды. При росте базы до 10⁵+ стоит перейти на
разреженную матрицу и 10–20 итераций — это зафиксировано в `docs/MEMORY.md`.

---

## 6. Забывание: разные типы стареют по-разному

Реализация: `kuni/memory/decay.py`. Все функции чистые, без БД и сети —
поэтому тестируются напрямую.

### 6.1. Таблицы констант

```python
# decay.py:21
TYPE_PERISHABILITY = {           # во сколько раз медленнее базового
    "EVENT": 1.0, "FACT": 3.0, "ENTITY_DESCRIPTION": 4.0,
    "THOUGHT": 0.5, "OTHER": 1.0,
}
TYPE_HALF_LIFE_HOURS = {         # базовая полужизнь
    "EVENT": 72.0,               # 3 суток
    "FACT": 24 * 30,             # 30 суток
    "ENTITY_DESCRIPTION": 24 * 60,  # 60 суток
    "THOUGHT": 12.0,             # полсуток
    "OTHER": 48.0,
}
```

Итоговая шкала = `half_life × perishability`, то есть для факта
`720 × 3 = 2160` часов.

### 6.2. Четыре модели

| Функция | Формула | Для чего |
|---|---|---|
| `actr_base_activation` | `ln(Σ (t)^−d) + K_m` | частота + недавность обращений |
| `ebbinghaus` | `exp(−t/S)` | базовая экспонента |
| `weibull` | `exp(−(t/λ)^k)` | разная форма обвала и плато |
| `fsrs_retrievability` | `(1 + t/(9S))^−1` | кривая из FSRS |

`type_decay` (`decay.py:84`) использует Вейбулл с формой по типу:
`shape = 1.4` для `FACT`/`ENTITY` (долгое плато, затем спад),
`shape = 0.9` для остальных (быстрый начальный обвал).

### 6.3. Практика замедляет забывание

`decay.py:94` (`retention`):

```python
stability_boost = 1.0 + math.log1p(len(accesses))
return min(1.0, base * stability_boost ** 0.5)
```

Логарифм, а не линейный рост: первое обращение даёт больше, чем десятое.
Корень сглаживает влияние. Это модель spacing effect — распределённое
повторение делает след устойчивее, чего чистая экспонента не умеет.

---

## 7. Сон: три фазы

Реализация: `kuni/memory/consolidation.py:71` (`Consolidator`),
точка входа `run()` на строке 85. Запускается планировщиком
(`app.py:159`, суточная задача на `[diary].consolidation_hour`).

### 7.1. Порядок фаз

```python
# consolidation.py:85
stats.captured     = await self._nrem(stamp)      # 1. захват
stats.replay_pairs = self._replay(stamp)          # 2. переигровка
await self._rem(stats, similarity_threshold, lookback_days, stamp)  # 3. дистилляция
self._forget(stats, stamp)                        # 4. забывание
stats.working_memory_dropped = self.working.cleanup()
```

Порядок не случаен: сначала нужно понять, что уже закреплено, потом
связать закреплённое, потом сжать плотные кластеры, и только затем
убирать слабое. Обратный порядок удалял бы данные до того, как они
попадут в долговременную память.

### 7.2. NREM — захват и переигровка

`_nrem` (`consolidation.py:117`): берёт все живые теги и переводит их
эпизоды в `stage='consolidated'` через `capture_tags`. Каждый захваченный
эпизод получает `touch()` — то есть обращение, усиливающее след.

`_replay` (`consolidation.py:131`): перебирает последние 64
консолидированных эпизода и связывает пары с косинусом ≥ 0.55:

```python
sim = dot(v_i, v_j) / (norm_i * norm_j)
if sim >= 0.55:
    self.store.link(ids[i], ids[j], kind="hebbian",
                    weight=0.05 * sim, confidence=sim, now=now)
```

Это модель sharp-wave ripples: во сне мозг «проигрывает» последовательности
и укрепляет связи между одновременными событиями. Вес маленький (0.05),
потому что подкрепление накопительное — за много ночей связь набирает силу.

### 7.3. REM — дистилляция

`_rem` (`consolidation.py:154`) → `_cluster` (`182`) → `_distill_cluster` (`209`).

Кластеризация жадная: берётся эпизод, к нему присоединяются соседи с
косинусом ≥ `similarity_threshold` (по умолчанию 0.6), максимум
`MAX_CLUSTER = 8`.

Дистилляция — единственное место, где сон зовёт LLM. Контракт:

```python
decision = await self.distill(cluster)
# ожидается: {"action": "merge|keep|discard",
#             "kind": "EVENT|FACT|ENTITY_DESCRIPTION|THOUGHT|OTHER",
#             "text": "сжатая формулировка"}
```

Два важных ограничения:

1. **LLM не решает, что правда.** Он формулирует короче. Что считать
   кластером, что хабом, что забывать — детерминированный код.
2. **Одиночные эпизоды не дистиллируются через LLM.** Для кластера из
   одного элемента вызывается `_maybe_archive_single` (`264`), которая
   либо оставляет, либо архивирует по `retention < 0.15 AND importance < 0.5`.

Если LLM недоступен, работает `_mechanical_merge` (`254`): склейка
самых важных фрагментов без переформулировки. Ничего не выдумывается.

### 7.4. FORGET — гомеостатическое забывание

`_forget` (`consolidation.py:280`):

```python
self.store.decay_edges(now=now)      # w *= 0.98, удаление ≤ 0.01
self.store.expire_tags(now=now)      # теги, не дождавшиеся захвата
stale = [e for e in self.store.older_than(now, stage="labile")
         if now - e.ts > 86400]      # сутки в буфере
for e in stale: set_stage("archived")
```

**Ничего не удаляется безвозвратно.** `archived` — это понижение стадии,
данные остаются в базе и доступны через `include_archived=True`.
Безвозвратного `DELETE` в памяти нет вообще, кроме явного
`delete_episode()` из API.

### 7.5. Что сон НЕ делает

- Не переиндексирует эмбеддинги (вектор остаётся тем же).
- Не трогает `facts` — семантика обновляется только через `upsert_fact`.
- Не меняет `provenance` и `checksum` — источник неизменяем.

---

## 8. Семантические факты и конфликты

Реализация: `episodic.py:418` (`upsert_fact`), `471` (`resolve_conflict`).

### 8.1. Механика

```python
prev = SELECT fact_id, serial FROM facts
       WHERE subject=? AND predicate=? AND valid_to IS NULL
       ORDER BY serial DESC LIMIT 1
if prev:
    next_serial = prev.serial + 1
    UPDATE facts SET valid_to=?, txn_to=? WHERE fact_id=prev.fact_id
else:
    next_serial = MAX(serial) + 1
INSERT новый факт
```

Результат: в любой момент времени открыт ровно один факт на пару
`(subject, predicate)`. Старые закрыты по `valid_to`, но не удалены.

### 8.2. Почему serial, а не время

Время может идти назад (часовые пояса, ретроспективные записи, импорт
истории). Счётчик монотонен по построению, поэтому «последний узнанный»
всегда побеждает независимо от системных часов. Плюс это детерминированно
тестируемо: тест задаёт `now=1000.0` и `now=2000.0`.

Тест `test_fact_resolution_by_serial` проверяет именно это: после двух
записей `resolve_conflict("Жан", "статус")` возвращает `"добрый"`,
а старый факт имеет `valid_to == 2000.0`.

### 8.3. Честное ограничение

`remember_fact` / `upsert_fact` **сейчас не вызываются из конвейера
диалога**. Проверка по коду:

```
grep remember_fact → только определение в diary.py:246
grep upsert_fact   → episodic.py:418 и тесты
```

То есть механизм готов и протестирован, но автоматического извлечения
троек `(subject, predicate, object)` из реплик ещё нет. Это следующий шаг,
и он важен для эмоций: отношения «кто к кому как относится» —
естественно факты, а не эпизоды.

---

## 9. Стык с конвейером и эмоциями

Это раздел, который важнее всего для проектирования эмоций: где именно
память трогает остальную систему.

### 9.1. Точки вызова (проверено по коду)

| Место | Строка | Что делает |
|---|---|---|
| всплытие перед ответом | `app.py:338` | `diary.flash(user_text, emotions.state, k=4)` |
| всплытие перед инициативой | `app.py:451` | `diary.flash(brief, state, k=3)` |
| запись после ответа | `app.py:705` | `_assess_emotion` → `diary.add(...)` |
| всплытие в прокси | `proxy/server.py` | `diary.recall(last_user, k=3)` |
| просмотр в дашборде | `dashboard/server.py` | `diary.stats()`, поиск по тексту |

### 9.2. Как воспоминания попадают в промпт

`app.py:338`:

```python
associations = await self.diary.flash(
    user_text or chat_label, self.emotions.state,
    k=cfg.memory.flash_k,
    fresh_window=cfg.memory.flash_fresh_window_sec,
)
system = self.persona.build(
    chat_title=chat_label, chat_type=msg.chat_type,
    associations=associations, working_memory=self.working.content(),
    thoughts=self.consume_thoughts(),
)
```

Дальше `persona.py:72` оборачивает их так:

```
Memories surfaced on their own while you read the message — involuntary,
like a scent hitting you in the middle of a street. Not a lookup, nobody
searched for anything; do not cite them as information you "found".
Let them color your reaction, mention only what would naturally slip out:
<associations>
- (2026-10-24 22:10) тело закопано за гаражами
- (2026-10-25 01:02) гаражи молчат уже неделю
</associations>
```

Формулировка принципиальна: модель не должна говорить «я нашла в памяти».
Воспоминание подаётся как непроизвольное, иначе персонаж превращается
в поисковик.

### 9.3. Три пути `flash()` — и роль эмоций

`diary.py:190`:

```python
# 1) прямой триггер: слова собеседника
for node in await self._search(stimulus, top_k=max(2, k), now=stamp):
    push(node.to_dict(), node.via or "слово зацепило")

# 2) настроение тянет своё
intensity = float(getattr(emotion, "intensity", 0) or 0)
emotion_word = str(getattr(emotion, "emotion", "") or "")
if intensity >= 0.4 and emotion_word:
    word = emotion_word.split()[0]
    for episode in self.store.search_text(word, limit=4):
        push(episode.to_dict(), f"настроение потянуло ({word})")

# 3) ассоциативная цепочка от всплывшего
for seed in list(collected[:2]):
    for neighbour_id, kind, weight in self.store.neighbours(seed_id, limit=3):
        if kind == "prevents" or weight <= 0:
            continue
        push(neighbour.to_dict(), "одно потянуло другое")
```

**Путь 2 — это уже готовая точка интеграции эмоций**, и она слабая.
Сейчас она делает буквальный `LIKE` по слову эмоции в тексте записи:

```sql
SELECT ... FROM episodes WHERE text LIKE '%грусть%'
```

То есть «настроение тянет воспоминания» реализовано как поиск слова
«грусть» в тексте, а не как поиск по эмоциональному тону. Настоящий
mood-congruent recall должен сравнивать `valence`/`arousal` записи с
текущим состоянием. Для этого в схеме уже есть колонки
`emotion_valence` и `emotion_arousal` — но, см. 9.5, они не заполняются.

### 9.4. Обратный поток: ответ → эмоция → память

`app.py:705` (`_assess_emotion`):

```python
data = await self.emotion_llm.chat_json([...])       # LLM оценивает обмен
state = self.emotions.apply_assessment(data, chat_id)  # PAD + mood
note = str(data.get("diary_note") or "").strip()
if note:
    salience = await estimate_importance(self.llm, ...)
    await self.diary.add(
        note, tags=["эмоция"], source="emotion", chat_id=chat_id,
        importance=salience["importance"],
        surprise=salience["surprise"],
        emotion=salience["emotion"],
        provenance=f"emotion:{chat_id}",
    )
```

Важно: эмоциональная оценка запускается через `asyncio.ensure_future`
(`app.py:386`), то есть **не блокирует ответ**. Пользователь получает
реплику, оценка и запись догоняют её асинхронно.

### 9.5. Честный список разрывов на стыке

Это самое важное для проектирования эмоций. Всё проверено grep'ом по коду.

| Разрыв | Факт | Следствие |
|---|---|---|
| `emotion_valence` не заполняется | при записи жёстко `0.0` (`diary.py:61`); единственные присваивания — определение и чтение | валентность эпизода всегда 0 → mood-congruent поиск по тону невозможен |
| `emotion_arousal` заполняется, но грубо | `diary.py:63`, из `estimate_importance().emotion` | это «эмоциональная заряженность», не arousal из PAD |
| путь 2 ищет по слову | `search_text(word)` — `LIKE '%грусть%'` | находит только записи, где эмоция названа словами |
| `kinds` при поиске однородны | `diary.py:157`: `kinds = [KIND_OTHER] * len(episodes)` | `type_decay` в поиске не различает факт и мысль, хотя в схеме тип есть |
| `facts` не наполняются | `remember_fact` не вызывается из конвейера | нет отношений «кто к кому как относится» |
| порог всплытия по эмоции | `intensity >= 0.4` | при 39% сильная эмоция не тянет воспоминания |

Первый пункт — блокирующий. Пока `emotion_valence` не заполняется,
любая система эмоций сможет влиять на память только через текстовый
поиск по слову и через `importance`.

---

## 10. Параметры: где крутить

Секция `[memory]` в `config.toml`, схема — `core/config.py:MemoryCfg`.

| Параметр | По умолчанию | Что делает | Когда менять |
|---|---|---|---|
| `enabled` | `true` | использовать новую память | для отладки старой базы |
| `labile_ttl_hours` | `2.0` | жизнь синаптического тега | больше — эпизоды дольше ждут сна |
| `auto_admit` | `0.62` | порог немедленного захвата | ниже — больше запоминается сразу |
| `similarity_threshold` | `0.6` | порог кластеризации во сне | выше — осторожнее сливает |
| `lookback_days` | `2.0` | окно реплея | больше — сон дольше и шире |
| `flash_k` | `4` | сколько всплывает перед ответом | больше — богаче контекст, дороже токены |
| `flash_fresh_window_sec` | `300.0` | не всплывать сразу после записи | защита от эха |
| `edge_decay` | `0.02` | затухание графа за ночь | выше — граф редеет быстрее |
| `use_llm_salience` | `true` | LLM оценивает важность | выключить, чтобы экономить вызовы |
| `use_llm_distill` | `true` | LLM сжимает кластеры | выключить — работает механическая склейка |
| `max_context_memories` | `6` | зарезервировано | сейчас не читается (см. 11) |

Константы в коде (не в конфиге): `HEBBIAN_DECAY = 0.02`,
`EDGE_MAX = 3.0`, `EDGE_MIN = 0.01`, `MAX_CLUSTER = 8`,
`AUTO_ADMIT = 0.62`, `LABILE_TTL = 7200.0`.

---

## 11. Что не подключено (полный список)

Проверено прямым поиском по коду. Перечисляю, чтобы не было иллюзий.

1. **`max_context_memories`** объявлен в `MemoryCfg`, но нигде не читается —
   фактический размер задаёт `flash_k`.
2. **`HEBBIAN_RATE`** объявлен, но в `link()` не используется.
3. **`strength`** в `lateral_inhibition` — параметр без эффекта.
4. **`kinds`** при поиске всегда `KIND_OTHER` (`diary.py:157`).
5. **`emotion_valence`** при записи всегда `0.0`.
6. **`facts`** не наполняются из диалога.
7. **`recall_sync_fallback`** используется только как ручной fallback,
   в конвейере не вызывается.
8. **`annotations`/`synonymy`** — тип ребра существует, но никем не ставится.

Ничего из этого не ломает работу: система деградирует в сторону
«проще, но честно». Но при проектировании эмоций на эти пункты
лучше опираться как на готовые заделы, а не как на работающее.

---

## 12. Тесты

`tests/test_memory_v2.py` — 411 строк, 32 теста. Разбивка:

| Группа | Что проверяет | Ключевой тест |
|---|---|---|
| Схема | все 6 таблиц, идемпотентность миграции | `test_schema_migration_is_idempotent` |
| Эпизоды | roundtrip, каскадное удаление, checksum | `test_delete_episode_cascades` |
| Граф | рост веса, потолок, затухание, `prevents` | `test_prevents_edges_are_negative_in_spreading` |
| Теги | TTL, захват, распад | `test_syn_tag_lifecycle` |
| Факты | серийное разрешение конфликтов | `test_fact_resolution_by_serial` |
| Затухание | монотонность, различие по типу, практика | `test_type_decay_differs_by_kind` |
| Поиск | токенизация, BM25, косинус, торможение | `test_lexical_scores_prefers_rare_word` |
| **Ассоциация** | всплытие по ребру, а не по словам | `test_dual_path_finds_associative_recall` |
| Дневник | запись, автозахват, recall, flash, promote | `test_diary_high_importance_promotes_immediately` |
| Сон | все фазы, LLM-дистилляция, архив | `test_consolidator_runs_all_phases` |
| Совместимость | старый API работает | `test_legacy_store_aliases_work` |

Плюс `tests/test_integration.py` (192 строки, 5 тестов) — сборка
`KuniApp` на временной директории без сети: приложение поднимается с
новой памятью, эпизод пишется, всплывает, сон консолидирует, все 23
промпта читаются.

Плюс `tests/test_regressions.py` (199 строк, 6 тестов) — закрытые баги,
включая краш на войсе с подписью.

Итого: **86 тестов**, все зелёные, прогон ~19 секунд.

```bash
pytest -q                            # всё
pytest tests/test_memory_v2.py -q    # только память
pytest tests/test_memory_v2.py::test_dual_path_finds_associative_recall -v
```

---

## 13. Что это значит для системы эмоций

Раздел адресный: как опираться на память, проектируя эмоции.

### 13.1. Готовые точки подключения

| Точка | Сигнатура | Где |
|---|---|---|
| всплытие с эмоцией | `flash(stimulus, emotion, k, fresh_window)` | `diary.py:190` |
| запись с эмоцией | `add(..., emotion=0.0, importance=..., surprise=...)` | `diary.py:44` |
| семантический факт | `remember_fact(subject, predicate, obj, confidence)` | `diary.py:246` |
| субъективное затухание | `retention(age, kind, access_times, now)` | `decay.py:94` |
| балл допуска | `score_for_admission(importance, surprise, emotion, novelty)` | `decay.py:111` |

`flash()` уже принимает объект эмоции и читает из него ровно два поля:
`intensity` и `emotion`. Если новая система эмоций даст эти два поля,
она подключится без правок памяти.

### 13.2. Три правки, которые окупятся сразу

**Правка 1: заполнять `emotion_valence`.** Сейчас `diary.py:61` жёстко
ставит `0.0`. Нужно передавать валентность из состояния:

```python
# было
emotion_valence=0.0,
# нужно
emotion_valence=max(-1.0, min(1.0, valence)),
```

и добавить `valence` в сигнатуру `Diary.add`. Без этого mood-congruent
поиск по тону физически невозможен.

**Правка 2: заменить путь 2 в `flash()` на сравнение по PAD.** Вместо
`search_text(emotion_word)` — выборка эпизодов, у которых
`abs(valence - current_valence) < threshold` при достаточном arousal.
Тогда «мне грустно» будет тянуть грустные сцены независимо от того,
названы ли они словом «грусть».

**Правка 3: передавать реальный `kind` в поиск.** `diary.py:157` ставит
всем `KIND_OTHER`, из-за чего `type_decay` не различает факт и мысль.
Нужно хранить `kind` в `episodes` (или брать из первой пропозиции) и
передавать в `dual_path_retrieve`.

### 13.3. Архитектурные выводы из реализации памяти

**Вывод 1: не смешивайте «состояние» и «событие».** Память уже разделила
их: `episodes` — что произошло, `facts` — что верно. Эмоции стоит делить
так же: вспышка (событие, живёт минуты) и настроение (состояние, живёт
часы). В текущем `Emotions` это уже сделано (`state` и `mood`), и память
к этому готова.

**Вывод 2: эмоция должна быть входом отбора, а не только выходом.**
Сейчас эмоция влияет на память только через `importance` при записи.
Правильнее — модулировать и всплытие (mood-congruent), и забывание
(эмоционально заряженное помнится дольше). Второе уже частично работает:
`retention` учитывает `access_times`, но не эмоцию.

**Вывод 3: затухание — по типу, а не глобальное.** Если вводите эмоции
как отдельный тип знания (`THOUGHT` для рефлексий, `EVENT` для сцен),
`TYPE_HALF_LIFE_HOURS` даст разное старение бесплатно. Не нужно
изобретать отдельные таймауты.

**Вывод 4: конфликты решать счётчиком, не моделью.** Правило
`max(serial)` работает и для эмоций: «Жан мне нравится» → «Жан меня
бесит» — это две версии одного факта, а не два факта. Совместите
`facts` с эмоциональными отношениями, и получите историю отношений
бесплатно.

**Вывод 5: не удаляйте эмоциональные пики.** Схема уже поддерживает
`archive` вместо `delete`. Для эмоций это критично: негативный опыт
с высокой интенсивностью может быть важен как урок, даже если
болезнен.

### 13.4. Чего избегать (грабли этой реализации)

| Грабля | Как проявилась | Как не повторить |
|---|---|---|
| Тип объявлен, но не передаётся | `kinds = [KIND_OTHER] * n` | один тест на то, что тип доходит до поиска |
| Поле в схеме, но не заполняется | `emotion_valence` всегда 0 | тест на заполнение при записи |
| Параметр в конфиге без читателя | `max_context_memories` | тест, что параметр влияет на поведение |
| «Умное» решение на входе | старый write-time regret | сначала сохранить, потом решать |
| Текстовый поиск под видом семантики | `LIKE '%грусть%'` | сравнивать числа, а не слова |

---

## 14. Как проверить всё самому

### 14.1. Структура базы

```bash
.venv\Scripts\python.exe -c "from kuni.memory.episodic import EpisodicStore; import tempfile,os; s=EpisodicStore(os.path.join(tempfile.mkdtemp(),'m.db')); [print(r[0]) for r in s.db.execute(\"select name from sqlite_master where type='table'\")]"
```

Ожидается: `edges, episodes, facts, meta, propositions, syn_tags`.

### 14.2. Ассоциативное всплытие

```bash
.venv\Scripts\python.exe -m pytest tests/test_memory_v2.py::test_dual_path_finds_associative_recall -v
```

Это тест, который доказывает главное отличие от старого kNN.

### 14.3. Все тесты

```bash
.venv\Scripts\python.exe -m pytest -q
```

Ожидается `86 passed`.

### 14.4. Живая база

```bash
.venv\Scripts\python.exe -c "from kuni.memory.episodic import EpisodicStore; from pathlib import Path; s=EpisodicStore(Path('data/memory.db')); print(s.stats())"
```

До первого запуска файла может не быть — тогда `stats()` покажет нули
после создания. После работы появится рост `entries` и `edges`.

---

## 15. Итог

Реализовано: пять уровней памяти, двухпутевой поиск с ассоциативным
всплытием, синаптическое тегирование с TTL, граф Хебба с затуханием
и тормозными рёбрами, битемпоральные факты с детерминированным
разрешением конфликтов, затухание по типу знания, трёхфазный сон
с архивом вместо удаления.

Проверено: 86 тестов, включая интеграционную сборку приложения
и регрессии на реальные баги.

Не сделано и честно помечено: заполнение `emotion_valence`, извлечение
фактов из диалога, передача типа знания в поиск, mood-congruent recall
по PAD-расстоянию.

Для эмоций это означает: каркас готов, три конкретные правки откроют
полноценную двустороннюю связь «эмоция ↔ память», и список грабель
из 13.4 стоит держать перед глазами, чтобы не воспроизвести те же
разрывы в новом модуле.

