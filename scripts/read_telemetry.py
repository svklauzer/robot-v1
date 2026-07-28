"""Разбор выгрузки телеметрии (#telemetry-snapshot-2026-07-27).

Пара к `collect_telemetry.ps1`: тот выгружает боевые данные в
`analytics_24h/telemetry_*.json`, этот сразу превращает их в сводку и —
главное — извлекает ТРАЕКТОРИИ сделок в формат калибровочных скриптов.

Раньше траектории приходилось переносить в чат вручную километрами curl;
локальный `storage/ml/trade_outcomes.jsonl` для этого не годится — он отстал
на 7 недель (101 запись против 283 на Render).

Запуск:
    python3 scripts/read_telemetry.py                  # последняя выгрузка
    python3 scripts/read_telemetry.py <файл>           # конкретная
    python3 scripts/read_telemetry.py --export-traj    # + calib_trades_live.json
"""
from __future__ import annotations

import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SNAP_DIR = os.path.join(HERE, "..", "analytics_24h")


def _latest() -> str | None:
    files = sorted(glob.glob(os.path.join(SNAP_DIR, "telemetry_*.json")))
    return files[-1] if files else None


def _get(d: dict, *path, default=None):
    for key in path:
        if not isinstance(d, dict):
            return default
        d = d.get(key)
        if d is None:
            return default
    return d


def summarize(snap: dict) -> None:
    data = snap.get("data", {})
    print(f"Выгрузка: {snap.get('collected_at', '?')}")
    if snap.get("errors"):
        print(f"Эндпоинтов с ошибкой: {len(snap['errors'])} — {list(snap['errors'])}")
    print()

    vg = data.get("validation_gates") or {}
    print("=== ГОТОВНОСТЬ К LIVE ===")
    print(f"  ready              : {vg.get('ready')}")
    print(f"  net PnL сырой      : {vg.get('net_pnl_usdt')}")
    print(f"  net PnL ЧЕСТНЫЙ    : {vg.get('net_pnl_honest_usdt')}")
    print(f"  фантомных филлов   : {vg.get('phantom_fill_count')} "
          f"(завышение {vg.get('phantom_fill_overstatement_usdt')})")
    print(f"  positive→negative  : {vg.get('positive_then_negative_rate_pct')}% "
          f"/ max {vg.get('positive_then_negative_max_pct')}%")
    for b in (vg.get("blockers") or []):
        print(f"    ✗ {b}")

    print("\n=== ПРИЧИНЫ ЗАКРЫТИЙ (72ч) ===")
    reasons = _get(data, "daily_quality_72h", "trading", "reasons", default={}) or {}
    for r, n in sorted(reasons.items(), key=lambda x: -x[1]):
        mark = "  ← новый ярус" if r == "trend_capture_band" else ""
        print(f"  {r:28} {n}{mark}")
    if "trend_capture_band" not in reasons:
        print("  (!) trend_capture_band не сработал ни разу")

    print("\n=== КОНТУРЫ ===")
    print(f"  grid realized      : {_get(data, 'grid_state', 'realized_pnl_usdt')}")
    print(f"  cross-arb realized : {_get(data, 'cross_arb', 'realized_total_usdt')}")
    print(f"  funding-arb        : {_get(data, 'funding_summary', 'realized_pnl')}")

    print("\n=== СЕТЬ ===")
    eg = data.get("egress_history") or {}
    print(f"  доступность 24ч    : {eg.get('availability_pct')}%  "
          f"окон недоступности: {len(eg.get('outage_windows') or [])}")

    dc = data.get("depth_coverage") or {}
    if dc.get("status") == "ok":
        w = dc.get("with_depth_confirmation") or {}
        wo = dc.get("without_depth_confirmation") or {}
        print("\n=== ВХОДЫ БЕЗ ПОДТВЕРЖДЕНИЯ СТАКАНОМ ===")
        print(f"  с подтверждением : n={w.get('count')} net={w.get('net_pnl_usdt')} "
              f"avg={w.get('avg_pnl_usdt')}")
        print(f"  без него         : n={wo.get('count')} net={wo.get('net_pnl_usdt')} "
              f"avg={wo.get('avg_pnl_usdt')}")

    # (#expectancy-2026-07-27) Главный блок: win-rate без payoff бессмыслен.
    # 67% побед при payoff 0.29 — убыточная система.
    exp = data.get("expectancy") or {}
    if exp.get("status") == "ok":
        o = exp.get("overall") or {}
        print("\n=== ОЖИДАНИЕ НА СДЕЛКУ ===")
        print(f"  всего            : n={o.get('count')}  winrate={o.get('winrate_pct')}%  "
              f"payoff={o.get('payoff_ratio')}")
        print(f"  ОЖИДАНИЕ         : {o.get('expectancy_usdt')} USDT/сделка  "
              f"(издержки {o.get('cost_share_of_gross_pct')}% от валового хода)")
        bad = [r for r in (exp.get("by_symbol") or []) if r.get("demote")]
        if bad:
            print("  под понижение:")
            for r in bad[:8]:
                print(f"    {r.get('symbol'):12} exp={r.get('expectancy_usdt')} "
                      f"n={r.get('count_with_money')} ptn={r.get('positive_then_negative_pct')}% "
                      f"— {r.get('why')}")
        reasons = [r for r in (exp.get("by_entry_reason") or [])
                   if not str(r.get("entry_reason", "")).startswith("legacy_")]
        if reasons:
            print("  по причинам входа:")
            for r in reasons[:8]:
                print(f"    {str(r.get('entry_reason'))[:34]:34} exp={r.get('expectancy_usdt')} "
                      f"n={r.get('count')}")
        else:
            print("  (!) причины входа ещё не накопились — поле пишется с 27.07")

    # (#venue-expectancy-2026-07-27) Инварианты филлов. Положительный разрыв
    # booked−achievable = запись результата лучше рынка, то есть ошибка учёта.
    ve = data.get("venue_expectancy") or {}
    if ve.get("status") == "ok":
        print("\n=== ПЛОЩАДКИ И ИСПОЛНЕНИЕ ===")
        for row in ve.get("by_market_type") or []:
            print(f"  {row.get('market_type'):8} n={row.get('count'):<4} "
                  f"exp={row.get('expectancy_usdt')}  "
                  f"ср.разрыв={row.get('avg_execution_gap_pct')}%")
        viol = ve.get("invariant_violation_count") or 0
        mark = "  ← РАЗБИРАТЬ" if viol else ""
        print(f"  нарушений инварианта филла: {viol}{mark}")

    # (#walk-forward-2026-07-27) Единственный честный ответ на вопрос «менять ли
    # конфиг»: подбор оценён на данных, которых он не видел.
    print("\n=== WALK-FORWARD (out-of-sample) ===")
    for regime in ("trend", "range", "scalp"):
        wf = data.get(f"wf_{regime}") or {}
        if wf.get("status") != "ok":
            print(f"  {regime:6} — {wf.get('status') or 'нет данных'} "
                  f"(сделок {wf.get('trades', 0)})")
            continue
        print(f"  {regime:6} n={wf.get('trades'):<4} edge={wf.get('oos_edge_pct')}%  "
              f"фолдов выиграно {wf.get('folds_won')}/{wf.get('folds_scored')}  "
              f"разных выборов {wf.get('unique_param_picks')}")
        print(f"         → {wf.get('verdict')}")

    wfh = data.get("wf_history") or {}
    if wfh.get("status") == "ok":
        print(f"\n  дрейф оптимума за {wfh.get('runs')} прогонов:")
        for regime, st in (wfh.get("stability") or {}).items():
            if st.get("runs"):
                print(f"    {regime:6} разных выборов {st.get('distinct_picks')}/{st.get('runs')} "
                      f"— {st.get('verdict')}")


