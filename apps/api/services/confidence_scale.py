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
