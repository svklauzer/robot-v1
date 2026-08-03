"""Калибровка порогов ТЗ по факту (#tz-enforce-2026-08-03).

Порог 23 для ADX взят из ТЗ, написанного под DEX. На наших данных ADX по
трендовым сетапам шёл 16.1 / 18.0 / 19.4 — ни один не достигал 23. Enforce на
таком пороге означал бы не отбор, а остановку трендового контура.

Скрипт НЕ назначает порог. Он показывает распределение записанных значений и
то, что каждое условие отсекло бы на истории: сколько сделок, с каким итогом.
Решение принимает человек, глядя на числа.

Запуск:
    python scripts/tz_calibrate.py --export storage/ml/trade_outcomes.jsonl
    python scripts/tz_calibrate.py --export ... --min-sample 40
"""
from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path


def _load(path: Path) -> list[dict]:
    rows = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _shadow_of(row: dict) -> dict | None:
    """Вердикт полосы из выгрузки.

    Основное место — `gates.tz_shadow` (#gates-in-export-2026-08-03). До этой
    правки гейты в выгрузку не писались вовсе, поэтому скрипт находил ноль
    записей на сотне строк. Остальные варианты оставлены для совместимости с
    ручными выгрузками из базы, где структура плана другая.
    """
    for container in (row.get("gates"), row.get("plan"), row.get("plan_json")):
        if isinstance(container, dict):
            shadow = container.get("tz_shadow")
            if isinstance(shadow, dict):
                return shadow if shadow.get("evaluated") else None
    shadow = row.get("tz_shadow")
    return shadow if isinstance(shadow, dict) and shadow.get("evaluated") else None


def _pnl(row: dict) -> float | None:
    for key in ("closed_net_pnl", "net_pnl"):
        value = row.get(key)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                pass
    return None


def _quantiles(values: list[float]) -> dict:
    ordered = sorted(values)
    if not ordered:
        return {}

    def q(p: float) -> float:
        idx = max(0, min(int(round(p * (len(ordered) - 1))), len(ordered) - 1))
        return round(ordered[idx], 2)

    return {
        "n": len(ordered),
        "min": round(ordered[0], 2),
        "q25": q(0.25),
        "median": q(0.50),
        "q75": q(0.75),
        "max": round(ordered[-1], 2),
        "mean": round(statistics.fmean(ordered), 2),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--export", required=True, help="jsonl с закрытыми сделками")
    ap.add_argument("--min-sample", type=int, default=40,
                    help="сколько оценок считать достаточным для решения")
    args = ap.parse_args()

    rows = _load(Path(args.export))
    pairs = []
    for row in rows:
        shadow = _shadow_of(row)
        if shadow is None:
            continue
        pnl = _pnl(row)
        if pnl is None:
            continue
        pairs.append((shadow, pnl))

    with_gates = sum(1 for r in rows if isinstance(r.get("gates"), dict))
    trend_rows = sum(
        1 for r in rows
        if str(r.get("trade_mode") or "").lower() in ("trend", "crt", "position")
    )

    print(f"Строк в выгрузке: {len(rows)}")
    print(f"  из них с блоком gates: {with_gates}")
    print(f"  из них трендовых (полоса считается только там): {trend_rows}")
    print(f"С посчитанной tz_shadow и известным итогом: {len(pairs)}")

    if not pairs:
        print()
        if with_gates == 0:
            print("Ни в одной строке нет блока `gates` — значит выгрузка сделана ДО")
            print("правки #gates-in-export-2026-08-03. Раньше вердикты гейтов жили")
            print("только в signal.plan_json в базе и в журнал не попадали, поэтому")
            print("калибровочных данных не появилось бы никогда, сколько ни ждать.")
            print("Нужны НОВЫЕ закрытия после деплоя правки.")
        elif trend_rows == 0:
            print("Блок gates есть, но трендовых сделок в выгрузке нет.")
            print("Полоса считается только для trend_up/trend_down — ждём таких закрытий.")
        else:
            print("Трендовые сделки есть, но tz_shadow у них не посчитан:")
            print("проверь, что индикаторы (ADX/StochRSI/OBV) отдаются в timeframes.")
        return

    if len(pairs) < args.min_sample:
        print(f"\n!! Выборки НЕДОСТАТОЧНО: {len(pairs)} < {args.min_sample}.")
        print("   Числа ниже показаны для наблюдения, но порог по ним не назначается:")
        print("   калибровка на такой выборке неотличима от подгонки.")

    for name in ("adx", "stoch_k", "di_spread"):
        values = [float(s[name]) for s, _ in pairs if s.get(name) is not None]
        stats = _quantiles(values)
        if stats:
            print(f"\n{name}: {stats}")

    # Что отсекло бы каждое условие и чем те сделки закончились.
    print("\nЭффект условий на истории (что было бы отсечено):")
    families: dict[str, list[float]] = {}
    for shadow, pnl in pairs:
        seen = set()
        for code in shadow.get("failed") or []:
            fam = str(code).split(":", 1)[0]
            fam = {
                "adx_below_min": "adx",
                "di_against_side": "di",
                "stoch_not_in_pullback": "stoch",
                "stoch_k_below_d": "stoch",
                "stoch_k_above_d": "stoch",
                "obv_below_ema": "obv",
                "obv_above_ema": "obv",
            }.get(fam, fam)
            if fam in seen:
                continue
            seen.add(fam)
            families.setdefault(fam, []).append(pnl)

    total = sum(p for _, p in pairs)
    print(f"  Итог всех {len(pairs)} оценённых сделок: {total:+.4f} USDT")
    for fam, blocked in sorted(families.items()):
        saved = -sum(blocked)
        wins = sum(1 for x in blocked if x > 0)
        print(f"  {fam:6s}: отсекло {len(blocked):3d} (из них прибыльных {wins:3d}), "
              f"эффект {saved:+.4f} USDT")

    passed = [p for s, p in pairs if not (s.get("failed") or [])]
    print(f"\n  Прошло бы ВСЕ условия: {len(passed)} сделок, "
          f"итог {sum(passed):+.4f} USDT")
    if len(passed) < 10:
        print("  (меньше 10 прошедших — по такому остатку судить нельзя)")


if __name__ == "__main__":
    main()
