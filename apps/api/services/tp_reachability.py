"""Достижима ли цель сделки. (#tp-reachability-2026-08-03)

Зачем
-----
Замер по 342 закрытым (`/analytics/mfe-mae`):

    scalp:  avg_mfe 0.391%   при SCALP_TARGET_PCT = 0.8

TP1 стоит вдвое дальше, чем цена в этом режиме вообще ходит. Из 79 скальпов до
TP1 не доходят порядка 82% — они закрываются тайм-стопом (24), безубыток-замком
(30) или flow-выходом (11).

Само по себе недостижение цели не смертельно: выходы её подстрахуют. Смертельно
другое — `net_rr_tp1` и `net_rr_tp2` считаются ОТ ЭТОЙ ЦЕЛИ, и гейт входа
(`min_rr_tp2 = 1.3`) пропускает сделку, опираясь на геометрию, которой не
существует. Сделка выглядит как «риск 0.4 ради 1.3», а фактически это «риск 0.4
ради 0.39, из которых заберём треть».

Как считается
-------------
Берём медиану РЕАЛИЗОВАННОГО MFE по паре символ+режим из журнала закрытых и
сравниваем с плановой дистанцией до TP1:

    reach = tp1_dist_pct / median_mfe_pct

reach ≤ 1 — цель внутри типичного хода. reach 2 — цель вдвое дальше, чем
рынок обычно даёт. Порог берётся из конфига, а медиана — из данных: подбирать
тут нечего, это факт по инструменту.

Медиана, а не среднее: MFE имеет длинный правый хвост (max 9.16% при среднем
0.816), и среднее тянется вверх редкими выбросами.

Режим
-----
`TP_REACH_MODE`: shadow (по умолчанию) | enforce. В shadow пишет вердикт в
`plan_json.tp_reach` и ничего не блокирует. Предохранитель по размеру выборки
обязателен: на трёх сделках медиана символа — это не медиана.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, asdict
from pathlib import Path

from core.config import settings

_CACHE: dict = {"ts": 0.0, "by_key": {}, "by_regime": {}}


@dataclass(frozen=True)
class TPReach:
    evaluated: bool
    allowed: bool
    reason: str
    tp1_dist_pct: float | None
    median_mfe_pct: float | None
    reach_ratio: float | None
    sample: int
    source: str

    def as_dict(self) -> dict:
        return asdict(self)


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _load_mfe(ttl_sec: float = 900.0) -> tuple[dict, dict]:
    """Медианы реализованного MFE по символ+режим и по режиму.

    Читается из журнала закрытых сделок с кешем: гейт зовут на каждом символе
    каждого прохода, а файл переписывается редко.
    """
    now = time.time()
    if now - float(_CACHE["ts"]) < ttl_sec and _CACHE["by_regime"]:
        return _CACHE["by_key"], _CACHE["by_regime"]

    raw_key: dict[tuple[str, str], list[float]] = {}
    raw_regime: dict[str, list[float]] = {}
    try:
        from services.ml_trade_logger import MLTradeLogger

        path = Path(MLTradeLogger().path)
        if path.exists():
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    lc = row.get("lifecycle") or {}
                    try:
                        mfe = float(lc.get("mfe_pct"))
                    except (TypeError, ValueError):
                        continue
                    if mfe <= 0:
                        continue
                    symbol = str(row.get("symbol") or "")
                    regime = str(row.get("regime") or "")
                    if regime:
                        raw_regime.setdefault(regime, []).append(mfe)
                        if symbol:
                            raw_key.setdefault((symbol, regime), []).append(mfe)
    except Exception:
        return _CACHE["by_key"], _CACHE["by_regime"]

    _CACHE["ts"] = now
    _CACHE["by_key"] = {k: v for k, v in raw_key.items()}
    _CACHE["by_regime"] = {k: v for k, v in raw_regime.items()}
    return _CACHE["by_key"], _CACHE["by_regime"]


def evaluate(*, symbol: str, regime: str, tp1_dist_pct: float | None) -> TPReach:
    """Достижима ли TP1 при типичном ходе этого инструмента в этом режиме.

    fail-open: нет данных — пропускаем. Отсутствие статистики не должно
    выглядеть как «цель недостижима».
    """
    if not tp1_dist_pct or float(tp1_dist_pct) <= 0:
        return TPReach(False, True, "no_tp1_distance", None, None, None, 0, "none")

    by_key, by_regime = _load_mfe()
    min_sample = int(getattr(settings, "TP_REACH_MIN_SAMPLE", 20))

    values = by_key.get((str(symbol), str(regime)))
    source = "symbol_regime"
    if not values or len(values) < min_sample:
        values = by_regime.get(str(regime))
        source = "regime"

    if not values or len(values) < min_sample:
        return TPReach(False, True, f"sample_too_small:{len(values or [])}<{min_sample}",
                       float(tp1_dist_pct), None, None, len(values or []), source)

    median_mfe = _median(values)
    if not median_mfe or median_mfe <= 0:
        return TPReach(False, True, "no_median", float(tp1_dist_pct), None, None,
                       len(values), source)

    ratio = float(tp1_dist_pct) / float(median_mfe)
    max_ratio = float(getattr(settings, "TP_REACH_MAX_RATIO", 1.5))
    within = ratio <= max_ratio

    if str(getattr(settings, "TP_REACH_MODE", "shadow")).lower() != "enforce":
        return TPReach(True, True, "mode_shadow", round(float(tp1_dist_pct), 4),
                       round(median_mfe, 4), round(ratio, 3), len(values), source)

    return TPReach(
        evaluated=True,
        allowed=within,
        reason="within_typical_move" if within else f"tp1_beyond_typical_move:{ratio:.2f}x",
        tp1_dist_pct=round(float(tp1_dist_pct), 4),
        median_mfe_pct=round(median_mfe, 4),
        reach_ratio=round(ratio, 3),
        sample=len(values),
        source=source,
    )
