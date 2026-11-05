# Холодность в persona-runtime: бриф для внешнего исследователя

Дата: 2026-10-27
От: координатора проекта persona-runtime
Кому: внешнему исследователю

Этот документ — полный контекст, чтобы ты мог спроектировать модель
холодности без доступа к машине. Код приложен отдельными файлами.

---

## 1. Что за проект

`persona-runtime` — Telegram-бот с характером. Не ассистент: собственная
личность, настроение, память, обиды, желания. Общается через
**юзер-аккаунт TDLib** (не bot API), то есть выглядит как живой человек
в телеге.

Python 3.13. 17 000 строк. 110 файлов. 248 тестов.

**Персонаж меняется прямо сейчас:** была «куни, 23, андеграунд-девушка,
чёрный юмор, роастит людей», станет **персонаж, 22**, из аниме Konosuba
(архиволшебница, одержима взрывной магией, chuunibyou, пафосная речь).

Важно: **код эмоций не зависит от персонажа**. Модель, которую ты
спроектируешь, должна работать при любой личности — потому что личность
как раз меняется.

---

## 2. Постановка задачи

Владелец сказал дословно:

> «холодность не зависит от длины ответа. холодно ответить можно и
> целой книгой. как нибудь продумай данную часть»

Нужна модель, которая отличает **настоящий холод** от просто короткого
ответа, и от просто вежливого ответа.

---

## 3. Канонический пример, который ломает наивные модели

**Она** (длинно, эмоционально, с обидой):
> «Я весь день ждала, что ты напишешь, и уже начала придумывать, что ты
> меня бросил»

**Он** (длинно, вежливо, грамотно):
> «Я прочитал дважды, и оба раза мне показалось, что ты торопилась.
> Давай перепишем.»

Это **холодно**. Хотя длинно, вежливо, без мата, без оскорблений.
Почему: он не признал её состояние, а вынес вердикт её же чувствам.
Обесценил переживание, перевёл в «ты неправильно сформулировала».

**Контрпример:**

**Она:** «ты дома?»
**Он:** «ок»

Это **нормально**. Не холодно. Запрос был про информацию — информация
получена.

**И третий случай:**

**Она:** «смотри что нашла на барахолке, представляешь, там был тот
самый винил!»
**Он:** «ого, покажи! это который ты год искала?»

Это **тепло**, хотя в нём нет слов «я рядом» или «обнимаю». Есть
вовлечение в её тему.

Вывод: холод = **несоответствие отклика типу запроса**, а не длина,
не вежливость, не отсутствие тёплых слов.

---

## 4. Что уже пробовали (не повторять)

### Попытка 1: словарь «холодных» слов

```python
_COLD = ("ок", "понятно", "ясно", "как хочешь", "пофиг")
if any(w in text.lower() for w in _COLD):
    congruence = -0.45      # → проходит порог социальной боли
```

**Провал:** длина ≠ холод. «ок» после «ты дома?» — норма. Длинный
вежливый разбор её чувств — удар, но он не ловится.

### Попытка 2: три измерения

`responsiveness` (ответил ли по существу), `attunement` (признал ли
чувство), `baseline_gap` (контраст с обычной манерой человека).

**Провал:** канонический холодный ответ получал **0.09** (тепло).
Причины:
- «ждала» и «бросил» не было в словаре чувств → `has_feeling = False`
- раз чувства не было, срабатывала заглушка `answered_feeling = True`
  («нечего было признавать»)
- 14 слов против ожидаемых 5 → «ответил по существу»

То есть модель воспроизвела ровно ту ошибку, от которой уходила.

### Попытка 3: модель по Готтману — работает, но откатывается

Четыре типа запроса, у каждого своя мера отклика:

| Тип | Что требуется |
|---|---|
| question | ответ по существу (да/нет/число/информация) |
| disclosure | признание чувства + забота |
| grievance | отчёт о причине + признание + забота |
| attention | вовлечение в её тему |

Плюс различение Готтмана: поворот «от» (молчание, плоскость) слабее
поворота «против» (газлайт, вердикт, предписание).

Результаты на канонических случаях:

| Случай | Coldness |
|---|---|
| длинный вежливый разбор её чувств | **0.95** |
| «ок» на монолог | **0.70** |
| «ок» на «ты дома?» | **0.04** |
| «тебе показалось» | **0.83** |
| «я рядом, прости, телефон разрядился» | **0.18** |
| «ого, покажи!» на её находку | **0.04** |

**Владелец решил откатить эту модель и отдать другому исследователю.**
Причина не в качестве результатов — он хочет независимый взгляд.

Рабочий код: `ref_coldness2.py` (19 КБ, приложен). Можешь взять за
основу или раскритиковать.

---

## 5. Куда это встраивается (архитектура)

### 5.1 Аффект: моноамины

`kuni/affect/engine.py` — три нейромедиатора в [0,1]:

- **S** серотонин (база 0.62) — стабильность, удовлетворённость
- **N** норадреналин (база 0.38) — возбуждение, тревога
- **D** дофамин (база 0.60) — мотивация, доминирование

Решаются ОДУ методом RK4. Клиренс в 1/мин (делится на 60 при
интегрировании в секундах). Полураспад S ≈ 28 мин, N ≈ 2 мин.

Проекция в PAD:
```
P   = 1.15·(S−0.5) − 0.65·(N−0.5) + 0.75·(D−0.5)   [знак D зависит от S]
A   = −0.35·(S−0.5) + 1.35·(N−0.5) + 0.55·(D−0.5)
Dom = 0.45·(S−0.5) + 0.35·(N−0.5) + 1.10·(D−0.5)
```

### 5.2 Оценка (appraisal) — 8 измерений

`kuni/affect/appraisal.py`. По Scherer SEC / Lazarus / EMA:

```
relevance   r  [0,1]   задевает ли её лично
congruence  c  [-1,1]  помогает или мешает целям
novelty     s  [0,1]   насколько резко нарушен контекст
rpe         d  [-2,2]  ошибка предсказания награды
agency      a  [-1,1]  кто виноват: другой / случай / она
coping      p  [0,1]   может ли ответить
norm        n  [-1,1]  нарушена ли норма
ego         e  [0,1]   удар по самооценке, публичность
```

Из них три индекса:
```
I_threat = r·max(0,−c)·(0.45·a⁺ + 0.35·e + 0.20·(1−p))
I_agon   = r·max(0,−c)·a⁺·p·(0.5 + 0.5·max(0,−n))
I_loss   = r·max(0,−c)·(1−p)·(0.5 + 0.5·e)
```

И приращения:
```
ΔS = g·(0.30·w̃·r·c − 0.45·w̃·I_threat + 0.20·r·n − 0.25·w̃·r·e·max(0,−c))
ΔN = g·(0.65·w̃·I_threat + 0.45·r·s + 0.35·w̃·I_agon − 0.10·r·max(0,c))
ΔD = g·(0.40·w̃·r·max(0,c) + 0.45·w̃·max(0,rpe) + 0.65·w̃·I_agon
        − 0.55·w̃·I_loss + 0.15·r·s)
```
где `w̃ = 1 + 0.5·(w_rel − 1)`, привязанность в [0.65, 2.0].

**Ключевое:** `coping` — переключатель. Высокий (может ответить) →
`I_agon` растёт → дофамин вверх → злость. Низкий (мут, бан) →
`I_loss` растёт → дофамин вниз → беспомощность.

### 5.3 Социальная боль

`kuni/affect/social.py`. Четыре потребности по Уильямсу (TNTM):

```
need_belong  принадлежность    норма 0.75
need_esteem  самооценка         норма 0.75
need_control контроль           норма 0.75
need_meaning значимость         норма 0.75
```

Индекс боли:
```
pain = Σ ωₖ·max(0, 0.75 − Nₖ)·(0.7 + 0.3·w̃)
ω = {bel: 0.30, est: 0.30, ctrl: 0.25, mean: 0.15}
```

Стадии: `reflexive` (0–40 мин) → `reflective` (40 мин–12 ч) →
`resignation` (18–24 ч+). Затухание τ = 14 ч, сон снимает 45%.

Профили удара (порядок: belong, esteem, control, meaning):
```
muted      (−0.35, −0.40, −0.65, −0.50)   кластер power
kicked     (−0.65, −0.50, −0.70, −0.60)   кластер power
insult     (−0.30, −0.45, −0.15, −0.05)   кластер belonging
humiliation(−0.40, −0.65, −0.35, −0.20)
```

### 5.4 Руминация

