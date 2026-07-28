"""ML feature contract — единый источник правды по признакам мета-лейблера.

Превращает И логированную строку trade_outcomes.jsonl (для обучения), И живого
кандидата из robot_loop (для предсказания) в ОДИН и тот же числовой вектор.
Любое расхождение train/serve — главный источник тихих багов в ML, поэтому
извлечение признаков живёт в одном месте.

Признаки берём ТОЛЬКО те, что есть и в логах, и у живого кандидата.

## Что пересмотрено 28.07 и почему (#ml-rework-2026-07-28)

**Метка была той же ловушкой, что и win-rate.** `is_win = closed_net_pnl > 0`
учит модель максимизировать ЧИСЛО побед. Наш собственный замер: 67% побед при
payoff 0.11 — убыточная система. Модель, обученная на `is_win`, отбирала бы
сетапы с частыми мелкими плюсами, то есть усиливала бы ровно ту патологию,
которую мы весь день лечим. Метка по умолчанию — `beats_costs`: сделка вернула
хотя бы `ML_LABEL_MIN_R` риска. Цель — ожидание, а не hit-rate.

**Две фичи стали константами.** `is_trend_up` / `is_trend_down` после
отключения убыточных режимов всегда 0: 154 записи из 287 — из мира, которого
больше нет. Константа не несёт информации, но участвует в регуляризации и
размывает веса живых признаков. Убраны.

**Не хватало того, что оказалось решающим.** Весь разбор дня свёлся к
издержкам и к дистанции стопа (она задаёт и риск, и размер), а в векторе их не
было вовсе. Добавлены: `stop_distance_pct`, `rr_asymmetry`, `notional_usdt`,
`hour_of_day`, `is_scalp`.
"""
from __future__ import annotations

from typing import Any

# (#audit-ml-cvd) CVD из окна с горсткой сделок — шум (cvd_ratio схлопывается в
# ±1.0 при 1–2 сделках; в live так почти всегда). Ниже порога зануляем CVD-фичи
# — ОДИНАКОВО в train и serve, иначе train/serve skew.
#
# (#cvd-noise-2026-07-28) Согласовано с депт-гейтом: там порог поднят с 1 до 8
# после того, как выяснилось, что при n=1 ratio равен ровно ±1.000 всегда.
CVD_MIN_TRADES: int = 10

# Порядок ВАЖЕН и фиксирован — модель обучается и предсказывает по нему.
# При изменении списка старая модель становится несовместимой: см.
# FEATURE_VERSION ниже, по нему обучение отбраковывает устаревшие артефакты.
FEATURE_NAMES: list[str] = [
    "confidence",
    "grade_ord",            # A+=3 A=2 B=1 C=0
    "side_is_short",        # 1 short / 0 long
    "net_rr_tp1",
    "net_rr_tp2",
    # Асимметрия наград: RR до TP2 относительно TP1. Высокая = сетап рассчитан
    # на runner, низкая = вся надежда на быструю фиксацию. Разные исходы.
    "rr_asymmetry",
    # Дистанция стопа в % от входа. Задаёт и риск, и размер позиции
    # (qty = risk / distance), и вероятность быть выбитым шумом. В прежнем
    # векторе её не было вовсе — при том, что вокруг неё крутился весь разбор.
    "stop_distance_pct",
    # Размер позиции: одна и та же ошибка на 500 и на 50 стоит по-разному, а
    # издержки почти пропорциональны нотионалу.
    "notional_usdt",
    # Час суток UTC. Ликвидность и спред меняются по сессиям; издержки — тоже.
    "hour_of_day",
    # Оставшиеся торгуемые режимы. is_trend_up/is_trend_down убраны: после
    # отключения они константные нули.
    "is_crt",
    "is_reversal",
    "is_scalp",
    "spread_pct",
    "obi",
    "bid_wall_share",
    "ask_wall_share",
    "cvd_ratio",
    "cvd_trades",
]

# Версия контракта. Модель, обученная на другом наборе, несовместима по длине
# и по смыслу вектора — тихо предсказывать по ней нельзя.
FEATURE_VERSION: int = 2