def export_trajectories(snap: dict) -> str:
    """Траектории закрытых сделок в формат калибровочных скриптов."""
    signals = _get(snap, "data", "signals", "items", default=[]) or []
    out = []
    for s in signals:
        if s.get("status") != "closed":
            continue
        plan = s.get("plan") or {}
        lc = plan.get("lifecycle") or {}
        traj = lc.get("traj")
        if not traj or len(traj) < 3:
            continue
        entry = lc.get("entry_price") or 0
        side = str(s.get("side") or "long")
        if not entry:
            continue

        def rp(p):
            p = float(p)
            return ((p - entry) / entry * 100) if side == "long" else ((entry - p) / entry * 100)

        tp = s.get("tp") or {}
        stop = s.get("stop_price")
        stop_pct = abs(rp(stop)) if stop else None
        # стоп на прибыльной стороне = уже переставленный в безубыток после TP1
        if stop and rp(stop) > 0:
            stop_pct = None
        out.append({
            "id": f"#{s.get('id')}",
            "symbol": s.get("symbol"),
            "side": side,
            "entry": entry,
            "notional": float(s.get("required_margin") or 0),
            "trade_mode": (plan.get("trade_mode") or "trend"),
            "regime": plan.get("regime"),
            "stop_pct": stop_pct,
            "tp1_pct": rp(tp.get("tp1")) if tp.get("tp1") else None,
            "tp2_pct": rp(tp.get("tp2")) if tp.get("tp2") else None,
            "actual_gross_pct": round(rp(s["closed_exit_price"]), 4) if s.get("closed_exit_price") else None,
            "mfe_pct": lc.get("mfe_pct"),
            "closed_reason": s.get("closed_reason"),
            "traj": traj,
        })

    path = os.path.join(HERE, "calib_trades_live.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    modes = {}
    for t in out:
        modes[t["trade_mode"]] = modes.get(t["trade_mode"], 0) + 1
    print(f"\n=== ТРАЕКТОРИИ ВЫГРУЖЕНЫ ===")
    print(f"  {path}")
    print(f"  сделок с траекторией: {len(out)}  по режимам: {modes}")
    print("  → калибровочные скрипты могут читать этот файл вместо ручного переноса")
    return path


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    path = args[0] if args else _latest()
    if not path or not os.path.exists(path):
        print("Выгрузок не найдено. Сначала:  .\\scripts\\collect_telemetry.ps1")
        raise SystemExit(1)

    with open(path, "r", encoding="utf-8-sig") as f:
        snap = json.load(f)

    summarize(snap)
    if "--export-traj" in sys.argv:
        export_trajectories(snap)


if __name__ == "__main__":
    main()
