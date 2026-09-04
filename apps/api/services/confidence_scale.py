"""Шкала «в пользу ЭТОЙ сделки» вместо шкалы «бычьести»
(#structure-mirror-2026-09-04).

`_score_context` выдаёт оценки по бычьей шкале: trend_up = 75, trend_down = 25.
Для шорта это перевёрнуто, поэтому trend и momentum давно зеркалятся. А
structure — нет, хотя считается ровно так же: «хорошо для long, когда цена
ближе к support» = 70, у сопротивления = 40. Для шорта это обратно: вход прямо
над поддержкой — худшее место, а получал за него +.

Цена ошибки соизмерима с расстоянием между грейдами. Размах structure 40..70 —
30 пунктов; в базовой уверенности это вес 0.20, то есть 6 пунктов, и ещё 7.5
пункта в setup_score через множитель 0.25. Разрыв порогов A и B — 7 пунктов
setup и 2 пункта confidence.

Почему модуль отдельный
-----------------------
Формула жила в ДВУХ копиях — в `_build_multi_timeframe_candidate` и в
`_intelligence_effective_confidence`, причём вторая честно писала в docstring
«mirror the formula in ...». Правка зеркала, положенная в одну копию, дала бы
две разные уверенности у одного сигнала. Копии сведены сюда до правки, а не
после.

Оговорка
--------
Зеркалится ВЫХОД, как у trend и momentum, а не пороги внутри `_score_context`:
полосы там нарезаны под лонг (0.10..0.45 от поддержки → 70), поэтому шортовая
шкала выходит сжатой (30/50/60 вместо 40/50/70). Это правит знак, но не
калибровку. Калибровать будет чем: `setup_quality` с этого дня пишется в
plan_json, и разбор по стороне покажет, осталась ли разница.
"""
from __future__ import annotations

# Веса композита. Сумма = 1.0.
WEIGHTS: dict[str, float] = {
    "trend": 0.30,
    "momentum": 0.20,
    "volume": 0.20,
    "structure": 0.20,
    "volatility": 0.10,
}

# Оценки, у которых «хорошо» зависит от стороны сделки. volume и volatility
# сюда НЕ входят: сильный объём и рабочая волатильность одинаково хороши для
# обеих сторон, зеркалить их значило бы называть хорошее плохим.
DIRECTIONAL: tuple[str, ...] = ("trend", "momentum", "structure")

_SIDES = ("long", "short")


def oriented(scores: dict, action: str) -> dict[str, float]:
    """Оценки в шкале «больше = благоприятнее для сделки с этим action».

    Для long — как есть. Для short направленные оценки отражаются (100 − x).
    Для всего остального (hold и прочее) — как есть: зеркалить нечего.
    """
    act = str(action or "").lower()
    out: dict[str, float] = {}
    for key in WEIGHTS:
        value = _num(scores.get(key) if isinstance(scores, dict) else None)
        out[key] = 100.0 - value if (act == "short" and key in DIRECTIONAL) else value
    return out


def confidence_base(scores: dict, action: str) -> float:
    """Взвешенная база уверенности по шкале стороны сделки."""
    directional = oriented(scores, action)
    return round(sum(directional[key] * weight for key, weight in WEIGHTS.items()), 2)


def structure_for_side(scores: dict, action: str) -> float:
    """Оценка структуры в шкале стороны — для setup_quality.

    Отдельная функция, а не обращение к `oriented(...)["structure"]` на месте
    вызова: так видно в grep, что у setup_quality та же шкала, что у базы, и
    следующая правка не заденет одну половину.
    """
    return oriented(scores, action)["structure"]


def _num(value) -> float:
    try:
        if value is None:
            return 50.0
        return float(value)
    except (TypeError, ValueError):
        return 50.0


