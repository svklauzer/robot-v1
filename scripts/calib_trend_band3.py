"""Калибровка трендовой фиксации — ПОЛНАЯ модель боевого контура.

Отличие от band2: моделируются TP1-partial (50% по цене TP1) и перевод стопа
в безубыток после TP1 — без них симуляция несправедлива к #269/#271/#274,
которые реально закрывались через partial+breakeven_stop.

Модель одного прохода по траектории:
  1. хард-стоп: pct <= -stop_pct                     → закрыть остаток
  2. TP1 (один раз): pct >= tp1_pct                  → зафиксировать 50% по tp1_pct,
                                                        стоп -> безубыток (+0.07% gross)
  3. после TP1: pct <= be_level                      → закрыть остаток по be_level
  4. TP2: pct >= tp2_pct*0.92                        → закрыть остаток
  5. ярус 1 ride:  mfe >= 0.8, отдали 50% пика       → закрыть остаток
  6. ярус 2 band:  arm <= mfe < 0.8, отдали give     → закрыть остаток
  иначе — досидели до конца траектории (честный факт).

Издержки COST_PCT начисляются на КАЖДУЮ закрываемую долю (как в бою).

Запуск: python3 scripts/calib_trend_band3.py
"""
from __future__ import annotations

import json
import os
from itertools import product

COST_PCT = 0.15
RIDE_MIN = 0.8
RIDE_GIVE = 0.50
BE_LEVEL = 0.07        # безубыток-стоп после TP1, gross % (замер: #271/#274 = 0.0699%)
TP1_SHARE = 0.5
DATA = os.path.join(os.path.dirname(__file__), "calib_trades_264_282.json")


def honest_actual(t: dict) -> float:
    return t["traj"][-1][1] if t["actual_gross_pct"] > t["mfe_pct"] + 1e-9 else t["actual_gross_pct"]


def run(t: dict, *, arm: float | None, give: float, floor_pct: float) -> tuple[float, str]:
    """Возвращает (net_usdt, причина закрытия остатка)."""
    notional = t["notional"]
    tp1, tp2, stop = t.get("tp1_pct"), t.get("tp2_pct"), t.get("stop_pct")
    realized = 0.0
    share = 1.0
    tp1_done = False
    mfe = 0.0

    def close(pct: float, why: str) -> tuple[float, str]:
        return realized + notional * share * (pct - COST_PCT) / 100.0, why

    for _age, pct in t["traj"]:
        mfe = max(mfe, pct)
        if stop is not None and not tp1_done and pct <= -stop:
            return close(-stop, "stop_loss")
        if not tp1_done and tp1 and pct >= tp1:
            realized += notional * TP1_SHARE * (tp1 - COST_PCT) / 100.0
            share -= TP1_SHARE
            tp1_done = True
            continue
        if tp1_done and pct <= BE_LEVEL:
            return close(BE_LEVEL, "breakeven_stop")
        if tp2 and pct >= tp2 * 0.92:
            return close(pct, "tp2")
        if mfe >= RIDE_MIN and (mfe - pct) >= mfe * RIDE_GIVE:
            fill = min(mfe * (1 - RIDE_GIVE), pct)
            if fill >= floor_pct:
                return close(fill, "ride")
        if arm is not None and mfe < RIDE_MIN and mfe >= arm and (mfe - pct) >= mfe * give:
            fill = min(mfe * (1 - give), pct)
            if fill >= floor_pct:
                return close(fill, "band")
    return close(t["traj"][-1][1], "hold_to_end")


def total(trades, **kw) -> tuple[float, dict, int]:
    tot = 0.0
    reasons: dict[str, int] = {}
    wins = 0
    for t in trades:
        v, why = run(t, **kw)
        tot += v
        wins += v > 0
        reasons[why] = reasons.get(why, 0) + 1
    return tot, reasons, wins


def main() -> None:
    trades = [t for t in json.load(open(DATA, encoding="utf-8")) if t["trade_mode"] == "trend"]

    # База = текущий бой, промоделированный тем же движком без яруса 2.
    base, base_r, base_w = total(trades, arm=None, give=0.0, floor_pct=1.80)
    print(f"Трендовых сделок: {len(trades)}")
    print(f"БАЗА (как в бою: ярус 2 выкл, пол 1.80% глушит ride): {base:+.2f} USDT, "
          f"WR {base_w/len(trades)*100:.0f}%, {base_r}\n")

    print("=== СЕТКА: ярус 2 (полоса mfe < 0.8) при поле 0.40% ===")
    print(f"{'arm':>6}{'give':>7}{'net':>9}{'дельта':>9}{'WR':>6}  причины")
    rows = []
    for arm, give in product([0.30, 0.40, 0.50, 0.60], [0.25, 0.30, 0.35, 0.40, 0.50]):
        tot, r, w = total(trades, arm=arm, give=give, floor_pct=0.40)
        rows.append((arm, give, tot, w / len(trades) * 100, r))
    rows.sort(key=lambda x: x[2], reverse=True)
    for arm, give, tot, wr, r in rows:
        print(f"{arm:>6.2f}{give:>7.2f}{tot:>9.2f}{tot-base:>+9.2f}{wr:>5.0f}%  {r}")

    arm, give = rows[0][0], rows[0][1]
    print(f"\nЛучшее: arm={arm} give={give}\n")

    print("=== ПОЛ (MIN_PROTECTIVE_EXIT_PCT) при лучшем ярусе 2 ===")
    for floor in (0.30, 0.40, 0.50, 0.60, 0.90, 1.80):
        tot, r, w = total(trades, arm=arm, give=give, floor_pct=floor)
        mark = "  <- текущий" if floor == 1.80 else ""
        print(f"  {floor:.2f}% : {tot:+7.2f} USDT  WR {w/len(trades)*100:3.0f}%  {r}{mark}")

    print("\n=== ПОСДЕЛОЧНО (пол 0.40) ===")
    print(f"{'id':>6}{'mfe':>7}{'база':>9}{'ново':>9}{'дельта':>9}  причина")
    tb = tn = 0.0
    for t in trades:
        b, _ = run(t, arm=None, give=0.0, floor_pct=1.80)
        n, why = run(t, arm=arm, give=give, floor_pct=0.40)
        tb += b
        tn += n
        print(f"{t['id']:>6}{t['mfe_pct']:>7.3f}{b:>9.2f}{n:>9.2f}{n-b:>+9.2f}  {why}")
    print(f"{'ИТОГО':>6}{'':>7}{tb:>9.2f}{tn:>9.2f}{tn-tb:>+9.2f}")

    print("\n=== УСТОЙЧИВОСТЬ ===")
    for label, sub in (
        ("без 2 крупнейших по марже", sorted(trades, key=lambda t: t["notional"])[:-2]),
        ("без 2 лучших по дельте", None),
        ("только вторая половина выборки", trades[: len(trades) // 2]),
    ):
        if sub is None:
            d = sorted(trades, key=lambda t: run(t, arm=arm, give=give, floor_pct=0.40)[0]
                       - run(t, arm=None, give=0.0, floor_pct=1.80)[0])
            sub = d[:-2]
        b, _, _ = total(sub, arm=None, give=0.0, floor_pct=1.80)
        n, _, _ = total(sub, arm=arm, give=give, floor_pct=0.40)
        print(f"  {label:32} ({len(sub):>2} сд.): {b:+7.2f} -> {n:+7.2f}  дельта {n-b:+.2f}")


if __name__ == "__main__":
    main()