`kuni/affect/social.py`, класс `RuminationEngine`. Заряд R ∈ [0,1].

Режим:
```
ρ = 1/(1 + exp(−[5.0·(S−0.42) − 3.5·(N−0.55) + 2.0·(p−0.30)]))
```
ρ < 0.5 — пережёвывание (распад 5 ч), ρ ≥ 0.5 — рефлексия (распад 1 ч).

Тонический дрейф: `dS/dt = −0.018·R·(1−ρ)` в минуту, гаснет у пола 0.15.

### 5.5 Дискретные эмоции

`kuni/character/emotions.py`. 24 эмоции, LLM возвращает JSON.
Затухание по семействам: испуг 0.5 ч, злость 1.7 ч, обида 11 ч,
грусть 16 ч. Руминация растягивает в `(1 + 4.5·R)` раз.

Согласование с телом: если `distress = 0.6·max(0,−P) + 0.4·pain ≥ 0.30`,
эмоция не может быть положительной — подменяется на негативную.

---

## 6. Что от тебя нужно

### 6.1 Критика

Разбери все три подхода (словарь, три измерения, Готтман). Что в них
наивно, что сломается на живых людях, где модели врут.

Отдельно: **почему длинный вежливый разбор её чувств холоднее короткого
«ок»** — есть ли для этого формальное основание, или это частный случай.

### 6.2 Модель из литературы

С источниками и операциональными определениями:

- **Gottman**: bids for connection, turning toward/away/against.
  Как формализовать?
- **Reis & Shaver**: perceived responsiveness — understanding,
  validation, caring. Как измеряют в психологии?
- **Williams TNTM**: кумуляция остракизма. Сколько отписок подряд
  становятся значимыми? Есть ли эмпирика?
- **Лингвистические маркеры**: что реально коррелирует с холодом?
  Lexical alignment (LSM), hedging, отсутствие вопросов, соотношение
  местоимений «я/ты», response latency. НЕ длина.

### 6.3 Формула

```
coldness(her_message, his_reply, context) -> [0, 1]
```

С обоснованием каждого веса. Обязательно:
- тип запроса определяется (или его нет)
- «отклик» измеряется по-разному для разных типов
- различение «от» (молчание) и «против» (обесценивание)
- ремонт (извинение) снимает холод
- серия отписок усиливает (один холодный — не рана)

### 6.4 Куда встроить

Варианты, между которыми надо выбрать с аргументами:

**А. В `appraisal.congruence`.** Холодность понижает congruence, что
двигает моноамины через общий конвейер. Минус: холод и грубость
смешиваются, а бьют по разным потребностям.

**Б. В `social.py` как отдельный тип события.** Холод → остракизм →
бьёт по `belong` и `meaning` (не по `esteem`, как оскорбление).
Плюс: правильно разделяет. Минус: нужен ещё один профиль.

**В. В руминацию.** Холод не бьёт сразу, а запускает пережёвывание —
«почему он так ответил». Плюс: реалистично, обида от холода зреет.
Минус: эффект отложенный.

**Г. Комбинация.** Например: холод → социальная боль (Б) + заряд
руминации (В), минуя моноамины. Или: слабый холод в appraisal,
сильный — в social.

Дай рекомендацию с обоснованием.

### 6.5 Тесты

Сценарии с ожидаемыми результатами. Обязательно включи:

1. канонический длинный холодный разбор → должен быть > 0.8
2. «ок» на «ты дома?» → должен быть < 0.2
3. «ок» на эмоциональный монолог → должен быть > 0.6
4. «ого, покажи!» на её находку → < 0.2
5. «тебе показалось» → > 0.8
6. «я рядом, прости, задержался» → < 0.3
7. три отписки подряд → серия, остракизм
8. извинение после серии → серия рвётся

---

## 7. Ограничения

- Один общий LLM-ключ. Не делай массовых параллельных вызовов.
- Модель должна работать **без LLM** — на регулярках и статистике.
  Вызов LLM на каждое сообщение слишком дорог.
- Языки: русский (основной) и английский. Мат — норма, не признак
  грубости сам по себе.
- **Личность меняется.** Не привязывайся к «куни»: она станет
  персонаж, с пафосной речью и chuunibyou. Модель должна быть
  нейтральна к характеру.

---

## 8. Формат ответа

1. Критика трёх подходов — что не так
2. Модель из литературы — компоненты, операционализация, ссылки
3. Формула с весами и обоснованием
4. Куда встроить в архитектуру (А/Б/В/Г) с аргументами
5. Тесты и ожидаемые результаты
6. Риски: где модель будет врать, что не покрывает
7. UNVERIFIED — что не удалось подтвердить

Ссылки на литературу обязательны.

---

## 9. Приложенные файлы

| Файл | Что внутри |
|---|---|
| `ref_coldness2.py` | рабочая модель по Готтману (откатывается) |
| `appraisal_SKIP.py` | модель оценки — если нужно понять, куда встраивать |
| `social_SKIP.py` | социальная боль и руминация |

(приложу по запросу, если понадобится больше)


---

# ПРИЛОЖЕНИЕ: КОД


## ref_coldness2.py — рабочая модель по Готтману (откатывается, можно взять за основу или раскритиковать)

