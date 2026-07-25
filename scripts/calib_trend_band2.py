"""Двухъярусная трендовая фиксация: узкая полоса + широкий ride (#trend-capture-band).

Ярус 1 (ride, как в бою): mfe >= RIDE_MIN (0.8) → широкий трейл give=0.50.
Ярус 2 (НОВЫЙ, «мёртвая зона»): mfe в [arm, RIDE_MIN) → тугой трейл give.

Ярус 2 намеренно ограничен полосой: как только сделка доказала себя (mfe >= RIDE_MIN),
управление передаётся широкому трейлу — раннеров не режем.

Запуск: python3 scripts/calib_trend_band2.py
"""
from __future__ import annotations

import json
import os
from itertools import product

COST_PCT = 0.15
RIDE_MIN = 0.8
RIDE_GIVE = 0.50
DATA = os.path.join(os.path.dirname(__file__), "calib_trades_264_282.json")


def net(notional: float, pct: float) -> float:
    return notional * (pct - COST_PCT) / 100.0


def honest_actual(t: dict) -> float:
    return t["traj"][-1][1] if t["actual_gross_pct"] > t["mfe_pct"] + 1e-9 else t["actual_gross_pct"]


def sim(t: dict, *, arm: float, give: float, floor_pct: float, band_only: bool = True):
    mfe = 0.0
    for _age, pct in t["traj"]:
        mfe = max(mfe, pct)
        if pct <= -t["stop_pct"]:
            return -t["stop_pct"], "stop_loss"
        if t.get("tp2_pct") and pct >= t["tp2_pct"] * 0.92:
            return pct, "tp2"
        # ярус 1 — широкий ride для доказавших себя
        if mfe >= RIDE_MIN and (mfe - pct) >= mfe * RIDE_GIVE:
            fill = min(mfe * (1 - RIDE_GIVE), pct)
            if fill >= floor_pct:
                return fill, "ride"
        # ярус 2 — тугой трейл в мёртвой зоне
        in_band = (mfe < RIDE_MIN) if band_only else True
        if in_band and mfe >= arm and (mfe - pct) >= mfe * give:
            fill = min(mfe * (1 - give), pct)
            if fill >= floor_pct:
                return fill, "band"
    return honest_actual(t), "actual"


def main() -> None:
    trades = [t for t in json.load(open(DATA, encoding="utf-8")) if t["trade_mode"] == "trend"]
    base = sum(net(t["notional"], honest_actual(t)) for t in trades)
    print(f"Трендовых сделок: {len(trades)}   честный факт: {base:+.2f} USDT\n")

    print("=== ЯРУС 2, ограниченный полосой mfe < 0.8 (пол 0.40%) ===")
    print(f"{'arm':>6}{'give':>7}{'net':>9}{'дельта':>9}{'WR':>6}{'band':>6}{'ride':>6}{'stop':>6}")
    rows = []
    for arm, give in product([0.30, 0.40, 0.50, 0.60, 0.70], [0.25, 0.30, 0.35, 0.40, 0.50]):
        tot = 0.0
        c = {"band": 0, "ride": 0, "stop_loss": 0, "tp2": 0, "actual": 0}
        wins = 0
        for t in trades:
            pct, why = sim(t, arm=arm, give=give, floor_pct=0.40)
            v = net(t["notional"], pct)
            tot += v
            wins += v > 0
            c[why] += 1
        rows.append((arm, give, tot, wins / len(trades) * 100, c))
    rows.sort(key=lambda r: r[2], reverse=True)
    for arm, give, tot, wr, c in rows[:12]:
        print(f"{arm:>6.2f}{give:>7.2f}{tot:>9.2f}{tot-base:>+9.2f}{wr:>5.0f}%"
              f"{c['band']:>6}{c['ride']:>6}{c['stop_loss']:>6}")

    arm, give = rows[0][0], rows[0][1]
    print(f"\nЛучшее: arm={arm} give={give}  ({rows[0][2]:+.2f}, дельта {rows[0][2]-base:+.2f})")

    print("\n=== ПОСДЕЛОЧНО при лучшем варианте ===")
    print(f"{'id':>6}{'mfe':>7}{'факт net':>10}{'новый net':>11}{'дельта':>9}  причина")
    tot = 0.0
    for t in trades:
        pct, why = sim(t, arm=arm, give=give, floor_pct=0.40)
        a = net(t["notional"], honest_actual(t))
        v = net(t["notional"], pct)
        tot += v
        print(f"{t['id']:>6}{t['mfe_pct']:>7.3f}{a:>10.2f}{v:>11.2f}{v-a:>+9.2f}  {why}")
    print(f"{'ИТОГО':>6}{'':>7}{base:>10.2f}{tot:>11.2f}{tot-base:>+9.2f}")

    print("\n=== УСТОЙЧИВОСТЬ: тот же вариант без 2 крупнейших сделок ===")
    big = sorted(trades, key=lambda t: t["notional"], reverse=True)[:2]
    sub = [t for t in trades if t not in big]
    b2 = sum(net(t["notional"], honest_actual(t)) for t in sub)
    n2 = sum(net(t["notional"], sim(t, arm=arm, give=give, floor_pct=0.40)[0]) for t in sub)
    print(f"  без {', '.join(t['id'] for t in big)}: факт {b2:+.2f} -> {n2:+.2f} (дельта {n2-b2:+.2f})")


if __name__ == "__main__":
    main()