# ── Калибровка по качеству сетапа ───────────────────────────────────────────
# (#confidence-ratchet-2026-09-04) Было: max(base, setup_score × K). Операция,
# которая умеет только ПОВЫШАТЬ. Два измерения одного и того же расходятся —
# берём большее; сетап, где рынок говорит 45, а чек-лист 76, получал 70 и грейд
# A. Расхождение обязано понижать уверенность, а не повышать её.
#
# Этим же объясняется странность в замере: гейт для A строже (setup 65 против
# 58, confidence 62 против 60), а ведро A БОЛЬШЕ — 53 сделки против 44. Строгий
# фильтр не может пропускать больше мягкого. Заталкивал наверх храповик.
#
# Стало: среднее двух ног. Сильный чек-лист по-прежнему поднимает слабую базу —
# ради этого механизм и вводился, — но слабый чек-лист теперь и опускает
# сильную базу, чего он не мог вовсе. Свободных параметров не добавилось.
#
# Копий было ТРИ, и они расходились: боевой путь (robot_loop) не имел ветки
# approve≥62 и умножал на 0.90, скан (main) имел и умножал на 0.92 с потолком
# 80. То есть скан показывал владельцу не то число, по которому робот торгует.
# Сведено к боевой форме: показания обязаны совпадать с решением.
from dataclasses import dataclass

from core.config import settings


@dataclass(frozen=True)
class Calibration:
    base: float
    setup_score: float
    setup_decision: str
    branch: str
    setup_leg: float | None      # нога чек-листа после множителя
    cap: float | None
    effective: float

    def as_dict(self) -> dict:
        return {
            "base": self.base,
            "setup_score": self.setup_score,
            "setup_decision": self.setup_decision,
            "branch": self.branch,
            "setup_leg": self.setup_leg,
            "cap": self.cap,
            "effective": self.effective,
            # Расхождение ног. Пишется отдельно, потому что это и есть величина,
            # которую храповик игнорировал: следующий разбор стопов проверит,
            # предсказывает ли САМО расхождение исход.
            "leg_gap": (round(self.setup_leg - self.base, 2)
                        if self.setup_leg is not None else None),
        }


_BRANCHES: tuple[tuple[str, str, float, float, float], ...] = (
    # (имя, требуемое решение, минимальный setup_score, множитель, потолок)
    ("approve_strong", "approve", 70.0, 0.90, 88.0),
    ("wait_moderate",  "wait",    55.0, 0.75, 72.0),
)


def calibrate(base: float, setup_score, setup_decision: str) -> Calibration:
    base = _num(base) if base is not None else 0.0
    score = float(setup_score or 0.0)
    decision = str(setup_decision or "")

    for name, need_decision, min_score, mult, cap in _BRANCHES:
        if decision == need_decision and score >= min_score:
            leg = round(score * mult, 2)
            blended = (base + leg) / 2.0 if _symmetric() else max(base, leg)
            return Calibration(
                base=round(base, 2), setup_score=round(score, 2),
                setup_decision=decision, branch=name, setup_leg=leg, cap=cap,
                effective=round(min(blended, cap), 2),
            )

    return Calibration(
        base=round(base, 2), setup_score=round(score, 2), setup_decision=decision,
        branch="base_only", setup_leg=None, cap=None, effective=round(base, 2),
    )


def _symmetric() -> bool:
    """Аварийный откат к односторонней форме.

    Правка меняет ВЫБОРКУ входов, а не их обработку: уверенность в среднем
    падает, значит часть сигналов перестанет проходить PROD_GATE. Проверить это
    на старых данных нельзя — поэтому переключатель, который возвращает прежнее
    поведение без деплоя, если поток входов схлопнется.
    """
    return bool(getattr(settings, "CONFIDENCE_SYMMETRIC_BLEND", True))


def confidence_calibration(result) -> Calibration:
    """Калибровка прямо по объекту скана — чтобы обе точки вызова доставали
    ноги ОДИНАКОВО, а не каждая по-своему."""
    setup_quality = getattr(result, "setup_quality", None)
    if not isinstance(setup_quality, dict):
        setup_quality = {}
    return calibrate(
        base=getattr(result, "confidence_hint", None) or 0.0,
        setup_score=setup_quality.get("final_score"),
        setup_decision=setup_quality.get("decision") or getattr(result, "setup_decision", "") or "",
    )
