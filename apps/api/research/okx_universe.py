"""Кандидаты во вселенную OKX (#okx-universe-2026-09-12).

С 12.09 все рыночные данные идут с OKX (свечи, стакан, наблюдение фандинга), и
выбор символов больше не ограничен листингом HTX. Этот скрипт собирает по
бессрочным контрактам OKX к USDT то, что решает пригодность символа для наших
движков:

  * суточный оборот в USDT (volCcy24h × цена — ccxt его для свопов не считает);
  * спред лучшей пары и глубину книги в пределах ±0.5% и ±1% от середины в USDT
    (размер уровня — в контрактах, умножается на contractSize);
  * волатильность — медианный ATR(14) часовых свечей за неделю, % цены;
  * текущая ставка фандинга; возраст листинга; есть ли символ на HTX.

    python -m research.okx_universe [--top 25] [--extra CHIP,PI]

Ничего не меняет в торговле — только таблица для решения владельца.
"""
from __future__ import annotations

import argparse
import statistics
import time


def turnover_usdt(info: dict, last: float | None) -> float | None:
    """Оборот за сутки в USDT: объём в базовой монете × цена."""
    try:
        return float(info.get("volCcy24h")) * float(last)
    except (TypeError, ValueError):
        return None


def depth_within(levels: list, mid: float, band_pct: float, contract_size: float) -> float:
    """USDT-объём уровней книги в пределах band_pct от середины."""
    total = 0.0
    for level in levels or []:
        price, size = float(level[0]), float(level[1])
        if abs(price - mid) / mid * 100.0 <= band_pct:
            total += price * size * contract_size
    return total


def atr_pct(ohlcv: list, period: int = 14) -> float | None:
    """Медиана ATR(period) по часовым свечам, % цены закрытия."""
    if len(ohlcv) <= period:
        return None
    trs, vals = [], []
    for i in range(1, len(ohlcv)):
        _, _, h, l, c, *_ = ohlcv[i]
        prev_c = ohlcv[i - 1][4]
        trs.append(max(h - l, abs(h - prev_c), abs(l - prev_c)))
        if len(trs) >= period:
            vals.append(sum(trs[-period:]) / period / c * 100.0)
    return statistics.median(vals) if vals else None


def collect(top: int = 25, extra: list[str] | None = None, current: list[str] | None = None) -> list[dict]:
    import ccxt

    ex = ccxt.okx({"enableRateLimit": True, "options": {"defaultType": "swap"}})
    markets = ex.load_markets()
    swaps = {m["symbol"]: m for m in markets.values()
             if m.get("swap") and m.get("settle") == "USDT" and m.get("active")}
    tickers = ex.fetch_tickers(list(swaps))

    htx_bases: set[str] = set()
    try:
        htx = ccxt.htx({"enableRateLimit": True, "options": {"defaultType": "swap"}})
        htx_bases = {m["base"] for m in htx.load_markets().values()
                     if m.get("swap") and m.get("settle") == "USDT"}
    except Exception:  # noqa: BLE001 — HTX недоступен: колонка останется пустой
        htx_bases = set()

    rows = []
    for sym, t in tickers.items():
        turnover = turnover_usdt(t.get("info") or {}, t.get("last"))
        if turnover:
            rows.append((turnover, sym))
    rows.sort(reverse=True)
    picked = [sym for _, sym in rows[:top]]
    for base in (extra or []) + (current or []):
        sym = f"{base.split('/')[0].upper()}/USDT:USDT"
        if sym in swaps and sym not in picked:
            picked.append(sym)

    out = []
    now_ms = time.time() * 1000
    for sym in picked:
        m, t = swaps[sym], tickers.get(sym) or {}
        bid, ask, last = t.get("bid"), t.get("ask"), t.get("last")
        if not bid or not ask:
            continue
        mid = (bid + ask) / 2
        cs = float(m.get("contractSize") or 1.0)
        try:
            book = ex.fetch_order_book(sym, limit=400)
        except Exception:  # noqa: BLE001
            book = {"bids": [], "asks": []}
        try:
            ohlcv = ex.fetch_ohlcv(sym, "1h", limit=168)
        except Exception:  # noqa: BLE001
            ohlcv = []
        try:
            funding = float((ex.fetch_funding_rate(sym) or {}).get("fundingRate") or 0) * 100
        except Exception:  # noqa: BLE001
            funding = None
        list_time = (m.get("info") or {}).get("listTime")
        base = m.get("base")
        out.append({
            "symbol": f"{base}/USDT",
            "turnover_musd": round((turnover_usdt(t.get("info") or {}, last) or 0) / 1e6, 1),
            "spread_pct": round((ask - bid) / mid * 100, 4),
            "depth_05_kusd": round(depth_within(book["bids"] + book["asks"], mid, 0.5, cs) / 1e3),
            "depth_1_kusd": round(depth_within(book["bids"] + book["asks"], mid, 1.0, cs) / 1e3),
            "atr_1h_pct": round(atr_pct(ohlcv) or 0, 3),
            "funding_pct": round(funding, 4) if funding is not None else None,
            "age_days": int((now_ms - int(list_time)) / 86400000) if list_time else None,
            "on_htx": (base in htx_bases) if htx_bases else None,
            "in_universe": f"{base}/USDT" in (current or []),
        })
    return out


def main() -> None:
    from core.config import settings

    parser = argparse.ArgumentParser()
    parser.add_argument("--top", type=int, default=25)
    parser.add_argument("--extra", default="CHIP,PI")
    args = parser.parse_args()
    rows = collect(args.top, [x.strip() for x in args.extra.split(",") if x.strip()],
                   list(settings.symbols))
    cols = ("symbol", "turnover_musd", "spread_pct", "depth_05_kusd", "depth_1_kusd",
            "atr_1h_pct", "funding_pct", "age_days", "on_htx", "in_universe")
    print("\t".join(cols))
    for r in rows:
        print("\t".join(str(r[c]) for c in cols))


if __name__ == "__main__":
    main()
