"""Калибровка трендовых выходов по РЕАЛЬНЫМ траекториям (#trend-capture-band-2026-07-25).

Источник — lifecycle.traj закрытых сигналов 264–282 (телеметрия 16–25.07).
traj = [[возраст_сек, gross_result_pct], ...], знак уже нормализован по стороне.

Что считаем:
  * фактический честный net (фантомные филлы заменены рыночной ценой);
  * сетку вариантов «трендовой фиксации в модальной полосе MFE»;
  * гейты MIN_PROTECTIVE_* — при каком пороге они перестают глушить выход.

Издержки round-trip = 0.15% нотионала (замер по total_cost/notional, сходится
на всех 19 сделках). Стоп моделируется как выход по -stop_pct (paper-слиппедж
уже внутри факта).

Запуск:  python3 scripts/calib_trend_band.py
"""
from __future__ import annotations

import json
import os
from itertools import product

COST_PCT = 0.15  # round-trip, % нотионала (факт по телеметрии)

DATA_PATH = os.path.join(os.path.dirname(__file__), "calib_trades_264_282.json")


def load_trades() -> list[dict]:
    with open(DATA_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def net_usdt(notional: float, gross_pct: float) -> float:
    return notional * (gross_pct - COST_PCT) / 100.0


def honest_actual(t: dict) -> float:
    """Честный факт: если записанный exit дал result выше MFE — это фантомный
    филл (цена выше максимума сделки). Берём последнюю точку траектории."""
    if t["actual_gross_pct"] > t["mfe_pct"] + 1e-9:
        return t["traj"][-1][1]
    return t["actual_gross_pct"]


def simulate(t: dict, *, arm: float, give: float, floor_pct: float) -> tuple[float, str]:
    """Трендовая фиксация в модальной полосе MFE.

    Вооружаемся при mfe >= arm; выходим, когда отдали долю пика (give).
    Филл — по рынку в момент срабатывания (min(уровень трейла, текущая цена)),
    но не ниже floor_pct: ниже пола фиксировать нечего (издержки не отбиты).
    Хард-стоп и TP2 моделируются как в бою.
    """
    mfe = 0.0
    for age, pct in t["traj"]:
        mfe = max(mfe, pct)
        if pct <= -t["stop_pct"]:
            return -t["stop_pct"], "stop_loss"
        if t.get("tp2_pct") and pct >= t["tp2_pct"] * 0.92:
            return pct, "tp2"
        if mfe >= arm and (mfe - pct) >= mfe * give:
            fill = min(mfe * (1.0 - give), pct)   # трейл не лучше рынка
            if fill >= floor_pct:
                return fill, "trend_capture"
            # ниже пола — фиксировать нечего, держим дальше
    return honest_actual(t), "actual_close"


def main() -> None:
    trades = [t for t in load_trades() if t["trade_mode"] == "trend"]
    scalp = [t for t in load_trades() if t["trade_mode"] != "trend"]

    print(f"Трендовых сделок с траекторией: {len(trades)}  (не-тренд: {len(scalp)})\n")

    # ── 1. Честный факт против записанного ────────────────────────────────
    booked = sum(net_usdt(t["notional"], t["actual_gross_pct"]) for t in trades)
    honest = sum(net_usdt(t["notional"], honest_actual(t)) for t in trades)
    phantom = [t for t in trades if t["actual_gross_pct"] > t["mfe_pct"] + 1e-9]
    print("=== ФАКТ ===")
    print(f"  записано в телеметрии : {booked:+.2f} USDT")
    print(f"  честно (филл по рынку): {honest:+.2f} USDT")
    print(f"  фантомных филлов      : {len(phantom)} ({', '.join(t['id'] for t in phantom)})")
    print(f"  завышение             : {booked - honest:+.2f} USDT\n")

    # ── 2. Распределение MFE ──────────────────────────────────────────────
    mfes = sorted(t["mfe_pct"] for t in trades)
    n = len(mfes)
    med = mfes[n // 2]
    print("=== РАСПРЕДЕЛЕНИЕ MFE (тренд) ===")
    print(f"  медиана {med:.3f}%   среднее {sum(mfes)/n:.3f}%   макс {mfes[-1]:.3f}%")
    for thr in (0.35, 0.5, 0.8, 1.0, 1.2, 1.8):
        share = sum(1 for m in mfes if m >= thr) / n * 100
        print(f"  MFE >= {thr:.2f}% : {share:5.1f}% сделок")
    print()

    # ── 3. Сетка трендовой фиксации ───────────────────────────────────────
    print("=== СЕТКА: arm / giveback (пол = net_safe 0.30%) ===")
    print(f"{'arm':>6}{'give':>7}{'net USDT':>11}{'winrate':>9}{'фиксаций':>10}")
    rows = []
    for arm, give in product([0.30, 0.40, 0.50, 0.60, 0.80], [0.30, 0.40, 0.50]):
        total = 0.0
        wins = 0
        fired = 0
        for t in trades:
            pct, reason = simulate(t, arm=arm, give=give, floor_pct=0.30)
            v = net_usdt(t["notional"], pct)
            total += v
            wins += 1 if v > 0 else 0
            fired += 1 if reason == "trend_capture" else 0
        rows.append((arm, give, total, wins / len(trades) * 100, fired))
    rows.sort(key=lambda r: r[2], reverse=True)
    for arm, give, total, wr, fired in rows:
        print(f"{arm:>6.2f}{give:>7.2f}{total:>11.2f}{wr:>8.0f}%{fired:>10}")
    best = rows[0]
    print(f"\n  Лучшее: arm={best[0]} give={best[1]} -> {best[2]:+.2f} USDT "
          f"(честный факт {honest:+.2f}, дельта {best[2]-honest:+.2f})\n")

    # ── 4. Гейт MIN_PROTECTIVE_EXIT_PCT ───────────────────────────────────
    print("=== ГЕЙТ MIN_PROTECTIVE_EXIT_PCT (сколько выходов он глушит) ===")
    arm, give = best[0], best[1]
    for gate in (0.20, 0.30, 0.40, 0.60, 0.90, 1.20, 1.80):
        total = 0.0
        fired = 0
        for t in trades:
            pct, reason = simulate(t, arm=arm, give=give, floor_pct=gate)
            total += net_usdt(t["notional"], pct)
            fired += 1 if reason == "trend_capture" else 0
        mark = "  <- текущий" if abs(gate - 1.80) < 1e-9 else ""
        print(f"  порог {gate:.2f}% : выходов {fired:>2}/{len(trades)}   net {total:+7.2f} USDT{mark}")

    # ── 5. Гейт MIN_PROTECTIVE_NET_USDT ───────────────────────────────────
    print("\n=== ГЕЙТ MIN_PROTECTIVE_NET_USDT (при arm/give лучшего варианта) ===")
    for gate_usdt in (0.0, 0.25, 0.50, 1.00, 2.50):
        total = 0.0
        fired = 0
        for t in trades:
            pct, reason = simulate(t, arm=arm, give=give, floor_pct=0.30)
            if reason == "trend_capture" and net_usdt(t["notional"], pct) < gate_usdt:
                pct, reason = honest_actual(t), "gate_blocked"
            total += net_usdt(t["notional"], pct)
            fired += 1 if reason == "trend_capture" else 0
        mark = "  <- текущий" if abs(gate_usdt - 2.50) < 1e-9 else ""
        print(f"  порог {gate_usdt:.2f}$ : выходов {fired:>2}/{len(trades)}   net {total:+7.2f} USDT{mark}")


if __name__ == "__main__":
    main()
