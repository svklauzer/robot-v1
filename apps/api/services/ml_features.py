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

from services.close_reasons import reached_tp2

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
    "grade_ord",         # Heuristic rank сетапа (A+=3, A=2, B=1, C=0)
    "spread_pct",        # Текущий спред на OKX (метрика издержек)
    "obi",               # Дисбаланс книги ордеров (Order Book Imbalance)
    "bid_wall_share",    # Сила лимитной поддержки покупателей
    "ask_wall_share",    # Сила лимитного сопротивления продавцов
    "cvd_ratio",         # Рыночный дисбаланс (CVD) рыночных ордеров
]

# Версия контракта. Модель, обученная на другом наборе, несовместима по длине
# и по смыслу вектора — тихо предсказывать по ней нельзя.
FEATURE_VERSION: int = 3  # Смена контракта заставит MetaLabeler сбросить старый pkl-файл!


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
    """Логированная строка ИЛИ живой кандидат → вектор фич по контракту V3."""
    d = _depth(row)
    cvd_trades = _f(d.get("cvd_trades"))
    cvd_reliable = cvd_trades >= float(CVD_MIN_TRADES)

    return [
        _grade_ord(row.get("grade")),
        _f(d.get("spread_pct")),
        _f(d.get("obi")),
        _f(d.get("bid_wall_share")),
        _f(d.get("ask_wall_share")),
        _f(d.get("cvd_ratio")) if cvd_reliable else 0.0,
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


def row_to_label(row: dict, label_kind: str = "beats_costs", min_r: float = 0.3) -> int | None:
    labels = row.get("labels") if isinstance(row.get("labels"), dict) else {}
    
    # КРИТИЧЕСКИЙ ФИКС: Если строка фантомная, она ЗАПРЕЩЕНА к маркировке как класс 1
    if is_phantom_row(row):
        return 0

    if label_kind == "hit_tp2":
        if "hit_tp2" in labels:
            return 1 if labels.get("hit_tp2") else 0
        return 1 if reached_tp2(row.get("closed_reason"), row.get("plan") or {}) else 0

    pnl = row.get("closed_net_pnl")
    if pnl is None:
        return None
    try:
        pnl = float(pnl)
    except (TypeError, ValueError):
        return None

    # Изменяем beats_costs: ориентируемся на реальный выход по тейкам или защитным прибылям
    if label_kind == "beats_costs":
        # Если сделка закрылась по стопу или это "positive_then_negative" слив — это строгий 0
        if labels.get("hit_stop") or labels.get("positive_then_negative"):
            return 0
            
        # Класс 1 только если забрали реальный TP или защитили профит (protected_profit)
        if labels.get("hit_tp2") or labels.get("protected_profit"):
            return 1
            
        risk = abs(_f(row.get("net_pnl_stop")))
        if risk <= 1e-9:
            return None
        return 1 if (pnl / risk) >= float(min_r) else 0

    if label_kind == "is_win":
        return 1 if pnl > 0 else 0

