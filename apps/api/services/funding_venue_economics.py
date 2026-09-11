"""Экономика фандинг-арбитража по биржам (#okx-funding-2026-09-12).

Контуры выключены 04.09 по экономике: доходность признана недостижимой, капитал
морозился. Возвращать их — только по пересчёту на наблюдениях. Этот отчёт
считает его по журналу ставок (цикл наблюдения пишет HTX и OKX раз в час):

Внутрибиржевой хедж (спот лонг + своп шорт на одной бирже)
    Доход — ставка фандинга за период по консервативной оценке (нижний
    квартиль наблюдавшихся), издержки — круг двух ног: (спот + своп) × 2.
    Безубыточный срок — во сколько периодов ставка окупит круг. У OKX спот
    дешевле HTX (тейкер 0.1% против 0.2%), и это прямо бьёт по сроку.

Межбиржевой (своп шорт там, где ставка выше, своп лонг там, где ниже)
    Доход — разница ставок двух бирж за один и тот же час, издержки — круг
    четырёх своп-ног. HTX↔OKX — единственная пара, где обе ноги торгуемые:
    Kraken у нас только на чтение. Ставки сравниваются в одном периоде (8 ч у
    обеих); Kraken с часовой каденцией сюда без пересчёта не годится.

Ничего не открывает и не меняет — только показания.
"""
from __future__ import annotations

from collections.abc import Callable
from itertools import combinations

from core.config import settings
from services.funding_rate_history import _dedupe_scan_writes, _load, _percentile, stability

FeeFn = Callable[[str, str, str], tuple[float, str]]


def _cost_engine_fee(venue: str, symbol: str, market_type: str) -> tuple[float, str]:
    from services.cost_engine import CostEngine

    return CostEngine(exchange=venue).fee_rate(symbol, market_type)


def _spot(symbol: str) -> str:
    return symbol.split(":", 1)[0]


def _swap(symbol: str) -> str:
    spot = _spot(symbol)
    quote = spot.split("/", 1)[1] if "/" in spot else "USDT"
    return f"{spot}:{quote}"


def _intra(venue: str, symbol: str, window_hours: float, hold: int, fee: FeeFn) -> dict:
    st = stability(_spot(symbol), window_hours, venue=venue)
    spot_fee, spot_src = fee(venue, _spot(symbol), "spot")
    swap_fee, swap_src = fee(venue, _swap(symbol), "swap")
    round_trip = (spot_fee + swap_fee) * 2 * 100
    cons = float(st.get("conservative_rate_pct") or 0.0)
    return {
        "venue": venue,
        "symbol": _spot(symbol),
        "observations": int(st.get("observations") or 0),
        "span_hours": st.get("span_hours"),
        "mean_rate_pct": st.get("mean_rate_pct"),
        "conservative_rate_pct": st.get("conservative_rate_pct"),
        "sign_consistency": st.get("sign_consistency"),
        "mean_basis_pct": st.get("mean_basis_pct"),
        "spot_taker": spot_fee,
        "swap_taker": swap_fee,
        "fee_source": f"{spot_src}/{swap_src}",
        "round_trip_pct": round(round_trip, 4),
        # Сколько 8-часовых периодов окупают круг по консервативной ставке.
        "break_even_periods": round(round_trip / cons, 1) if cons > 0 else None,
        "net_over_hold_pct": round(cons * hold - round_trip, 4)
        if st.get("observations") else None,
    }


def _hourly(symbol: str, venue: str, window_hours: float) -> dict[int, float]:
    """Ставка по часам: последнее наблюдение в каждом часу."""
    rows = _dedupe_scan_writes(_load(_spot(symbol), window_hours, venue))
    out: dict[int, float] = {}
    for row in rows:
        try:
            out[int(float(row["ts"]) // 3600)] = float(row["r"])
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _cross(a: str, b: str, symbol: str, window_hours: float, hold: int, fee: FeeFn) -> dict:
    ra, rb = _hourly(symbol, a, window_hours), _hourly(symbol, b, window_hours)
    common = sorted(set(ra) & set(rb))
    spreads = [ra[h] - rb[h] for h in common]
    swap_a, _ = fee(a, _swap(symbol), "swap")
    swap_b, _ = fee(b, _swap(symbol), "swap")
    round_trip = (swap_a + swap_b) * 2 * 100
    out = {
        "pair": f"{a}-{b}",
        "symbol": _spot(symbol),
        "hours_both_observed": len(common),
        "round_trip_pct": round(round_trip, 4),
    }
    if not spreads:
        return {**out, "mean_spread_pct": None, "note": "нет часов, где наблюдались обе биржи"}

    mean = sum(spreads) / len(spreads)
    sign = 1.0 if mean >= 0 else -1.0
    # Разница в направлении средней: сколько раз она была в нашу сторону и
    # насколько плохой бывала (нижний квартиль) — как у внутрибиржевого.
    directed = [s * sign for s in spreads]
    cons = _percentile(directed, float(getattr(settings, "FUNDING_ARB_CONSERVATIVE_QUANTILE", 0.25)))
    return {
        **out,
        "mean_spread_pct": round(mean, 6),
        # Шорт там, где ставка выше: при положительной ставке шорт получает.
        "direction": f"short {a} / long {b}" if mean >= 0 else f"short {b} / long {a}",
        "sign_consistency": round(sum(1 for d in directed if d > 0) / len(directed), 4),
        "conservative_spread_pct": round(cons, 6),
        "break_even_periods": round(round_trip / cons, 1) if cons > 0 else None,
        "net_over_hold_pct": round(cons * hold - round_trip, 4),
    }


def build(*, window_hours: float = 168.0, hold_periods: int | None = None,
          venues: list[str] | None = None, symbols: list[str] | None = None,
          fee: FeeFn | None = None) -> dict:
    from services.funding_observer import observe_venues

    venues = venues or observe_venues()
    symbols = symbols or list(settings.funding_arb_symbols)
    hold = int(hold_periods or getattr(settings, "FUNDING_ARB_CONFIRM_HOLD_PERIODS", 10))
    fee = fee or _cost_engine_fee

    intra = [_intra(v, s, window_hours, hold, fee) for v in venues for s in symbols]
    cross = [_cross(a, b, s, window_hours, hold, fee)
             for a, b in combinations(venues, 2) for s in symbols]
    return {
        "window_hours": float(window_hours),
        "hold_periods": hold,
        "venues": venues,
        "symbols": [_spot(s) for s in symbols],
        "intra": intra,
        "cross": cross,
        "note": (
            "Ставки в % за 8-часовой период. conservative — нижний квартиль "
            "наблюдавшихся (для межбиржевого — разницы в сторону средней). "
            "break_even_periods — за сколько периодов доход окупит круг "
            "издержек; net_over_hold_pct — итог за hold_periods периодов. "
            "fee_source показывает, откуда взята комиссия: fallback_settings "
            "значит общую настройку, а не комиссию биржи."
        ),
    }