def _f(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _grade_ord(grade: Any) -> float:
    return {"A+": 3.0, "A": 2.0, "B": 1.0, "C": 0.0}.get(str(grade or "").upper(), 1.0)


def _depth(row: dict) -> dict:
    d = row.get("entry_depth")
    return d if isinstance(d, dict) else {}


def _entry_price(row: dict) -> float:
    """Цена входа: из lifecycle (лог) либо из середины зоны (живой кандидат)."""
    lc = row.get("lifecycle") if isinstance(row.get("lifecycle"), dict) else {}
    px = _f(lc.get("entry_price"))
    if px > 0:
        return px
    zone = row.get("entry_zone")
    if isinstance(zone, dict):
        lo, hi = _f(zone.get("from")), _f(zone.get("to"))
        if lo > 0 and hi > 0:
            return (lo + hi) / 2.0
    if isinstance(zone, (list, tuple)) and len(zone) >= 2:
        lo, hi = _f(zone[0]), _f(zone[1])
        if lo > 0 and hi > 0:
            return (lo + hi) / 2.0
    return 0.0


def _hour_of_day(row: dict) -> float:
    """Час UTC открытия. Для живого кандидата — текущий час."""
    raw = row.get("opened_at") or row.get("created_at")
    if raw:
        s = str(raw)
        # ISO-строка: часы стоят после первого пробела или 'T'.
        for sep in ("T", " "):
            if sep in s:
                try:
                    return float(int(s.split(sep, 1)[1][:2]))
                except (ValueError, IndexError):
                    break
    from datetime import datetime, timezone as _tz

    return float(datetime.now(_tz.utc).hour)


def row_to_features(row: dict) -> list[float]:
    """Логированная строка ИЛИ живой кандидат → вектор фич (порядок FEATURE_NAMES)."""
    regime = str(row.get("regime") or "").lower()
    side = str(row.get("side") or row.get("action") or "").lower()
    d = _depth(row)
    cvd_trades = _f(d.get("cvd_trades"))
    cvd_reliable = cvd_trades >= float(CVD_MIN_TRADES)

    rr1 = _f(row.get("net_rr_tp1"))
    rr2 = _f(row.get("net_rr_tp2"))

    entry = _entry_price(row)
    stop = _f(row.get("stop_price"))
    stop_dist_pct = abs(entry - stop) / entry * 100.0 if entry > 0 and stop > 0 else 0.0

    return [
        _f(row.get("confidence"), 60.0),
        _grade_ord(row.get("grade")),
        1.0 if side in ("short", "sell") else 0.0,
        rr1,
        rr2,
        rr2 / rr1 if rr1 > 1e-9 else 0.0,
        stop_dist_pct,
        _f(row.get("required_margin")),
        _hour_of_day(row),
        1.0 if "crt" in regime else 0.0,
        1.0 if "reversal" in regime else 0.0,
        1.0 if "scalp" in regime else 0.0,
        _f(d.get("spread_pct")),
        _f(d.get("obi")),
        _f(d.get("bid_wall_share")),
        _f(d.get("ask_wall_share")),
        _f(d.get("cvd_ratio")) if cvd_reliable else 0.0,
        cvd_trades,
    ]


def is_phantom_row(row: dict) -> bool:
    """Строка с фантомным филлом: результат выше пика, которого сделка достигала.

    (#phantom-fill) Такие исходы — фикция: цена выхода бралась из экономического
    порога, а не с рынка. В датасете их 13 из 287, и метка `is_win` у них
    положительная там, где рынок дал минус. Учить на них — учить модель, что
    определённый сетап приносит +8.18 USDT, которых не было.
    """
    lc = row.get("lifecycle") if isinstance(row.get("lifecycle"), dict) else {}
    try:
        return float(row["result_pct"]) > float(lc["mfe_pct"]) + 1e-9
    except (KeyError, TypeError, ValueError):
        return False


def row_to_label(row: dict, label_kind: str = "beats_costs",
                 min_r: float = 0.3) -> int | None:
    """Метка из логированного исхода. None — если строка ещё без исхода.

    `beats_costs` (по умолчанию) — сделка вернула хотя бы `min_r` риска.
    Именно этот вопрос имеет смысл задавать фильтру входов: не «будет ли плюс»,
    а «оправдает ли сделка потраченный риск и издержки».

    `is_win` оставлен для сравнения и обратной совместимости, но как цель
    обучения он воспроизводит ошибку win-rate: 67% побед при payoff 0.11 —
    убыточная система.
    """
    labels = row.get("labels") if isinstance(row.get("labels"), dict) else {}

    if label_kind == "hit_tp2":
        if "hit_tp2" in labels:
            return 1 if labels.get("hit_tp2") else 0
        return 1 if str(row.get("closed_reason")) == "tp2_reached" else 0

    pnl = row.get("closed_net_pnl")
    if pnl is None:
        return None
    try:
        pnl = float(pnl)
    except (TypeError, ValueError):
        return None

    if label_kind == "is_win":
        if "is_win" in labels:
            return 1 if labels.get("is_win") else 0
        return 1 if pnl > 0 else 0

    # beats_costs: результат в единицах риска. Риск — плановый убыток по стопу,
    # он уже посчитан с издержками (net_pnl_stop) и потому сравним со сделками
    # разного размера.
    risk = abs(_f(row.get("net_pnl_stop")))
    if risk <= 1e-9:
        # Без плана риска судить не о чем: строка не участвует в обучении.
        # Молча подставлять «плюс/минус» здесь нельзя — это и есть тот самый
        # тихий перекос выборки.
        return None
    return 1 if (pnl / risk) >= float(min_r) else 0
