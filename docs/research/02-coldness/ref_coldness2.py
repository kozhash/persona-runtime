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
