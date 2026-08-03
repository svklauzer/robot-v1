#!/usr/bin/env python3
"""Что изменилось после правок 31.07: маршрут, издержки, стакан, триггер.

Разделение когорт — по наличию `plan_json.trend_trigger`: это поле появилось
вместе с правками, поэтому отделяет сделки старой логики от новой точнее,
чем дата.

Правила счёта, которые здесь соблюдаются (на них уже ошибались):
  * `result_pct` и `lifecycle.final_result_pct` записаны УЖЕ НЕТТО;
    валовый = closed_net_pnl + closed_total_cost.
  * Издержки учитываются ровно один раз с каждой стороны сравнения.
  * Малая выборка помечается явно, вывод «стало лучше» без интервала
    не делается.

Запуск:
    python scripts/session_report.py analytics_24h\\signals_export_<...>.json
"""
from __future__ import annotations

import argparse
import json
import random
import statistics as st
from collections import Counter, defaultdict
from pathlib import Path

BOOT = 20000
MIN_CI = 12


def plan(row):
    return row.get("plan_json") or row.get("plan") or {}


def lifecycle(row):
    return plan(row).get("lifecycle") or {}


def regime(row):
    return str(plan(row).get("regime") or "?")


def notional(row):
    lc = lifecycle(row)
    try:
        return float(lc.get("entry_price") or 0) * float(plan(row).get("qty") or row.get("qty") or 0)
    except (TypeError, ValueError):
        return 0.0


