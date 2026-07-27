"""Стресс-тест: плечо vs депозит vs равная доля (#stress-2026-07-27).

Вопрос Капитана: «дать системе дышать — плечо 1:5 или депозит 5000?»

Проверяем НЕ на дефолтах, а прогоном РЕАЛЬНЫХ траекторий сделок 264–282
(честные филлы) при разных конфигурациях капитала. Считаем то, что решает:
не итоговый PnL, а просадку и расстояние до принудительного закрытия.

Ключевая механика сайзинга (trade_plan.py):
    qty              = min(qty_by_risk, qty_by_balance, qty_by_position_cap)
    qty_by_position_cap = (balance × MAX_POSITION_MARGIN_PCT × leverage) / price
    required_margin  = notional / leverage

Отсюда: при росте плеча нотионал растёт, а маржа НЕ меняется. Значит плечо
НЕ добавляет слотов под символы — оно умножает размер каждой позиции.

Запуск: python3 scripts/stress_leverage_equity.py
"""
from __future__ import annotations

import json
import os

COST_PCT = 0.15
DATA = os.path.join(os.path.dirname(__file__), "calib_trades_264_282.json")

MAX_POSITION_MARGIN_PCT = 0.13
CEILING_PCT = 0.70          # ANTI_DRAIN_POSITION_MAX_USED_MARGIN_PCT
MAX_DAILY_LOSS_PCT = 3.0
UNIVERSE = 8                # BTC ETH SOL XRP AVAX TRX ADA ARB


def honest(t: dict) -> float:
    return t["traj"][-1][1] if t["actual_gross_pct"] > t["mfe_pct"] + 1e-9 else t["actual_gross_pct"]


def simulate(trades: list[dict], *, equity: float, leverage: float) -> dict:
    """Прогон по факту: маржа позиции фиксирована капом, нотионал = маржа×плечо."""
    margin_per_trade = equity * MAX_POSITION_MARGIN_PCT
    notional = margin_per_trade * leverage

    eq = equity
    peak = equity
    max_dd = 0.0
    worst = 0.0
    results = []
    for t in trades:
        gross = honest(t)
        pnl = notional * (gross - COST_PCT) / 100.0
        eq += pnl
        peak = max(peak, eq)
        max_dd = max(max_dd, (peak - eq) / peak * 100)
        worst = min(worst, pnl)
        results.append(pnl)

    # худшая серия подряд
    run = best_run = 0.0
    for p in results:
        run = min(0.0, run + p)
        best_run = min(best_run, run)

    return {
        "equity": equity,
        "leverage": leverage,
        "margin_per_trade": margin_per_trade,
        "notional": notional,
        "slots": int(equity * CEILING_PCT / margin_per_trade),
        "net": eq - equity,
        "net_pct": (eq - equity) / equity * 100,
        "max_dd_pct": max_dd,
        "worst_trade": worst,
        "worst_trade_pct": worst / equity * 100,
        "worst_streak": best_run,
        "worst_streak_pct": best_run / equity * 100,
    }


def main() -> None:
    trades = [t for t in json.load(open(DATA, encoding="utf-8")) if t["trade_mode"] == "trend"]
    print(f"Стресс-тест на {len(trades)} реальных траекториях (честные филлы)\n")

    print("=== 1. ЧТО ДАЁТ ПЛЕЧО: слоты под символы ===")
    print(f"{'конфигурация':28}{'маржа/сделку':>14}{'нотионал':>11}{'слотов':>9}")
    for eq, lev, tag in ((1000, 1, "депозит 1000, плечо 1"), (1000, 3, "депозит 1000, плечо 3"),
                         (1000, 5, "депозит 1000, плечо 5"), (5000, 1, "депозит 5000, плечо 1")):
        r = simulate(trades, equity=eq, leverage=lev)
        print(f"{tag:28}{r['margin_per_trade']:>14.0f}{r['notional']:>11.0f}{r['slots']:>9}")
    print(f"\n  Символов в работе: {UNIVERSE}. Плечо слотов НЕ добавляет — только депозит.\n")

    print("=== 2. ЦЕНА РИСКА на тех же сделках ===")
    print(f"{'конфигурация':28}{'net':>10}{'net %':>9}{'max DD %':>10}"
          f"{'худшая':>9}{'% экв':>8}{'серия':>9}{'% экв':>8}")
    for eq, lev, tag in ((1000, 1, "депозит 1000, плечо 1"), (1000, 3, "депозит 1000, плечо 3"),
                         (1000, 5, "депозит 1000, плечо 5"), (5000, 1, "депозит 5000, плечо 1"),
                         (1500, 1, "депозит 1500, плечо 1")):
        r = simulate(trades, equity=eq, leverage=lev)
        print(f"{tag:28}{r['net']:>10.2f}{r['net_pct']:>8.2f}%{r['max_dd_pct']:>9.2f}%"
              f"{r['worst_trade']:>9.2f}{r['worst_trade_pct']:>7.2f}%"
              f"{r['worst_streak']:>9.2f}{r['worst_streak_pct']:>7.2f}%")

    print("\n=== 3. ДНЕВНОЙ ПРЕДОХРАНИТЕЛЬ (MAX_DAILY_LOSS_PCT = 3%) ===")
    for eq, lev, tag in ((1000, 1, "плечо 1"), (1000, 3, "плечо 3"), (1000, 5, "плечо 5")):
        r = simulate(trades, equity=eq, leverage=lev)
        limit = eq * MAX_DAILY_LOSS_PCT / 100
        n = abs(limit / r["worst_trade"]) if r["worst_trade"] else 999
        print(f"  {tag}: лимит {limit:.0f} USDT, худшая сделка {r['worst_trade']:.2f} "
              f"-> бота остановит после {n:.1f} таких сделок")

    print("\n=== 4. СПРАВЕДЛИВАЯ ДОЛЯ: сколько нужно, чтобы все символы влезли ===")
    print(f"{'депозит':>9}{'потолок 70%':>13}{'доля/символ':>13}{'vs текущие 130':>16}")
    for eq in (1000, 1500, 2000, 3000, 5000):
        ceil_ = eq * CEILING_PCT
        share = ceil_ / UNIVERSE
        verdict = "меньше" if share < 130 else "хватает"
        print(f"{eq:>9}{ceil_:>13.0f}{share:>13.1f}{verdict:>16}")
    need = 130 * UNIVERSE / CEILING_PCT
    print(f"\n  Чтобы все {UNIVERSE} символов держали позицию текущего размера (~130 маржи),")
    print(f"  нужен депозит ≈ {need:.0f} USDT. Плечо здесь бесполезно.")


if __name__ == "__main__":
    main()