```python
"""v2: coldness = 1 - bid-specific completion, plus turning-against.

Fix vs v1: v1 summed "absence of nice things" with one weight set for all
bids. That is wrong: a question bid is satisfied by information, a disclosure
bid by validation, a grievance by repair. v1 therefore scored "ок" after
"ты дома?" and "ок" after a monologue identically (0.58), i.e. it reproduced
the very defect it was meant to fix.

Also: turning away (silence, flatness) is capped below turning against
(contempt, invalidation) — Gottman's distinction, and it keeps a single flat
reply from maxing out the scale.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

STOP = set("""
и в во не что он на я с со как а то все она так его но да ты к у же вы за бы по только ее мне
было вот от меня еще нет о из ему теперь когда даже ну вдруг ли если уже или ни быть был него до
вас нибудь опять уж вам ведь там потом себя ничего ей может они тут где есть надо ней для мы тебя
их чем была сам чтоб без будто чего раз тоже себе под будет ж тогда кто этот того потому этого
какой совсем ним здесь этом один почти мой тем чтобы нее сейчас были куда зачем всех никогда можно
при наконец два об другой хоть после над больше тот через эти нас про всего них какая много разве
три эту моя впрочем хорошо свою этой перед иногда лучше чуть том нельзя такой им более всегда
конечно всю между
the a an and or but if then than that this these those i you he she it we they me him her them us
my your his its our their to of in on at for with from by as is are was were be been being do does
did not no yes so just very really too also about into over under out up down always never
""".split())

FUNC = set("""
я ты он она мы вы они мне меня тебя его ее нас вас их мой твой свой наш ваш
и а но да же ли не ни то что как так вот в на с со к у за по от до из о об при для без через над под
было был была были будет быть есть
i you he she we they me my your his her our their it its
a an the and or but if so not no to of in on at for with from by as is are was were be been am
""".split())

FEEL = set("""
обидно обида больно боль страшно страх тяжело тяжко устала устал усталость люблю скучаю злюсь злость
бесит радостно тревожно тревога стыдно стыд одиноко пусто хорошо спокойно тепло нежно нравится
боюсь надеюсь разочаровал разочарована предал задел задело ждала ждал жду бросил бросила кинул
одинокая ненужная невыносимо плохо грустно тоскливо виновата виноват растеряна
sad angry hurt tired lonely scared afraid happy anxious ashamed upset abandoned ignored
""".split())

ACK = ("понимаю", "представляю", "вижу", "слышу", "верю тебе", "это понятно",
       "могу себе представить", "неудивительно", "и правильно", "я бы тоже",
       "i understand", "i see", "i hear you", "that makes sense", "i believe you")

CARE = ("я рядом", "я здесь", "я с тобой", "расскажи", "я слушаю", "прости", "извини",
        "мне жаль", "мне очень жаль", "обнимаю", "хочу знать", "чем помочь", "давай подумаем",
        "ты важна", "ты важен", "я за тебя", "i'm here", "i am here", "i'm sorry",
        "tell me", "i'm listening", "i care", "hug")

INVALID = ("тебе показалось", "показалось", "не выдумывай", "не придумывай", "ты преувеличиваешь",
           "преувеличиваешь", "ничего страшного", "не о чем", "сама виновата", "сам виноват",
           "ты торопилась", "ты торопился", "опять ты", "не начинай", "ты всегда", "ты никогда",
           "накрутила", "накрутил", "проблема в тебе", "это твои проблемы",
           "придумываешь", "выдумываешь", "не обязан", "не обязана", "отчитываться",
           "устал от этих", "устала от этих", "этих сцен", "сцен", "ты сама", "ты сам",
           "you're overreacting", "you imagined", "you're too sensitive", "calm down",
           "stop being")

#: Closing the channel without insulting: "давай не будем", "не сейчас".
SHUTDOWN = ("давай не будем", "не будем сейчас", "не сейчас", "потом", "не хочу об этом",
            "закрыли тему", "хватит об этом", "устал", "устала", "нет сил", "позже",
            "not now", "let's not", "drop it", "too tired")

PRESCR = ("тебе надо", "тебе нужно", "тебе стоит", "перестань", "успокойся",
          "будь проще", "надо было", "следовало", "ты должна", "ты должен",
          "возьми себя в руки", "хватит", "перепиши", "исправь", "обсудить это с психологом",
          "you should", "you need to", "stop it", "get over it")

VERDICT = ("мне показалось", "мне кажется", "оба раза", "я прочитал", "я перечитал",
           "по порядку", "во-первых", "во-вторых", "в-третьих", "разберём",
           "формулируй", "точнее")

REPAIR = ("я был неправ", "я была неправ", "я виноват", "я виновата", "это моя вина",
          "прости", "извини", "я тебя услышал", "я тебя услышала",
          "я понял свою ошибку", "ты права", "ты прав", "i was wrong", "my fault",
          "i'm sorry", "you're right")

AFFIRM = set("да ага ок окей конечно yes yeah yep sure".split())
DENY = set("нет не no nope".split())
#: Specific, checkable causes of absence. Deliberately excludes generic
#: "на работе / работал": that is background, not an account.
ACCOUNT_STRONG = ("был на смене", "была на смене", "смена", "телефон",
                  "не мог", "не могла", "так вышло", "задержался", "задержалась",
                  "уснул", "уснула", "без сети", "не было связи", "был в дороге",
                  "был без связи", "разрядился", "не слышал", "не видел")
#: weak context — counts only together with causal language
ACCOUNT_WEAK = ("работал", "работала", "на работе", "был занят", "была занята", "ехал")

QUESTION_TAIL = ("?", "？")
EMOJI = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF]+")
HEDGE = ("наверное", "кажется", "вроде", "может быть", "типа", "как бы", "не знаю",
         "probably", "maybe", "i guess", "sort of")


def norm(s): return (s or "").strip().lower()
def tokens(s): return re.findall(r"[a-zA-Zа-яА-ЯёЁ]+", s or "")
def content_words(s): return [t for t in tokens(s) if len(t) >= 3 and t not in STOP]
def has_any(s, needles):
    """Count matches with overlap suppression.

    Naive substring counting double-counts: «мне показалось» and «показалось»
    are the same evidence, «я прочитал дважды» and «оба раза» are one
    construction. We match longest-first and blank out consumed spans.
    """
    buf = s
    hits = []
    for n in sorted(set(needles), key=len, reverse=True):
        if n and n in buf:
            hits.append(n)
            buf = buf.replace(n, "\u0000" * len(n))
    return hits


def count_hits(s, needles, cap=1.0, per=0.5):
    return min(cap, per * len(has_any(s, needles)))


def lsm(her, their):
    h, t = tokens(her), tokens(their)
    if not h or not t:
        return 0.0
    sc = []
    for w in FUNC:
        f1, f2 = h.count(w) / len(h), t.count(w) / len(t)
        if f1 == 0 and f2 == 0:
            continue
        sc.append(1.0 - abs(f1 - f2) / (f1 + f2))
    return sum(sc) / len(sc) if sc else 0.0


@dataclass(frozen=True)
class Bid:
    kind: str        # question | disclosure | grievance | attention | none
    load: float
    qtype: str = ""  # for question bids: info | feeling


@dataclass(frozen=True)
class Yield:
    answer: float
    uptake: float
    validation: float
    care: float
    engagement: float
    invalidation: float
    verdict: float
    prescription: float
    repair: float
    alignment: float
    emoji_only: bool
    account: float = 0.0
    i_ratio: float = 0.0
    you_ratio: float = 0.0
    shutdown: float = 0.0


def detect_bid(her: str) -> Bid:
    h = norm(her)
    toks = tokens(h)
    if not toks and not EMOJI.search(h):
        return Bid("none", 0.0)
    feel = has_any(h, FEEL)
    first = bool(re.search(r"\b(я|мне|меня|мой|моя|мной)\b", h))
    q = any(h.rstrip().endswith(x) for x in QUESTION_TAIL) or bool(
        re.search(r"\b(как|что|почему|зачем|где|когда|сколько|кто|во сколько)\b", h))
    # grievance: accusation/owed-behaviour about the partner, or "обидно что ты"
    grievance = bool(re.search(r"(что ты|ты не|почему ты|ты опять|ты всегда|ты никогда)", h)) \
        and bool(feel or "ушел" in h or "ушёл" in h or "не написал" in h
                 or "не позвонил" in h or "бросил" in h)
    disclosure = bool(feel) and first and not grievance
    attention = bool(re.search(r"\b(смотри|глянь|представляешь|знаешь|слушай)\b", h)) \
        or h.count("!") >= 2
    n = len(toks)
    if grievance:
        return Bid("grievance", min(1.0, 0.80 + 0.05 * min(4, len(feel))))
    if disclosure:
        return Bid("disclosure", min(1.0, 0.70 + 0.06 * min(5, len(feel))))
    if q:
        return Bid("question", 0.55, "feeling" if feel else "info")
    if attention:
        return Bid("attention", 0.35 + 0.02 * min(10, n))
    if n >= 12:
        return Bid("attention", 0.25)
    return Bid("none", 0.0)


def measure_yield(her, their, bid: Bid) -> Yield:
    h, t = norm(her), norm(their)
    ch, ct = content_words(h), content_words(t)
    toks_t = tokens(t)
    emoji_only = (bool(EMOJI.fullmatch(t or " "))) or (not toks_t and bool(t))

    # ---- answer: only for question bids. Affirmative/negative/quantity/
    #      short substantive reply all count as *answering*.
    answer = 0.0
    if bid.kind == "question":
        first_tok = toks_t[0] if toks_t else ""
        qw = set(content_words(h))
        linked = bool(qw & set(ct)) or bool(re.search(r"\d", t))
        if first_tok in AFFIRM or first_tok in DENY:
            answer = 0.9
        elif re.search(r"\d", t):
            answer = 1.0
        elif toks_t and len(toks_t) <= 5:
            answer = 0.8
        elif linked:
            answer = 0.85
        elif toks_t:
            answer = 0.25
    else:
        answer = 0.0

    # ---- uptake: does the reply reference her content (topical continuity)
    overlap = len(set(ch) & set(ct)) / max(1, min(len(ch), 6)) if ch else 0.0
    coref = 1.0 if has_any(t, ("это", "об этом", "про это", "тогда", "за это", "такое")) else 0.0
    uptake = min(1.0, 0.8 * overlap + 0.2 * coref)

    # ---- validation: names her state and attributes it to her
    ack = min(1.0, 0.5 * len(has_any(t, ACK)))
    names_her = bool(re.search(r"\b(тебе|тебя|тобой|ты|твой|твоя|у тебя)\b", t))
    named_feel = 1.0 if (has_any(t, FEEL) and names_her) else 0.0
    any_feel = 1.0 if has_any(t, FEEL) else 0.0
    negated = bool(re.search(r"\bне\s+\w{0,14}\s*(обидно|больно|страшно|тяжело|важно)\b", t))
    validation = min(1.0, 0.50 * ack + 0.50 * max(named_feel, 0.45 * any_feel))
    if negated:
        validation *= 0.4

    # ---- care
    care = min(1.0, 0.6 * len(has_any(t, CARE)))

    # ---- engagement: keeps the channel open
    engagement = 1.0 if (any(t.rstrip().endswith(x) for x in QUESTION_TAIL)
                         or has_any(t, ("расскажи", "как ты", "а ты", "что ты", "tell me"))) else 0.0

    # ---- turning against
    inv = min(1.0, 0.5 * len(has_any(t, INVALID)) + (0.35 if negated else 0.0))
    verdict = min(1.0, 0.25 * len(has_any(t, VERDICT)))
    presc = has_any(t, PRESCR)
    prescription = min(1.0, 0.5 * len(presc)) * (1.0 if validation < 0.30 else 0.25)
    rep = min(1.0, 0.55 * len(has_any(t, REPAIR)))
    acc_hits = has_any(t, ACCOUNT_STRONG)
    causal = any(x in t for x in ("потому", "так как", "из-за", "поэтому"))
    if causal:
        acc_hits = acc_hits + has_any(t, ACCOUNT_WEAK)
    # an account counts only if it is tied to the grievance: either causal
    # language, or shared content words with her message. A monologue about
    # his own hard day is self-focus, not an account.
    linked_to_her = bool(set(content_words(h)) & set(ct))
    account = 0.0
    if acc_hits:
        account = min(1.0, 0.55 * len(acc_hits))
        if not causal and not linked_to_her:
            account *= 0.75     # bare facts: weaker, but still an account
        if not causal:
            account = min(account, 0.7)

    shut = min(1.0, 0.5 * len(has_any(t, SHUTDOWN)))
    toks_all = tokens(t) or [""]
    n_all = max(1, len(toks_all))
    i_ratio = sum(1 for x in toks_all if x in ("я", "мне", "меня", "мой", "моя", "мной")) / n_all
    you_ratio = sum(1 for x in toks_all if x in ("ты", "тебе", "тебя", "твой", "твоя", "тобой")) / n_all
    return Yield(answer, uptake, validation, care, engagement, inv,
                 verdict, prescription, rep, lsm(h, t), emoji_only, account,
                 i_ratio, you_ratio, shut)


#: what each bid *demands*. Weights sum to 1.
REQS = {
    "question":    {"answer": 0.80, "uptake": 0.20},
    "disclosure":  {"validation": 0.55, "care": 0.25, "uptake": 0.20},
    "grievance":   {"account": 0.45, "validation": 0.35, "uptake": 0.20},
    "attention":   {"uptake": 0.45, "care": 0.30, "validation": 0.25},
    "none":        {},
}

#: turning away is milder than turning against (Gottman: away vs against)
AWAY_CAP = 0.60
#: a bid met at this level counts as met; below it the shortfall is billed
THETA = 0.45
W_INVAL = 0.42
W_VERDICT = 0.26
W_FRAME = 0.30
W_GAP = 0.12
W_ALIGN = 0.10
W_REPAIR = 0.30


def score_coldness(bid, y, baseline_gap=0.0, streak=0, topical_shift=0.0):
    reqs = REQS.get(bid.kind, {})
    if not reqs:
        # no bid: only contempt can be cold, and only weakly
        raw = W_INVAL * y.invalidation + W_VERDICT * y.verdict
        return round(min(0.35, raw), 3), {"mode": "no_bid"}
    comp = 0.0
    detail = {}
    for k, w in reqs.items():
        s = getattr(y, k, 0.0)
        if k == "account":
            # validation substitutes for an account up to a point
            s = max(s, 0.60 * y.validation)
        # a verdict/invalidating reply cancels whatever "answer" it gave
        if k in ("answer", "uptake", "validation", "account") and y.invalidation > 0.4:
            s *= 0.4
        comp += w * s
        detail[k] = round(w * s, 3)
    shortfall = max(0.0, THETA - comp) / THETA
    away = min(AWAY_CAP, bid.load * shortfall)
    against = (W_INVAL * y.invalidation + W_VERDICT * y.verdict
               + W_FRAME * y.prescription + 0.30 * y.shutdown)
    flat = 0.12 if y.emoji_only else 0.0
    # self-focus: a reply about the responder's inner state, not hers. Cold
    # only when NOT paired with validation; otherwise it is reciprocal
    # self-disclosure, which builds intimacy.
    self_focus = 0.0
    if bid.kind in ("disclosure", "grievance") and y.validation < 0.30:
        self_focus = 0.22 * max(0.0, min(1.0, (y.i_ratio - y.you_ratio) + 0.25))
    mod = (W_GAP * min(1.0, baseline_gap)
           + W_ALIGN * max(0.0, 0.45 - y.alignment) + flat + self_focus)
    repair = W_REPAIR * y.repair * (1.0 if bid.kind in ("grievance", "disclosure") else 0.35)
    open_bonus = 0.10 * y.engagement if bid.kind in ("disclosure", "grievance") else 0.0
    raw = away + against * (1.0 - 0.5 * y.repair) + mod - repair - open_bonus
    amp = 1.0 + 0.18 * max(0, streak)
    # soft knee: compresses the top of the scale instead of clipping it, so
    # ordering survives. knee=0.72, asymptote -> 1.0.
    x = max(0.0, raw * amp)
    knee = 0.72
    cold = x if x <= knee else knee + (1.0 - knee) * (1.0 - math.exp(-(x - knee) / (1.0 - knee)))
    if y.repair >= 0.9 and y.invalidation == 0.0:
        cold = min(cold, 0.18)
    return round(cold, 3), {"away": round(away, 3), "against": round(against, 3),
                            "frame": round(W_FRAME * y.prescription, 3),
                            "self_focus": round(self_focus, 3),
                            "shutdown": round(0.30 * y.shutdown, 3),
                            "mod": round(mod, 3), "repair": round(-repair, 3),
                            "open": round(-open_bonus, 3), "comp": round(comp, 3),
                            "short": round(shortfall, 3), **detail}


def assess(her, their, baseline_gap=0.0, streak=0, topical_shift=0.0):
    bid = detect_bid(her)
    y = measure_yield(her, their, bid)
    cold, parts = score_coldness(bid, y, baseline_gap, streak, topical_shift)
    return bid, y, cold, parts

```


