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
    anhedonia_mod: float = 1.0,
) -> tuple[float, float, float]:
    """Вектор оценки -> приращения (dS, dN, dD).

    Матрица B* отличается от прежней тем, что дофамин растёт не только
    от награды, но и от агонистической мобилизации. Без этого члена
    ярость была математически недостижима: оскорбление роняло D, а зона
    ярости требует высокого D.

    `anhedonia_mod` (28.10) — множитель ТОЛЬКО для положительных вкладов
    в дофамин. По Kao et al. (2026) ангедония это отдельная ось от
    настроения: притупляется чувствительность к награде, а способность
    злиться остаётся. Поэтому похвала в эпизоде даёт в 2.5 раза меньший
    отклик, а оскорбление — прежний.
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

    # положительные вклады в D гаснут при ангедонии, отрицательные — нет
    reward = anhedonia_mod * (0.40 * w * r * pos
                              + 0.45 * w * max(0.0, vec.rpe))
    dd = (reward
          + 0.65 * w * i_agon
          - 0.55 * w * i_loss
          + 0.15 * r * vec.novelty)

    return (
        _clamp(ds * burst_gain, -ceiling, ceiling),
        _clamp(dn * burst_gain, -ceiling, ceiling),
        _clamp(dd * burst_gain, -ceiling, ceiling),
    )
