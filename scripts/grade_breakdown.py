#!/usr/bin/env python3
"""Сравнительный анализ грейдов по движкам: какой класс качества льёт.

(#grade-breakdown-2026-07-30)

Зачем отдельный скрипт, а не разрез в /analytics
------------------------------------------------
Наивная таблица «грейд → net USDT» на этих данных даёт неверный ответ, и
ошибиться можно тремя разными способами. Скрипт закрывает все три явно.

**1. Грейд сам управляет размером.** `_grade_mult` в robot_loop даёт A/A+
полный бюджет, B режется до `DYNAMIC_MARGIN_B_CAP_PCT_OF_FREE = 0.5`; сверху
`LEVERAGE_GRADE_*` (1.0 / 0.7 / 0.4). Значит часть разницы в USDT — это просто
разный номинал, а не разное качество отбора. Сравнивать классы можно только в
единицах риска:

    expectancy_R = Σ closed_net_pnl / Σ |net_pnl_stop|

**2. Классы не пересекаются во времени.** По выгрузке 30.07 ВСЕ 50 сделок
грейда A лежат во второй половине выборки, в первой их нет ни одной. То есть A
никогда не торговал тот рынок, на котором B потерял большую часть. Сравнение
«A против B» на всей истории — это сравнение двух периодов. Поэтому каждая
ячейка печатается ещё и по половинам, а пересечение периодов выводится
отдельной таблицей: если оно мало, вердикт по всей выборке недействителен.

**3. Ячеек больше, чем данных.** 50 сделок A на шесть движков — это ~8
наблюдений на ячейку. Бутстрап-интервал на каждой ячейке показывает, накрывает
ли она ноль. Ячейка без интервала — не результат, а шум с подписью.

**Издержки.** История до правки `market_routing` (28.07) записана по спотовой
ставке 0.446% при своп-маршруте. Скрипт переиспользует `analyse()` из
`backfill_routing_costs.py`, чтобы судить по фактической себестоимости. Важно:
издержки учитываются РОВНО ОДИН РАЗ — `closed_net_pnl` уже нетто, повторное
вычитание round-trip в этом репозитории однажды дало ложноположительный
результат на целой гипотезе (см. services/setup_reach.py).

Использование
-------------
    python scripts/grade_breakdown.py analytics_24h/signals_export_*.json
    python scripts/grade_breakdown.py <файл> --long-market swap
    python scripts/grade_breakdown.py <файл> --raw     # без пересчёта издержек
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from backfill_routing_costs import analyse, round_trip_rates
except ImportError:  # pragma: no cover
    analyse = None
    round_trip_rates = None

GRADES = ["A+", "A", "B", "C"]
MIN_CELL = 12          # ниже — интервал бессмыслен, печатаем как «мало данных»
BOOTSTRAP = 20000


def grade_of(row: dict) -> str:
    return str(row.get("grade") or "?")


def regime_of(row: dict) -> str:
    plan = row.get("plan_json") or row.get("plan") or {}
    return str(plan.get("regime") or "?")


def risk_of(row: dict) -> float:
    try:
        return abs(float(row.get("net_pnl_stop") or 0.0))
    except (TypeError, ValueError):
        return 0.0


def net_of(row: dict) -> float:
    try:
        return float(row.get("closed_net_pnl") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def expectancy_r(rows: list[dict]) -> tuple[float | None, float, float, int]:
    """(exp_R, net, risk, n) по сделкам с ненулевым плановым риском.

    Сделка без планового риска исключается из ОБЕИХ сумм: считать её риск нулём
    значит занизить знаменатель и раздуть ожидание.
    """
    usable = [r for r in rows if risk_of(r) > 0]
    if not usable:
        return None, 0.0, 0.0, 0
    net = sum(net_of(r) for r in usable)
    risk = sum(risk_of(r) for r in usable)
    return (net / risk if risk > 0 else None), net, risk, len(usable)


def bootstrap_ci(rows: list[dict], iterations: int = BOOTSTRAP) -> tuple[float, float] | None:
    """95% интервал для expectancy_R. Ресэмплим СДЕЛКИ целиком (пара net/risk),
    иначе числитель и знаменатель разъедутся и интервал будет фиктивным."""
    usable = [(net_of(r), risk_of(r)) for r in rows if risk_of(r) > 0]
    if len(usable) < MIN_CELL:
        return None
    n = len(usable)
    out = []
    for _ in range(iterations):
        num = 0.0
        den = 0.0
        for _ in range(n):
            a, b = usable[random.randrange(n)]
            num += a
            den += b
        if den > 0:
            out.append(num / den)
    if not out:
        return None
    out.sort()
    return out[int(0.025 * len(out))], out[int(0.975 * len(out))]


def fmt_cell(rows: list[dict]) -> str:
    exp, _net, _risk, n = expectancy_r(rows)
    if n == 0:
        return "—"
    if n < MIN_CELL:
        return f"n={n} ({exp:+.3f}R, мало)"
    ci = bootstrap_ci(rows)
    if ci is None:
        return f"n={n} {exp:+.3f}R"
    mark = "" if (ci[0] <= 0 <= ci[1]) else "  ЗНАЧИМ"
    return f"n={n} {exp:+.3f}R [{ci[0]:+.3f};{ci[1]:+.3f}]{mark}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("export", help="JSON-выгрузка сигналов")
    parser.add_argument("--long-market", choices=("auto", "spot", "swap"), default="swap",
                        help="рынок лонга для пересчёта издержек (в проде swap)")
    parser.add_argument("--raw", action="store_true",
                        help="не пересчитывать издержки — смотреть как записано")
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()
    random.seed(args.seed)

    payload = json.loads(Path(args.export).read_text(encoding="utf-8"))
    items = payload.get("items") if isinstance(payload, dict) else payload
    rows = [r for r in (items or [])
            if r.get("status") == "closed" and r.get("closed_net_pnl") is not None]

    if not args.raw and analyse is not None:
        rates = round_trip_rates(None)
        restated = 0
        for row in rows:
            change = analyse(row, rates, None, args.long_market)
            if change:
                row["closed_net_pnl"] = change["new_net"]
                row["closed_total_cost"] = change["new_cost"]
                restated += 1
        print(f"Издержки пересчитаны по фактическому маршруту: {restated} из {len(rows)} сделок")
        print(f"Рынок лонга: {args.long_market}\n")
    else:
        print("Издержки — как записано (--raw)\n")

    rows.sort(key=lambda r: str(r.get("created_at") or ""))
    half = len(rows) // 2
    halves = {"H1": rows[:half], "H2": rows[half:]}

    present_grades = [g for g in GRADES if any(grade_of(r) == g for r in rows)]
    regimes = sorted({regime_of(r) for r in rows})

    # ── 0. Пересекаются ли грейды во времени ────────────────────────────────
    print("=" * 78)
    print("0. РАСПРЕДЕЛЕНИЕ ПО ПОЛОВИНАМ ВЫБОРКИ")
    print("   Если грейд встречается только в одной половине, сравнивать его")
    print("   с другими на всей истории нельзя: это разные периоды рынка.")
    print("-" * 78)
    print(f"{'грейд':<8}{'H1':>8}{'H2':>8}   вердикт")
    for g in present_grades:
        a = sum(1 for r in halves["H1"] if grade_of(r) == g)
        b = sum(1 for r in halves["H2"] if grade_of(r) == g)
        verdict = "ок" if min(a, b) >= MIN_CELL else "НЕ СРАВНИВАТЬ на всей выборке"
        print(f"{g:<8}{a:>8}{b:>8}   {verdict}")

    # ── 1. Грейд в целом ────────────────────────────────────────────────────
    print("\n" + "=" * 78)
    print("1. ГРЕЙД ЦЕЛИКОМ: USDT против нормировки на риск")
    print("   USDT завышает A и занижает B: B носит половинный размер")
    print("   (DYNAMIC_MARGIN_B_CAP_PCT_OF_FREE=0.5) и меньшее плечо.")
    print("-" * 78)
    print(f"{'грейд':<8}{'n':>5}{'net USDT':>11}{'риск USDT':>11}{'exp_R':>9}{'winrate':>9}   95% ДИ")
    for g in present_grades:
        sub = [r for r in rows if grade_of(r) == g]
        exp, net, risk, n = expectancy_r(sub)
        if n == 0:
            continue
        wins = sum(1 for r in sub if net_of(r) > 0)
        ci = bootstrap_ci(sub)
        ci_s = f"[{ci[0]:+.3f}; {ci[1]:+.3f}]" if ci else "мало данных"
        print(f"{g:<8}{len(sub):>5}{net:>11.2f}{risk:>11.1f}{exp:>9.4f}"
              f"{100 * wins / len(sub):>8.0f}%   {ci_s}")

    # ── 2. Грейд × движок ───────────────────────────────────────────────────
    print("\n" + "=" * 78)
    print("2. ГРЕЙД × ДВИЖОК (expectancy в единицах риска)")
    print("   «ЗНАЧИМ» = 95% интервал не накрывает ноль. Без пометки —")
    print("   наблюдаемое отличие от нуля выборкой не подтверждается.")
    print("-" * 78)
    for regime in regimes:
        print(f"\n{regime}")
        for g in present_grades:
            sub = [r for r in rows if regime_of(r) == regime and grade_of(r) == g]
            if not sub:
                continue
            print(f"   {g:<4}{fmt_cell(sub)}")

    # ── 3. Устойчивость по половинам ────────────────────────────────────────
    print("\n" + "=" * 78)
    print("3. ГРЕЙД × ДВИЖОК ПО ПОЛОВИНАМ")
    print("   Разрез, меняющий знак между половинами, — подгонка, а не находка.")
    print("-" * 78)
    print(f"{'движок':<26}{'грейд':<6}{'H1':>22}{'H2':>22}")
    for regime in regimes:
        for g in present_grades:
            cells = []
            any_data = False
            for key in ("H1", "H2"):
                sub = [r for r in halves[key]
                       if regime_of(r) == regime and grade_of(r) == g]
                exp, _n_, _r_, n = expectancy_r(sub)
                if n:
                    any_data = True
                    cells.append(f"{('n=%d %+.3fR' % (n, exp)):>22}")
                else:
                    cells.append(f"{'—':>22}")
            if any_data:
                print(f"{regime:<26}{g:<6}" + "".join(cells))

    # ── 4. Мощность ─────────────────────────────────────────────────────────
    print("\n" + "=" * 78)
    print("4. СКОЛЬКО СДЕЛОК НУЖНО, чтобы отличить наблюдаемое от нуля")
    print("   Правило: n ≈ (2.8 · разброс / |среднее|)². Если требуемое n")
    print("   много больше имеющегося — по этой ячейке вывода нет никакого.")
    print("-" * 78)
    for g in present_grades:
        sub = [r for r in rows if grade_of(r) == g and risk_of(r) > 0]
        if len(sub) < 3:
            continue
        per = [net_of(r) / risk_of(r) for r in sub]
        mean = sum(per) / len(per)
        var = sum((v - mean) ** 2 for v in per) / len(per)
        sd = var ** 0.5
        need = int((2.8 * sd / abs(mean)) ** 2) if abs(mean) > 1e-9 else None
        need_s = f"{need:,}" if need is not None and need < 10 ** 7 else "∞"
        print(f"   {g:<4} среднее {mean:+.4f}R, разброс {sd:.4f} → нужно ~{need_s} сделок "
              f"(есть {len(sub)})")

    print("\n" + "=" * 78)
    print("ЧИТАТЬ ТАК: сначала раздел 0. Если грейды не пересекаются во времени,")
    print("разделы 1–2 описывают разные периоды рынка, а не разное качество")
    print("отбора, и вывод «грейд X льёт» из них не следует.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