def net(row):
    try:
        return float(row.get("closed_net_pnl") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def cost(row):
    try:
        return float(row.get("closed_total_cost") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def gross(row):
    return net(row) + cost(row)


def risk(row):
    try:
        return abs(float(row.get("net_pnl_stop") or 0.0))
    except (TypeError, ValueError):
        return 0.0


def is_new(row):
    return isinstance(plan(row).get("trend_trigger"), dict)


def route(row):
    r = plan(row).get("routing") or {}
    return str(r.get("market_type") or "?")


def rt_pct(row):
    cfg = (plan(row).get("config") or {}).get("market") or {}
    try:
        return float(cfg.get("round_trip_pct"))
    except (TypeError, ValueError):
        return None


def depth_fresh(row):
    d = plan(row).get("entry_depth")
    if not isinstance(d, dict):
        return None
    return bool(d.get("fresh"))


def boot_ci(values, iters=BOOT):
    if len(values) < MIN_CI:
        return None
    n = len(values)
    out = []
    for _ in range(iters):
        out.append(sum(random.choice(values) for _ in range(n)) / n)
    out.sort()
    return out[int(0.025 * iters)], out[int(0.975 * iters)]


def line(title):
    print("\n" + "=" * 78)
    print(title)
    print("-" * 78)


def econ(rows, label):
    if not rows:
        print(f"  {label:<28} нет сделок")
        return
    g, c, n_ = sum(gross(r) for r in rows), sum(cost(r) for r in rows), len(rows)
    wins = sum(1 for r in rows if net(r) > 0)
    print(f"  {label:<28} n={n_:<4} валовый {g:+8.2f}  издержки {-c:8.2f}  "
          f"net {g - c:+8.2f}  ({(g - c) / n_:+.4f}/сделка, win {100 * wins / n_:.0f}%)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("export")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    random.seed(args.seed)

    payload = json.loads(Path(args.export).read_text(encoding="utf-8"))
    items = payload.get("items") if isinstance(payload, dict) else payload
    items = list(items or [])
    closed = [r for r in items if r.get("status") == "closed" and r.get("closed_net_pnl") is not None]
    closed.sort(key=lambda r: str(r.get("created_at") or ""))

    old = [r for r in closed if not is_new(r)]
    new = [r for r in closed if is_new(r)]

    print(f"выгрузка: {payload.get('collected_at')}")
    print(f"всего сигналов {len(items)}, закрытых {len(closed)}")
    print(f"старая логика {len(old)}, новая (есть trend_trigger) {len(new)}")
    if closed:
        print(f"период: {str(closed[0].get('created_at'))[:16]} .. {str(closed[-1].get('created_at'))[:16]}")

    # ── 1. Маршрут ──────────────────────────────────────────────────────────
    line("1. МАРШРУТ И ТАРИФ — ушли ли лонги на своп")
    for label, rows in (("старые", old), ("новые", new)):
        by = defaultdict(Counter)
        for r in rows:
            by[r.get("side")][route(r)] += 1
        print(f"  {label}: " + ("; ".join(f"{s}: {dict(c)}" for s, c in by.items()) or "нет данных"))
    print()
    for label, rows in (("старые", old), ("новые", new)):
        vals = defaultdict(list)
        for r in rows:
            v = rt_pct(r)
            if v is not None:
                vals[r.get("side")].append(v)
        for side, v in vals.items():
            print(f"  {label} {side:<6} round_trip медиана {st.median(v):.3f}%  "
                  f"(мин {min(v):.3f} макс {max(v):.3f}, n={len(v)})")

    # ── 2. Экономика ────────────────────────────────────────────────────────
    line("2. ЭКОНОМИКА — валовый и издержки раздельно")
    econ(old, "старая логика")
    econ(new, "новая логика")
    print()
    for label, rows in (("старые", old), ("новые", new)):
        for side in ("long", "short"):
            econ([r for r in rows if r.get("side") == side], f"{label} {side}")

    # ── 3. Стакан ───────────────────────────────────────────────────────────
    line("3. СТАКАН — поднялся ли своповый фид (OB_MARKET_TYPE=swap)")
    for label, rows in (("старые", old), ("новые", new)):
        vals = [depth_fresh(r) for r in rows]
        have = [v for v in vals if v is not None]
        none_n = sum(1 for v in vals if v is None)
        if not rows:
            continue
        fresh = sum(1 for v in have if v)
        print(f"  {label}: без entry_depth {none_n}/{len(rows)}, "
              f"fresh {fresh}/{len(have) or 1} "
              f"({100 * fresh / (len(have) or 1):.0f}%)")
    print("\n  Режимы, которым книга обязательна (scalp/range/crt) — сколько закрылось:")
    for label, rows in (("старые", old), ("новые", new)):
        c = Counter(regime(r) for r in rows)
        print(f"    {label}: {dict(c)}")

    # ── 4. Триггер ──────────────────────────────────────────────────────────
    line("4. TREND_TRIGGER — распределение растянутости и калибровка порога")
    ext = []
    for r in new:
        t = plan(r).get("trend_trigger") or {}
        v = t.get("extension_atr")
        if v is not None and t.get("regime") in ("trend_up_candidate", "trend_down_candidate"):
            ext.append((float(v), r))
    if len(ext) < 5:
        print(f"  трендовых сделок с замером: {len(ext)} — мало для калибровки")
    else:
        vals = sorted(v for v, _ in ext)
        qs = {p: vals[min(int(p * len(vals)), len(vals) - 1)] for p in (0.1, 0.25, 0.5, 0.75, 0.9)}
        print(f"  n={len(vals)}  медиана {qs[0.5]:+.3f} ATR  "
              f"p25 {qs[0.25]:+.3f}  p75 {qs[0.75]:+.3f}  p90 {qs[0.9]:+.3f}")
        print("\n  что дал бы порог (отсекаем extension > X):")
        print(f"  {'порог':<8}{'отсечено':>10}{'их net':>10}{'осталось':>10}{'net остатка':>13}")
        for thr in (0.5, 1.0, 1.5, 2.0, 2.5, 3.0):
            cut = [r for v, r in ext if v > thr]
            keep = [r for v, r in ext if v <= thr]
            print(f"  {thr:<8.1f}{len(cut):>10}{sum(net(r) for r in cut):>10.2f}"
                  f"{len(keep):>10}{sum(net(r) for r in keep):>13.2f}")
        print("\n  ВНИМАНИЕ: порог, выбранный по этой же таблице, — подгонка.")
        print("  Смотреть надо на монотонность, а не на лучшую строку.")

    # ── 5. Гейты ────────────────────────────────────────────────────────────
    line("5. VALIDATION GATES — окно 50 последних закрытий")
    window = sorted(closed, key=lambda r: r.get("id") or 0, reverse=True)[:50]
    if len(window) < 50:
        print(f"  закрытий всего {len(window)} — окно неполное")
    if window:
        n_ = sum(net(r) for r in window)
        ph = 0
        for r in window:
            lc = lifecycle(r)
            try:
                if float(r.get("result_pct")) > float(lc.get("mfe_pct")) + 1e-9:
                    ph += 1
            except (TypeError, ValueError):
                pass
        lcs = [lifecycle(r) for r in window if lifecycle(r)]
        ptn = 100 * sum(1 for l in lcs if l.get("positive_then_negative")) / (len(lcs) or 1)
        fs = 100 * sum(1 for r in window if r.get("closed_reason") == "failed_setup_exit") / len(window)
        print(f"  net {n_:+.2f} | фантомных филлов {ph} | positive_then_negative {ptn:.1f}% | "
              f"failed_setup {fs:.1f}%")
        print(f"  гейты: net>0={n_ > 0}  no_phantom={ph == 0}  ptn<25={ptn < 25}  "
              f"failed<35={fs < 35}  closed>=50={len(window) >= 50}")

    # ── 6. Мощность ─────────────────────────────────────────────────────────
    line("6. ХВАТАЕТ ЛИ ВЫБОРКИ")
    for label, rows in (("старая логика", old), ("новая логика", new)):
        per = [net(r) / risk(r) for r in rows if risk(r) > 0]
        if len(per) < 3:
            print(f"  {label:<16} n={len(per)} — судить не о чем")
            continue
        m, sd = st.mean(per), st.pstdev(per)
        ci = boot_ci(per)
        need = int((2.8 * sd / abs(m)) ** 2) if abs(m) > 1e-9 else None
        need_s = f"{need:,}" if need and need < 10 ** 7 else "∞"
        ci_s = f"[{ci[0]:+.3f}; {ci[1]:+.3f}]" if ci else "мало данных"
        print(f"  {label:<16} n={len(per):<4} среднее {m:+.4f}R  95% ДИ {ci_s}  "
              f"для значимости нужно ~{need_s}")

    print("\n" + "=" * 78)
    print("Разделы 1–3 — проверка, что правки применились (это факты).")
    print("Раздел 4 — калибровка. Раздел 6 — можно ли вообще делать вывод о прибыли.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
