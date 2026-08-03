#!/usr/bin/env python3
"""Сколько стоит ярус trend_capture_band (#capture-band-cost-2026-08-03).

Вопрос
------
Полоса ловит сделки с MFE в диапазоне [TREND_CAPTURE_ARM_PCT; ride_min) и
закрывает их, когда откат от пика достигает доли band_give. По дневному отчёту
она стала доминирующим выходом: 11 из 27 закрытий за 72 часа против 1 из 302
в прежней выборке. Трендовые сделки живут теперь 1.5 часа вместо 5–12.

Вопрос не «хорошо это или плохо вообще», а конкретный: какая доля пойманных
полосой сделок дошла бы до хвоста (ride/tp2), если бы полосы не было.

Почему считаем на СТАРЫХ данных
-------------------------------
У сделки, закрытой полосой, траектория обрывается на выходе — что было дальше,
не записано. То же ограничение у `/ml/exit-replay`: вариант, выходящий ПОЗЖЕ
фактического закрытия, книжится как `actual_close`, поэтому инструмент не умеет
показывать выгоду от удержания.

В выборке до 27.07 полоса срабатывала один раз на 302 сделки — значит там
траектории полные, и на них видно, куда сделка шла ПОСЛЕ того момента, когда
полоса бы её закрыла. Это и есть честный контрфакт.

Издержки в сравнении сокращаются: у обоих исходов ровно один выход, комиссия
одинакова. Поэтому считаем в процентах хода, как и exit_replay.

Запуск:
    python scripts\\capture_band_cost.py analytics_24h\\signals_export_<...>.json
"""
from __future__ import annotations

import argparse
import json
import statistics as st
from collections import Counter
from pathlib import Path

TREND_REGIMES = {"trend_up_candidate", "trend_down_candidate", "crt",
                 "reversal_long_candidate"}


def plan(row):
    return row.get("plan_json") or row.get("plan") or {}


def lifecycle(row):
    return plan(row).get("lifecycle") or {}