## appraisal.py — модель оценки — сюда встраивается coldness

```python
"""Когнитивная оценка (appraisal): двухступенчатый конвейер SEC.

Зачем этот модуль. Раньше аффект был расщеплён на два несогласованных
контура: моноамины двигал лексический фильтр из 39 слов, а глубокая
оценка от LLM попадала только в дискретные эмоции и обратно не
возвращалась. Из-за этого сарказм, холодное обесценивание и косвенное
унижение не сдвигали S/N/D вообще: в промпте одновременно оказывались
«обида 80%» и положительная интероцепция. Здесь оба контура сводятся
в один вектор оценки, и уже он двигает моноамины.

Две ступени:

1. Быстрая (FastAppraisalExtractor, без LLM, микросекунды). Считает
   8-мерный вектор по структуре высказывания и метаданным чата. Даёт
   40% фазического импульса сразу — чтобы состояние сдвинулось до
   генерации ответа, а не после.
2. Кортикальная (merge_cortical_sec). Берёт оценку из уже существующего
   JSON-вызова эмоций и корректирует быстрый вектор. Если грубая фраза
   оказалась дружеской шуткой, поправка гасит ложный всплеск.

Вектор (Scherer SEC / Smith & Ellsworth / EMA):

    relevance           r   [0,1]   задевает ли её лично
    congruence          c   [-1,1]  помогает или мешает целям
    novelty             s   [0,1]   насколько резко нарушен контекст
    rpe                 d   [-2,2]  ошибка предсказания награды
    agency              a   [-1,1]  кто виноват: другой / случай / она
    coping              p   [0,1]   может ли ответить
    norm                n   [-1,1]  нарушена ли норма, справедливость
    ego                 e   [0,1]   удар по самооценке, публичность

Из него считаются три индекса — угроза, агонистическая мобилизация и
социальная потеря — и уже они дают приращения моноаминов. Ключевое:
гнев требует высокого coping, поэтому `I_agon` даёт рост дофамина.
Именно этого не хватало: раньше оскорбление только роняло D, и зона
ярости была недостижима математически.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

#: Демпфирование привязанности: w_rel 2.5 не должно выбивать в потолок.
def amplify(w_rel: float) -> float:
    return max(0.65, min(2.0, 1.0 + 0.5 * (max(0.3, w_rel) - 1.0)))


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


@dataclass(frozen=True)
class AppraisalVector:
    """8 измерений оценки + вес привязанности."""
    relevance: float = 0.5
    congruence: float = 0.0
    novelty: float = 0.2
    rpe: float = 0.0
    agency: float = 0.0
    coping: float = 0.5
    norm: float = 0.0
    ego: float = 0.3
    w_rel: float = 1.0

    # ------------------------------------------------------------- индексы

    def threat_index(self) -> float:
        """Угроза: умышленная агрессия, бьющая по лицу."""
        harm = max(0.0, -self.congruence)
        attr = max(0.0, self.agency)
        return _clamp(
            self.relevance * harm
            * (0.45 * attr + 0.35 * self.ego + 0.20 * (1.0 - self.coping)),
            0.0, 1.5,
        )

    def agonistic_index(self) -> float:
        """Агонистическая мобилизация: могу ответить и несправедливо.

        Это и есть источник гнева. Без высокого coping индекса нет:
        беспомощная обида уходит в социальную потерю.
        """
        harm = max(0.0, -self.congruence)
        attr = max(0.0, self.agency)
        injustice = 0.5 + 0.5 * max(0.0, -self.norm)
        return _clamp(
            self.relevance * harm * attr * self.coping * injustice, 0.0, 1.5)

    def loss_index(self) -> float:
        """Социальная потеря: отвержение без возможности ответить."""
        harm = max(0.0, -self.congruence)
        return _clamp(
            self.relevance * harm * (1.0 - self.coping)
            * (0.5 + 0.5 * self.ego), 0.0, 1.5)

    # ------------------------------------------------------------- экспорт

    def to_dict(self) -> dict[str, float]:
        return {
            "relevance": round(self.relevance, 3),
            "congruence": round(self.congruence, 3),
            "novelty": round(self.novelty, 3),
            "rpe": round(self.rpe, 3),
            "agency": round(self.agency, 3),
            "coping": round(self.coping, 3),
            "norm": round(self.norm, 3),
            "ego": round(self.ego, 3),
            "w_rel": round(self.w_rel, 3),
            "threat_index": round(self.threat_index(), 3),
            "agonistic_index": round(self.agonistic_index(), 3),
            "loss_index": round(self.loss_index(), 3),
        }

    @classmethod
    def from_llm(cls, data: dict[str, Any], w_rel: float = 1.0) -> "AppraisalVector":
        """Собрать вектор из JSON-ответа модели (поле sec или плоские ключи)."""
        sec = data.get("sec") if isinstance(data.get("sec"), dict) else data

        def num(*names: str, default: float, lo: float, hi: float) -> float:
            for name in names:
                if name in sec:
                    try:
                        return _clamp(float(sec[name]), lo, hi)
                    except (TypeError, ValueError):
                        continue
            return default

        return cls(
            relevance=num("rel", "relevance", default=0.5, lo=0.0, hi=1.0),
            congruence=num("cong", "congruence", default=0.0, lo=-1.0, hi=1.0),
            novelty=num("nov", "novelty", default=0.2, lo=0.0, hi=1.0),
            rpe=num("rpe", default=0.0, lo=-2.0, hi=2.0),
            agency=num("agency", "attr", default=0.0, lo=-1.0, hi=1.0),
            coping=num("cope", "coping", default=0.5, lo=0.0, hi=1.0),
            norm=num("norm", "norm_compatibility", default=0.0, lo=-1.0, hi=1.0),
            ego=num("ego", "ego_involvement", default=0.3, lo=0.0, hi=1.0),
            w_rel=w_rel,
        )


# ---------------------------------------------------------------- лексика

#: Прямая агрессия: посыл, унижение, угроза. Считается умышленной.
_INSULT = (
    "дура", "дурак", "тупая", "тупой", "идиот", "сука", "блядь", "ненавижу",
    "заткнись", "отстань", "бесит", "отвратительно", "мерзко", "убей",
    "сдохни", "тупица", "дебил", "кретин", "тварь", "мразь", "пошла",
    "пошёл", "пошел", "нахер", "нахуй", "нафиг", "отвали", "достала",
    "заебала", "хватит", "никчемная", "ничтожество", "позор", "стыд",
    # публичное унижение. Раньше этих слов не было, и «какая же ты
    # жалкая, всем смешно с тебя» проходило как нейтральная фраза:
    # социальная боль росла, а соматика оставалась спокойной —
    # расхождение, из-за которого она обижалась, не чувствуя обиды.
    "жалкая", "жалкий", "жалко", "смешно с тебя", "ущербная", "ущербный",
    "лохушка", "позорище", "опозорилась", "никому не нужна",
    "никто тебя не любит", "надоела",
)

#: Тепло и принятие.
_WARM = (
    "люблю", "любим", "спасибо", "молодец", "умница", "красивая", "нежная",
    "жаным", "родная", "хорошая", "обожаю", "скучал", "скучаю", "приятно",
    "рад", "рада", "лучшая", "милая", "дорогая", "солнце", "котик",
    "обнимаю", "целую", "прости", "извини", "прости меня", "люблю тебя",
)

#: Холодное обесценивание без мата: раньше вообще не считалось негативом.
_COLD = ("ясно", "ок", "окей", "ага", "угу", "мхм", "понятно", "ну да",
         "как хочешь", "не важно", "пофиг", "всё равно", "все равно")

#: Прямое обращение: повышает релевантность.
_ADDRESS = ("ты ", "тебе", "тебя", "тобой", "твой", "твоя", "твоё", "твое")

_QUESTION_TAIL = ("?", "？")


class FastAppraisalExtractor:
    """Быстрая ступень: структура фразы и метаданные, без LLM."""

    def extract_from_message(
        self,
        text: str,
        *,
        w_rel: float = 1.0,
        expected_valence: float = 0.0,
        is_reply_to_self: bool = False,
        is_mention: bool = False,
        chat_type: str = "private",
        is_muted: bool = False,
        witnesses: int = 0,
        is_cold_after_emotional: bool = False,
    ) -> AppraisalVector:
        lowered = (text or "").lower().strip()
        if not lowered:
            return AppraisalVector(w_rel=w_rel)

        addressed = (is_reply_to_self or is_mention
                     or any(marker in lowered for marker in _ADDRESS))
        relevance = 0.95 if is_reply_to_self else (
            0.85 if addressed else (0.35 if chat_type != "private" else 0.75))

        hits_insult = sum(1 for w in _INSULT if w in lowered)
        hits_warm = sum(1 for w in _WARM if w in lowered)
        hits_cold = sum(1 for w in _COLD if lowered == w or lowered.startswith(w + " "))

        # Сарказм: похвала с матом рядом или кавычки вокруг тёплого слова.
        sarcasm = 0.0
        if hits_insult and hits_warm:
            sarcasm = 0.6
        if re.search(r'["«»].*(умница|молодец|люблю).*["«»]', lowered):
            sarcasm = max(sarcasm, 0.5)

        if hits_insult:
            congruence = _clamp(-0.75 - 0.15 * min(3, hits_insult) + sarcasm * 0.3,
                                -1.0, 1.0)
        elif hits_cold and not hits_warm:
            # Односложный холодный ответ сам по себе — не оскорбление.
            # «ок» и «понятно» люди пишут постоянно, и считать это обидой
            # значит обижаться на каждое второе сообщение. Осадок остаётся
            # лёгкий (-0.25, ниже порога социальной боли), а настоящая боль
            # требует контекста: холод после эмоциональной реплики. Его
            # передаёт is_cold_after_emotional — его ставит вызывающий код,
            # который видит историю.
            congruence = -0.45 if is_cold_after_emotional else -0.25
        elif hits_warm:
            congruence = _clamp(0.55 + 0.15 * min(3, hits_warm) - sarcasm * 0.5,
                                -1.0, 1.0)
        else:
            congruence = 0.0

        # Прямая агрессия умышленна; холод — скорее дистанция, чем удар.
        if hits_insult:
            agency = 0.9
        elif hits_cold and not hits_warm:
            agency = 0.45
        elif hits_warm:
            agency = 0.2
        else:
            agency = 0.0

        # Потенциал совладания: может ли она ответить. Мут и группа с
        # чужими свидетелями его снижают, личка — почти не снижает.
        if is_muted:
            coping = 0.05
        elif chat_type != "private" and witnesses >= 2:
            coping = 0.45
        elif hits_insult:
            coping = 0.7
        else:
            coping = 0.6

        # Норма: оскорбление в личке от близкого — нарушение контракта.
        norm = 0.0
        if hits_insult:
            norm = -0.8 if w_rel >= 1.5 else -0.55
        elif hits_cold and not hits_warm:
            norm = -0.3

        # Удар по лицу: публичность и близость усиливают.
        ego = 0.3
        if hits_insult:
            ego = 0.75
        elif hits_cold and not hits_warm:
            ego = 0.5
        elif hits_warm:
            ego = 0.4
        if chat_type != "private":
            ego = min(1.0, ego * 1.35)
        if witnesses >= 2:
            ego = min(1.0, ego * (1.0 + 0.25 * math.log(1 + witnesses)))

        # Новизна: крик, длина, резкость перехода.
        letters = [c for c in text if c.isalpha()]
        caps = (sum(1 for c in letters if c.isupper()) / len(letters)) if letters else 0.0
        novelty = 0.2 + (0.35 if caps > 0.6 and len(letters) > 4 else 0.0)
        novelty = min(1.0, novelty + 0.1 * min(3, hits_insult))
        if any(lowered.endswith(q) for q in _QUESTION_TAIL):
            novelty = min(1.0, novelty + 0.1)

        rpe = _clamp(congruence * 1.2 - expected_valence, -2.0, 2.0)

        return AppraisalVector(
            relevance=relevance, congruence=congruence, novelty=novelty,
            rpe=rpe, agency=agency, coping=coping, norm=norm, ego=ego,
            w_rel=w_rel,
        )

    def extract_from_system_event(
        self, kind: str, *, w_rel: float = 1.0, witnesses: int = 0,
    ) -> AppraisalVector:
        """Системное событие: мут, бан, исключение, игнор.

        Здесь всё определено типом события, а не текстом: это не реплика,
        а изменение её положения в чате.
        """
        table = {
            "muted": dict(relevance=1.0, congruence=-0.9, novelty=0.7,
                          rpe=-1.0, agency=1.0, coping=0.05, norm=-0.7,
                          ego=0.7),
            "kicked": dict(relevance=1.0, congruence=-1.0, novelty=0.9,
                           rpe=-1.2, agency=1.0, coping=0.0, norm=-0.9,
                           ego=0.9),
            "banned": dict(relevance=1.0, congruence=-1.0, novelty=0.9,
                           rpe=-1.4, agency=1.0, coping=0.0, norm=-0.95,
                           ego=0.95),
            "ignored": dict(relevance=0.9, congruence=-0.6, novelty=0.4,
                            rpe=-0.7, agency=0.3, coping=0.2, norm=-0.4,
                            ego=0.6),
            "unmuted": dict(relevance=0.9, congruence=0.5, novelty=0.5,
                            rpe=0.4, agency=0.5, coping=0.8, norm=0.3,
                            ego=0.4),
            "apology": dict(relevance=1.0, congruence=0.8, novelty=0.4,
                            rpe=0.9, agency=-0.2, coping=0.8, norm=0.6,
                            ego=0.3),
        }
        params = table.get(kind, dict(relevance=0.5, congruence=-0.3,
                                      novelty=0.3, rpe=-0.3, agency=0.5,
                                      coping=0.3, norm=-0.2, ego=0.4))
        ego = params["ego"]
        if witnesses >= 2:
            ego = min(1.0, ego * (1.0 + 0.25 * math.log(1 + witnesses)))
        return AppraisalVector(w_rel=w_rel, ego=ego, **{
            k: v for k, v in params.items() if k != "ego"})


# ------------------------------------------------------- кортикальная ступень

def merge_cortical_sec(
    fast: AppraisalVector, llm_sec: dict[str, Any],
) -> tuple[AppraisalVector, AppraisalVector]:
    """Свести быструю оценку с кортикальной.

    Возвращает (итоговый вектор, поправка). Поправка — это разница,
    которую надо дослать вторым импульсом: если быстрый фильтр ошибся
    (принял дружескую шутку за оскорбление), она имеет обратный знак
    и гасит ложный всплеск.
    """
    cortical = AppraisalVector.from_llm(llm_sec, w_rel=fast.w_rel)
    # быстрая ступень уже дала 40% — добавляем остаток, а не заменяем
    def blend(f: float, c: float) -> float:
        return _clamp(0.4 * f + 0.6 * c, -2.0, 2.0)

    merged = AppraisalVector(
        relevance=blend(fast.relevance, cortical.relevance),
        congruence=blend(fast.congruence, cortical.congruence),
        novelty=blend(fast.novelty, cortical.novelty),
        rpe=blend(fast.rpe, cortical.rpe),
        agency=blend(fast.agency, cortical.agency),
        coping=blend(fast.coping, cortical.coping),
        norm=blend(fast.norm, cortical.norm),
        ego=blend(fast.ego, cortical.ego),
        w_rel=fast.w_rel,
    )
    correction = AppraisalVector(
        relevance=merged.relevance - 0.4 * fast.relevance,
        congruence=merged.congruence - 0.4 * fast.congruence,
        novelty=merged.novelty - 0.4 * fast.novelty,
        rpe=merged.rpe - 0.4 * fast.rpe,
        agency=merged.agency - 0.4 * fast.agency,
        coping=merged.coping - 0.4 * fast.coping,
        norm=merged.norm - 0.4 * fast.norm,
        ego=merged.ego - 0.4 * fast.ego,
        w_rel=fast.w_rel,
    )
    return merged, correction


# -------------------------------------------------- проекция в моноамины

def compute_monoamine_burst(
    vec: AppraisalVector, burst_gain: float = 0.22, ceiling: float = 0.45,
) -> tuple[float, float, float]:
    """Вектор оценки -> приращения (dS, dN, dD).

    Матрица B* отличается от прежней тем, что дофамин растёт не только
    от награды, но и от агонистической мобилизации. Без этого члена
    ярость была математически недостижима: оскорбление роняло D, а зона
    ярости требует высокого D.
    """
    w = amplify(vec.w_rel)
    r, c = vec.relevance, vec.congruence
    neg = max(0.0, -c)
    pos = max(0.0, c)
    i_threat = vec.threat_index()
    i_agon = vec.agonistic_index()
    i_loss = vec.loss_index()

    ds = (0.30 * w * r * c
          - 0.45 * w * i_threat
          + 0.20 * r * vec.norm
          - 0.25 * w * r * vec.ego * neg)

    dn = (0.65 * w * i_threat
          + 0.45 * r * vec.novelty
          + 0.35 * w * i_agon
          - 0.10 * r * pos)

    dd = (0.40 * w * r * pos
          + 0.45 * w * max(0.0, vec.rpe)
          + 0.65 * w * i_agon
          - 0.55 * w * i_loss
          + 0.15 * r * vec.novelty)

    return (
        _clamp(ds * burst_gain, -ceiling, ceiling),
        _clamp(dn * burst_gain, -ceiling, ceiling),
        _clamp(dd * burst_gain, -ceiling, ceiling),
    )

```