def simulate_band(traj, *, arm: float, give: float, ride_min: float, floor: float):
    """Где полоса закрыла бы сделку. None — не сработала бы.

    Повторяет exit_policy: вооружается при arm <= mfe < ride_min, выходит когда
    откат от пика >= mfe*give, филл не лучше рынка (min с текущей точкой).
    """
    mfe = 0.0
    for _t, pct in traj:
        mfe = max(mfe, pct)
        if not (arm <= mfe < ride_min):
            continue
        drawdown = mfe - pct
        if drawdown < mfe * give:
            continue
        band_pct = min(mfe * (1.0 - give), pct)
        if band_pct >= floor:
            return band_pct, mfe
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("export")
    ap.add_argument("--arm", type=float, default=0.40)
    ap.add_argument("--give", type=float, default=0.25)
    ap.add_argument("--ride-min", type=float, default=0.80)
    ap.add_argument("--floor", type=float, default=0.30)
    args = ap.parse_args()

    payload = json.loads(Path(args.export).read_text(encoding="utf-8"))
    items = payload.get("items") if isinstance(payload, dict) else payload
    rows = [r for r in (items or []) if r.get("status") == "closed"]

    # Только сделки с профилем ведения trend и полной траекторией.
    pool = []
    for r in rows:
        p = plan(r)
        lc = lifecycle(r)
        if p.get("trade_mode") != "trend":
            continue
        traj = lc.get("traj") or []
        fin = lc.get("final_result_pct")
        if len(traj) < 3 or fin is None:
            continue
        # Сделка, уже закрытая полосой, для контрфакта непригодна: её
        # траектория обрывается там, где полоса сработала.
        if r.get("closed_reason") == "trend_capture_band":
            continue
        pool.append(r)

    print(f"трендовых сделок с полной траекторией: {len(pool)}")
    if not pool:
        print("нечего считать")
        return 0

    caught, missed_band = [], []
    for r in pool:
        lc = lifecycle(r)
        hit = simulate_band(lc["traj"], arm=args.arm, give=args.give,
                            ride_min=args.ride_min, floor=args.floor)
        (caught if hit else missed_band).append((r, hit))

    print(f"полоса поймала бы: {len(caught)}   не тронула бы: {len(missed_band)}")
    if not caught:
        return 0

    band_total = sum(hit[0] for _r, hit in caught)
    actual_total = sum(lifecycle(r)["final_result_pct"] for r, _h in caught)

    print("\n" + "=" * 74)
    print(f"ЧТО ПОЛОСА СДЕЛАЛА БЫ С ЭТИМИ {len(caught)} СДЕЛКАМИ")
    print("-" * 74)
    print(f"  выход по полосе : {band_total:+8.2f}%   среднее {band_total / len(caught):+.4f}%")
    print(f"  фактический     : {actual_total:+8.2f}%   среднее {actual_total / len(caught):+.4f}%")
    print(f"  разница         : {band_total - actual_total:+8.2f}%")

    # Куда сделка ушла ПОСЛЕ момента, когда полоса бы её закрыла.
    ran_further, died = [], []
    for r, hit in caught:
        band_pct, mfe_at_band = hit
        peak_after = max(p for _t, p in lifecycle(r)["traj"])
        (ran_further if peak_after >= args.ride_min else died).append(
            (r, band_pct, peak_after, lifecycle(r)["final_result_pct"])
        )

    print("\n" + "=" * 74)
    print("ГЛАВНЫЙ ВОПРОС: сколько из пойманных дошло бы до хвоста")
    print("-" * 74)
    print(f"  дошли до MFE >= {args.ride_min}% (зона ride/tp2): "
          f"{len(ran_further)} из {len(caught)} ({100 * len(ran_further) / len(caught):.0f}%)")
    if ran_further:
        b = sum(x[1] for x in ran_further)
        a = sum(x[3] for x in ran_further)
        print(f"    полоса взяла бы {b:+.2f}%, фактически вышло {a:+.2f}%  "
              f"→ упущено {a - b:+.2f}%")
    if died:
        b = sum(x[1] for x in died)
        a = sum(x[3] for x in died)
        print(f"  не дошли: {len(died)}")
        print(f"    полоса взяла бы {b:+.2f}%, фактически вышло {a:+.2f}%  "
              f"→ спасено {b - a:+.2f}%")

    print("\n" + "=" * 74)
    print("ЧУВСТВИТЕЛЬНОСТЬ К ПОРОГУ ВООРУЖЕНИЯ")
    print("-" * 74)
    print(f"  {'arm':<8}{'поймано':>9}{'полоса %':>11}{'факт %':>11}{'дельта':>10}")
    for arm in (0.30, 0.40, 0.55, 0.70):
        hits = [(r, simulate_band(lifecycle(r)["traj"], arm=arm, give=args.give,
                                  ride_min=args.ride_min, floor=args.floor))
                for r in pool]
        hits = [(r, h) for r, h in hits if h]
        if not hits:
            print(f"  {arm:<8.2f}{0:>9}")
            continue
        b = sum(h[0] for _r, h in hits)
        a = sum(lifecycle(r)["final_result_pct"] for r, _h in hits)
        print(f"  {arm:<8.2f}{len(hits):>9}{b:>11.2f}{a:>11.2f}{b - a:>10.2f}")

    print("\n" + "=" * 74)
    print("Дельта > 0 — полоса улучшает результат на этой выборке.")
    print("Дельта < 0 — режет хвосты дороже, чем спасает откаты.")
    print("Смотрите на монотонность по arm, а не на лучшую строку: выбор")
    print("порога по этой же таблице — подгонка.")
    print(f"\nсделок в выборке {len(pool)} — при разбросе трендовых MFE вывод")
    print("считать указанием, а не доказательством.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