## social.py — социальная боль и руминация — альтернативное место встройки

```python
"""Социальная боль и остракизм: четыре потребности, три стадии.

Почему отдельный модуль. В engine.py живёт непрерывная биохимия — S/N/D
и клиренс. Социальная боль — не биохимия, а когнитивный автомат: она
переключает стадии, накапливает дефицит потребностей и решает, чего
персонаж хочет после отвержения. Смешивать это с RK4-решателем значит
потерять оба.

Теория: темпоральная модель угрозы потребностям Уильямса (TNTM, 2007).
Отвержение бьёт по четырём потребностям — принадлежность, самооценка,
контроль, значимость существования — и проходит три стадии:

    reflexive   (0–40 мин)    острая боль, самозащита, ещё не думает
    reflective  (40 мин–12 ч) выбор: вернуться или отдалиться
    resignation (18–24 ч+)    апатия, ресурс копинга исчерпан

Ключевая деталь, которой не было: мут и бан — это НЕ то же самое, что
оскорбление. При оскорблении канал открыт (coping высокий) и человек
злится. При муте канал закрыт (coping → 0) и человек чувствует
беспомощность. Отсюда разные профили удара по потребностям.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field, replace
from typing import Any

from .appraisal import AppraisalVector, amplify

#: Гомеостатическая норма каждой потребности.
NEED_NORM = 0.75

#: Веса потребностей в интегральном индексе боли.
OMEGA = {"belong": 0.30, "esteem": 0.30, "control": 0.25, "meaning": 0.15}

#: Пассивное затухание дефицита: 14 часов до половины.
TAU_SOCIAL_SEC = 14 * 3600.0

#: Ночной сон снимает 45% остаточного дефицита.
SLEEP_RELIEF = 0.45

#: Границы стадий (секунды).
REFLEXIVE_MAX_SEC = 40 * 60.0
REFLECTIVE_MAX_SEC = 12 * 3600.0
RESIGNATION_AFTER_SEC = 20 * 3600.0

#: Сколько повторов отвержения от одного лица считаем хроническим.
CHRONIC_REPEATS = 3


@dataclass(frozen=True)
class SocialPainSnapshot:
    """Состояние социальных потребностей в конкретном чате."""
    chat_id: int = 0
    need_belong: float = NEED_NORM
    need_esteem: float = NEED_NORM
    need_control: float = NEED_NORM
    need_meaning: float = NEED_NORM
    pain_index: float = 0.0
    stage: str = "none"          # none | reflexive | reflective | resignation
    stage_entered_ts: float = 0.0
    unanswered_streak: int = 0
    is_muted: bool = False
    last_event: str = ""
    repeats: int = 0             # сколько раз отвергли от одного лица
    updated_ts: float = 0.0

    # ------------------------------------------------------------ вычисления

    def deficits(self) -> dict[str, float]:
        return {
            "belong": max(0.0, NEED_NORM - self.need_belong),
            "esteem": max(0.0, NEED_NORM - self.need_esteem),
            "control": max(0.0, NEED_NORM - self.need_control),
            "meaning": max(0.0, NEED_NORM - self.need_meaning),
        }

    def recalc_pain(self, w_rel: float = 1.0) -> "SocialPainSnapshot":
        """Интегральный индекс острой боли (аналог сигнала dACC)."""
        w = amplify(w_rel)
        total = sum(OMEGA[k] * v for k, v in self.deficits().items())
        pain = max(0.0, min(1.0, total * (0.7 + 0.3 * w)))
        return replace(self, pain_index=round(pain, 4))

    def cluster(self) -> str:
        """Какой кластер потребностей поражён сильнее.

        По Уильямсу от этого зависит стратегия: если болит
        принадлежность и самооценка — человек ищет возвращения;
        если контроль и значимость — уходит в холод.
        """
        d = self.deficits()
        belonging_cluster = d["belong"] + d["esteem"]
        power_cluster = d["control"] + d["meaning"]
        return "belonging" if belonging_cluster > power_cluster else "power"

    def to_dict(self) -> dict[str, Any]:
        return {
            "chat_id": self.chat_id,
            "pain_index": round(self.pain_index, 3),
            "stage": self.stage,
            "cluster": self.cluster(),
            "needs": {
                "belong": round(self.need_belong, 3),
                "esteem": round(self.need_esteem, 3),
                "control": round(self.need_control, 3),
                "meaning": round(self.need_meaning, 3),
            },
            "deficits": {k: round(v, 3) for k, v in self.deficits().items()},
            "unanswered_streak": self.unanswered_streak,
            "is_muted": self.is_muted,
            "last_event": self.last_event,
            "repeats": self.repeats,
        }


#: Профили удара по потребностям. Порядок: belong, esteem, control, meaning.
_PROFILES: dict[str, tuple[float, float, float, float]] = {
    "muted":   (-0.35, -0.40, -0.65, -0.50),
    "kicked":  (-0.65, -0.50, -0.70, -0.60),
    "banned":  (-0.65, -0.50, -0.70, -0.60),
    "ignored": (-0.25, -0.20, -0.30, -0.45),
    "insult":  (-0.30, -0.45, -0.15, -0.05),
    "humiliation": (-0.40, -0.65, -0.35, -0.20),
    "unmuted": (0.25, 0.10, 0.45, 0.20),
    "apology": (0.30, 0.35, 0.15, 0.10),
}


class SocialPainTracker:
    """Автомат социальной боли: события, стадии, затухание, исцеление."""

    def apply_event(
        self,
        state: SocialPainSnapshot,
        vec: AppraisalVector,
        event_type: str,
        *,
        witnesses: int = 0,
        now: float | None = None,
    ) -> SocialPainSnapshot:
        stamp = now if now is not None else time.time()
        w_full = amplify(vec.w_rel)
        # Для системных событий (мут, бан, исключение) вес привязанности
        # уже заложен в сами числа профиля. Умножать их ещё и на w_rel
        # значит обнулять все четыре потребности одним событием, чего
        # по Уильямсу не происходит: урон накапливается, а не уничтожает.
        w = 1.0 + 0.25 * (w_full - 1.0)

        key = event_type if event_type in _PROFILES else "insult"
        profile = _PROFILES[key]

        # Публичность усиливает удар по самооценке.
        pub = 1.0
        if witnesses >= 2:
            pub = 1.0 + 0.25 * math.log(1.0 + witnesses)
        # Кумуляция игнора: второй раз больнее, потом потолок.
        eta = 1.0
        if key == "ignored":
            streak = state.unanswered_streak + 1
            eta = min(1.5, 0.45 * max(0, streak - 1)) or 1.0

        # Релевантность обязательна: оскорбление постороннего в группе,
        # которое её не касается, не должно бить так же, как прямое
        # обращение. Раньше профиль брался целиком, и свидетель чужого
        # скандала получал боль 0.585 — почти как пострадавший.
        rel = max(0.0, min(1.0, vec.relevance))
        belong, esteem, control, meaning = (v * rel for v in profile)

        # Насыщение: чем ниже потребность, тем слабее следующий удар.
        # Без этого три оскорбления подряд обнуляли принадлежность и
        # самооценку в ноль (0.45 x 3), и она проваливалась в пустоту,
        # хотя человек так не устроен: пятый удар подряд бьёт слабее
        # первого, потому что бить уже некуда.
        def saturate(need: float) -> float:
            # выключатель нужен для честного сравнения вариантов:
            # без него нельзя понять, что именно дало эффект
            if not getattr(self, "saturation_enabled", True):
                return 1.0
            return max(0.15, min(1.0, need / NEED_NORM))

        d_belong = belong * eta * saturate(state.need_belong) * (
            w if key not in ("unmuted", "apology") else 1.0)
        d_esteem = esteem * eta * pub * saturate(state.need_esteem) * (
            w if key not in ("unmuted", "apology") else 1.0)
        d_control = control * eta * saturate(state.need_control)
        d_meaning = meaning * eta * w * saturate(state.need_meaning)

        # Технический мут обнуляет контроль целиком.
        if key == "muted":
            d_control = min(d_control, -0.45)
        if key in ("kicked", "banned"):
            d_control = min(d_control, -0.50)

        # Потолок урона за одно событие: дальше только накопление.
        # Один бан делает больно, но не превращает человека в пустоту
        # мгновенно — иначе теряется вся динамика стадий.
        def cap(value: float) -> float:
            return max(-0.45, min(0.45, value))

        d_belong, d_esteem = cap(d_belong), cap(d_esteem)
        d_control, d_meaning = cap(d_control), cap(d_meaning)

        new = replace(
            state,
            need_belong=_clamp(state.need_belong + d_belong),
            need_esteem=_clamp(state.need_esteem + d_esteem),
            need_control=_clamp(state.need_control + d_control),
            need_meaning=_clamp(state.need_meaning + d_meaning),
            last_event=key,
            updated_ts=stamp,
            is_muted=(True if key == "muted" else
                      (False if key == "unmuted" else state.is_muted)),
            unanswered_streak=(0 if key in ("unmuted", "apology")
                              else (state.unanswered_streak + 1
                                    if key == "ignored" else 0)),
            repeats=(state.repeats + 1 if key in
                     ("muted", "kicked", "banned", "insult", "humiliation")
                     else state.repeats),
        ).recalc_pain(vec.w_rel)

        return self._advance_stage(new, stamp, vec)

    def _advance_stage(self, state: SocialPainSnapshot, stamp: float,
                       vec: AppraisalVector) -> SocialPainSnapshot:
        """Стадия по Уильямсу: reflexive -> reflective -> resignation."""
        if state.pain_index < 0.12:
            if state.stage != "none":
                return replace(state, stage="none", stage_entered_ts=stamp)
            return state

        # Хроническое отвержение: много повторов или долго без примирения.
        chronic = (state.repeats >= CHRONIC_REPEATS
                   or (state.stage_entered_ts
                       and stamp - state.stage_entered_ts > RESIGNATION_AFTER_SEC))
        if chronic and state.pain_index >= 0.2:
            if state.stage != "resignation":
                return replace(state, stage="resignation",
                               stage_entered_ts=stamp)
            return state

        if state.stage == "none":
            return replace(state, stage="reflexive", stage_entered_ts=stamp)
        return state

    def step_decay(self, state: SocialPainSnapshot, dt_sec: float,
                   *, now: float | None = None,
                   after_sleep: bool = False) -> SocialPainSnapshot:
        """Пассивное затухание и переходы стадий по времени."""
        if dt_sec <= 0:
            return state
        stamp = now if now is not None else time.time()

        # Экспоненциальное вытягивание к норме.
        factor = math.exp(-dt_sec / TAU_SOCIAL_SEC)
        if after_sleep:
            factor *= (1.0 - SLEEP_RELIEF)

        def pull(value: float) -> float:
            return NEED_NORM - (NEED_NORM - value) * factor

        new = replace(
            state,
            need_belong=_clamp(pull(state.need_belong)),
            need_esteem=_clamp(pull(state.need_esteem)),
            need_control=_clamp(pull(state.need_control)),
            need_meaning=_clamp(pull(state.need_meaning)),
            updated_ts=stamp,
        ).recalc_pain(1.0)

        # Стадия зависит от возраста, а не только от боли.
        age = stamp - (new.stage_entered_ts or stamp)
        stage = new.stage
        if new.pain_index < 0.12:
            stage = "none"
            # Кумуляция сбрасывается, когда боль ушла. Иначе три
            # случайных мута за месяц навсегда помечали чат как
            # «хроническое отвержение», и resignation включалась
            # от любого пустяка.
            new = replace(new, repeats=0)
        elif age < REFLEXIVE_MAX_SEC:
            stage = "reflexive"
        elif age < REFLECTIVE_MAX_SEC:
            stage = "reflective"
        elif age >= RESIGNATION_AFTER_SEC:
            stage = "resignation"
        else:
            stage = "reflective"

        if stage != new.stage:
            # stage_entered_ts ставим только при СМЕНЕ стадии. Раньше он
            # обновлялся на каждом тике, из-за чего стадия «омолаживалась»
            # каждые 5 минут и resignation не наступала никогда.
            return replace(new, stage=stage, stage_entered_ts=stamp)
        return new

    def repair(self, state: SocialPainSnapshot, kind: str,
               now: float | None = None) -> SocialPainSnapshot:
        """Исцеление: извинение или снятие мута."""
        stamp = now if now is not None else time.time()
        profile = _PROFILES.get("apology" if kind == "apology" else "unmuted",
                               _PROFILES["apology"])
        new = replace(
            state,
            need_belong=_clamp(state.need_belong + profile[0]),
            need_esteem=_clamp(state.need_esteem + profile[1]),
            need_control=_clamp(state.need_control + profile[2]),
            need_meaning=_clamp(state.need_meaning + profile[3]),
            is_muted=(False if kind == "unmuted" else state.is_muted),
            repeats=(0 if kind == "apology" else state.repeats),
            updated_ts=stamp,
        ).recalc_pain(1.0)
        if new.pain_index < 0.12:
            new = replace(new, stage="none", stage_entered_ts=stamp)
        return new


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


# ============================================================== руминация

#: Распад заряда: пережёвывание 5 ч до половины, рефлексия 1 ч.
LAMBDA_BROOD_PER_MIN = 0.0023
LAMBDA_REFLECT_PER_MIN = 0.0115

#: Тонический дрейф от руминации (в минуту).
RUM_DS_PER_MIN = -0.018
RUM_DN_PER_MIN = 0.024

#: Ниже этого заряда след неактивен.
RUM_MIN_CHARGE = 0.15

#: Дно, ниже которого руминация не опускает серотонин.
#: Человек не становится бесконечно несчастнее: наступает дно.
RUMMINATION_FLOOR = 0.15


@dataclass
class RuminationState:
    """След пережёвывания: заряд, режим, источник."""
    trace_id: str = ""
    chat_id: int = 0
    source_episode_id: str = ""
    trigger_summary: str = ""
    current_charge: float = 0.0
    mode_rho: float = 0.0        # <0.5 пережёвывание, >=0.5 рефлексия
    created_ts: float = 0.0
    updated_ts: float = 0.0
    resolved: str = ""           # "" | reflection | apology | archived
    author_id: int = 0
    message_id: int = 0

    @property
    def active(self) -> bool:
        return not self.resolved and self.current_charge >= RUM_MIN_CHARGE

    def mode_name(self) -> str:
        if not self.active:
            return "none"
        return "reflection" if self.mode_rho >= 0.5 else "brooding"

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "chat_id": self.chat_id,
            "trigger": self.trigger_summary[:160],
            "charge": round(self.current_charge, 3),
            "mode_rho": round(self.mode_rho, 3),
            "mode": self.mode_name(),
            "resolved": self.resolved,
            "created_ts": self.created_ts,
            "updated_ts": self.updated_ts,
        }


class RuminationEngine:
    """Заряд пережёвывания: рождение, режим, распад, дрейф в моноамины."""

    def initial_charge(self, vec: AppraisalVector, *,
                       breaker_blocked: bool = False) -> float:
        """Начальный заряд: значимость, удар по лицу, беспомощность."""
        w = amplify(vec.w_rel)
        value = (0.45 * (vec.relevance * w)
                 + 0.35 * vec.ego
                 + 0.20 * (1.0 - vec.coping)
                 + (0.25 if breaker_blocked else 0.0))
        return round(max(0.0, min(1.0, value)), 4)

    def should_spawn(self, vec: AppraisalVector, *,
                     social_pain: float = 0.0,
                     breaker_blocked: bool = False,
                     emotion_intensity: float = 0.0) -> bool:
        """Когда обида превращается в след, который живёт часами."""
        if breaker_blocked:
            return True
        if social_pain >= 0.45:
            return True
        if vec.relevance * vec.ego * max(0.0, -vec.congruence) >= 0.50:
            return True
        if emotion_intensity >= 0.65 and vec.norm <= -0.40:
            return True
        return False

    def mode_rho(self, s: float, n: float, coping: float) -> float:
        """Режим переработки: пережёвывание или рефлексия.

        Логистическая сигмоида префронтального контроля: высокий S и
        низкий N дают рефлексию, истощённый серотонин при высоком
        норадреналине — зацикливание.
        """
        z = 5.0 * (s - 0.42) - 3.5 * (n - 0.55) + 2.0 * (coping - 0.30)
        return round(1.0 / (1.0 + math.exp(-z)), 4)

    def step(self, state: RuminationState, dt_sec: float, *,
             s: float, n: float, coping: float,
             now: float | None = None) -> RuminationState:
        """Один шаг: обновить режим и снять заряд."""
        if not state.active or dt_sec <= 0:
            return state
        stamp = now if now is not None else time.time()
        rho = self.mode_rho(s, n, coping)
        rate = (LAMBDA_BROOD_PER_MIN * (1.0 - rho)
                + LAMBDA_REFLECT_PER_MIN * rho)
        charge = state.current_charge * math.exp(-rate * (dt_sec / 60.0))
        resolved = state.resolved
        if charge < RUM_MIN_CHARGE and not resolved:
            # распалось само — считаем рефлексией, если режим был высокий
            resolved = "reflection" if rho >= 0.5 else "archived"
        return replace(state, current_charge=round(charge, 4),
                       mode_rho=rho, updated_ts=stamp, resolved=resolved)

    def drift(self, state: RuminationState, dt_sec: float,
              s_current: float | None = None) -> tuple[float, float]:
        """Тонический вклад руминации в моноамины (dS, dN).

        Пережёвывание держит серотонин внизу и норадреналин наверху —
        ровно то, из-за чего обида не проходит сама.

        Но у этого есть предел. Без ограничителя дрейф сильнее клиренса
        и серотонин упирается в ноль и держится там часами: равновесие
        выходит 0.62 − 0.018/0.035 = 0.106, а с дискретными тиками и
        вовсе 0.000. Человек не становится бесконечно несчастнее —
        наступает дно, и дальше он просто живёт на дне. Поэтому ниже
        RUMMINATION_FLOOR дрейф гасится до нуля.
        """
        if not state.active or dt_sec <= 0:
            return 0.0, 0.0

        # гасим дрейф у пола: дальше опускать некуда
        suppress = 1.0
        if s_current is not None and s_current < RUMMINATION_FLOOR:
            suppress = 0.0
        elif s_current is not None and s_current < RUMMINATION_FLOOR + 0.10:
            suppress = (s_current - RUMMINATION_FLOOR) / 0.10

        if suppress <= 0:
            return 0.0, 0.0

        minutes = dt_sec / 60.0
        rho = state.mode_rho
        ds = (RUM_DS_PER_MIN * state.current_charge * (1.0 - rho)
              * minutes * suppress)
        dn = (RUM_DN_PER_MIN * state.current_charge
              * (1.0 - 0.5 * rho) * minutes * suppress)
        return ds, dn

    def apology_relief(self, state: RuminationState,
                       now: float | None = None) -> RuminationState:
        """Извинение снимает 60% заряда, но не всё: тяжёлая обида инерционна."""
        if not state.active:
            return state
        stamp = now if now is not None else time.time()
        charge = state.current_charge * 0.40
        return replace(state, current_charge=round(charge, 4),
                       updated_ts=stamp,
                       resolved=("apology" if charge < RUM_MIN_CHARGE
                                 else state.resolved))

```
