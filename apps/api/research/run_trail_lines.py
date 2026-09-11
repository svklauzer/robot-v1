"""Запуск стенда линий трейла после TP1 (#trail-lines-2026-09-11).

    python -m research.run_trail_lines --export C:/Users/svk/robot-export [--max-hold-hours 72]

Сделки — из signals_*.json выгрузки, свечи — своп OKX 15m из кеша стенда
(research.run_candle_replay.fetch_range). Эталон — живая схема (`live`); по
каждому варианту — сумма нетто в п.п. номинала в обоих режимах касания, разница
с эталоном с 95% интервалом (парная, по сделкам), по половинам периода и по
режимам сделки. Для линий в форме ride — разбор: где линия стоит относительно
фиксации, сколько держит, сколько отдаёт и что цена делала после выхода.

Вариантов много, и лучший из них выглядит хорошо отчасти случайно. Поэтому
верить стоит тому, что держится в ОБЕИХ половинах периода и в обоих режимах
касания, а не максимуму таблицы.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import time
from collections import Counter
from pathlib import Path

from research.candle_replay import BAR_SEC, atr_pct, kama_pct, load_trades, net_pct, path_pct, replay
from research.run_candle_replay import fetch_range
from research.trail_lines import (
    Rule, after_exit, anchored_vwap, kama_hourly, replay_trail, rolling_vwap,
    session_vwap, typical,
)

# Прогрев индикаторов: 5 суток. KAMA стартует с сырого закрытия и на топтании
# сходится медленно (SC ≈ 0.004 за бар), а часовой KAMA нужны десятки часов.
WARMUP_BARS = 480
MIN_WARM_BARS = 200
STOP_SLIP = 0.05          # как в run_candle_replay: проскальзывание выхода по стопу

LINE_RULES: tuple[Rule, ...] = (
    Rule("kama15", ("kama15",)),
    Rule("kama15_2bar", ("kama15",), confirm=2),
    Rule("kama1h", ("kama1h",)),
    Rule("vwap_session", ("vwap_session",)),
    Rule("vwap_roll20", ("vwap_roll20",)),
    Rule("avwap_entry", ("avwap_entry",)),
    Rule("avwap_tp1", ("avwap_tp1",)),
    Rule("kama15&vwap_session", ("kama15", "vwap_session"), "and"),
    Rule("kama15|vwap_session", ("kama15", "vwap_session"), "or"),
    Rule("kama15&avwap_tp1", ("kama15", "avwap_tp1"), "and"),
    Rule("kama15|avwap_tp1", ("kama15", "avwap_tp1"), "or"),
)
LIVE = Rule("live", (), shape="live")


def variants() -> list[tuple[str, object]]:
    out: list[tuple[str, object]] = [("live", LIVE)]
    # Прежние варианты стенда (research.candle_replay.replay), без фиксации на TP1.
    out += [("tp2_ceiling", ("current", {"partial": 0.0})),
            ("atr_k2", ("atr_trail", {"k": 2.0, "partial": 0.0})),
            ("atr_k3", ("atr_trail", {"k": 3.0, "partial": 0.0}))]
    for r in LINE_RULES:
        out.append((r.name, r))
    for r in LINE_RULES:
        out.append((f"{r.name}+tp2", Rule(r.name, r.lines, r.combine, r.confirm, "tp2")))
    return out


def trade_lines(t, window: list[list[float]], start_i: int) -> tuple[list, list, dict, list]:
    warm = path_pct(t, window)
    ts = [int(c[0]) for c in window]
    vol = [float(c[5]) if len(c) > 5 and c[5] else 0.0 for c in window]
    typ = typical(warm)
    full = {
        "kama15": kama_pct(t, window),
        "kama1h": kama_hourly(t, window),
        "vwap_session": session_vwap(typ, vol, ts),
        "vwap_roll20": rolling_vwap(typ, vol, 20),
        "avwap_entry": anchored_vwap(typ, vol, start_i),
    }
    lines = {k: v[start_i:] for k, v in full.items()}
    return warm[start_i:], vol[start_i:], lines, atr_pct(warm)[start_i:]


def run(export: Path, max_hold_hours: float = 72.0) -> dict:
    import ccxt

    trades = load_trades(sorted(export.glob("signals_*.json")))
    ex = ccxt.okx({"enableRateLimit": True, "options": {"defaultType": "swap"}})
    hold = max_hold_hours * 3600
    by_symbol: dict[str, list] = {}
    for t in trades:
        by_symbol.setdefault(t.symbol, []).append(t)

    rows, skipped = [], []
    specs = variants()
    for symbol, items in by_symbol.items():
        start = int((min(t.opened_ts for t in items) - WARMUP_BARS * BAR_SEC) * 1000)
        end = int((max(t.opened_ts for t in items) + hold) * 1000)
        try:
            candles = fetch_range(ex, symbol, start, min(end, int(time.time() * 1000)),
                                  export / "candles")
        except Exception as exc:  # noqa: BLE001
            skipped.append(f"{symbol}: {type(exc).__name__}: {exc}")
            continue
        for t in items:
            entry_ms = int(t.opened_ts * 1000)
            idx = next((i for i, c in enumerate(candles) if c[0] > entry_ms), None)
            if idx is None or idx < MIN_WARM_BARS:
                skipped.append(f"#{t.id} {symbol}: мало свечей до входа")
                continue
            last = next((i for i, c in enumerate(candles) if c[0] > entry_ms + hold * 1000),
                        len(candles))
            start_i = min(idx, WARMUP_BARS)
            path, vol, lines, atr = trade_lines(t, candles[idx - start_i: last], start_i)
            if not path:
                continue
            row = {"id": t.id, "symbol": symbol, "side": t.side, "mode": t.trade_mode,
                   "opened_ts": t.opened_ts, "actual": t.actual_pct}
            for name, spec in specs:
                for pess in (True, False):
                    key = f"{name}|{'pess' if pess else 'opt'}"
                    if isinstance(spec, Rule):
                        res = replay_trail(t, path, spec, lines=lines, vol=vol,
                                           pessimistic=pess, stop_slip=STOP_SLIP)
                        if pess and spec.shape == "ride":
                            row[f"{name}|diag"] = {
                                "tp1_bar": res.tp1_bar, "exit_bar": res.exit_bar,
                                "reason": res.reason, "peak": res.peak,
                                "exit_pct": res.exits[-1].pct if res.exits else None,
                                "binding": res.binding_bars, "watched": res.watched_bars,
                                "after": after_exit(t, path, res),
                            }
                    else:
                        policy, kw = spec
                        res = replay(t, path, policy, pessimistic=pess, stop_slip=STOP_SLIP,
                                     atr=atr, **kw)
                    row[key] = net_pct(t, res)
            rows.append(row)
    return {"rows": rows, "skipped": skipped}


def _ci(diffs: list[float]) -> tuple[float, float]:
    """Сумма парных разниц и полуширина её 95% интервала."""
    if len(diffs) < 2:
        return (sum(diffs), float("nan"))
    return (sum(diffs), 1.96 * statistics.stdev(diffs) * math.sqrt(len(diffs)))


def summarize(rows: list[dict]) -> list[dict]:
    if not rows:
        return []
    cut = statistics.median(r["opened_ts"] for r in rows)
    out = []
    for name, _ in variants():
        rec = {"variant": name}
        for m in ("pess", "opt"):
            vals = [r[f"{name}|{m}"] for r in rows]
            d = [r[f"{name}|{m}"] - r[f"live|{m}"] for r in rows]
            s, h = _ci(d)
            rec[f"sum_{m}"] = round(sum(vals), 1)
            rec[f"d_{m}"] = round(s, 1)
            rec[f"ci_{m}"] = round(h, 1)
        rec["win_pess"] = round(sum(1 for r in rows if r[f"{name}|pess"] > 0) / len(rows), 3)
        for label, pick in (("early", lambda r: r["opened_ts"] < cut),
                            ("late", lambda r: r["opened_ts"] >= cut),
                            ("trend", lambda r: r["mode"] == "trend"),
                            ("scalp", lambda r: r["mode"] != "trend")):
            sub = [r for r in rows if pick(r)]
            for m in ("pess", "opt"):
                rec[f"d_{label}_{m}"] = round(sum(r[f"{name}|{m}"] - r[f"live|{m}"] for r in sub), 1)
        out.append(rec)
    return out


def diagnose(rows: list[dict]) -> list[dict]:
    """Разбор линий в форме ride (пессимистичный режим) по сделкам, дошедшим до TP1."""
    out = []
    for r in LINE_RULES:
        diags = [row[f"{r.name}|diag"] for row in rows if row[f"{r.name}|diag"]["tp1_bar"] is not None]
        if not diags:
            continue
        reasons = Counter(d["reason"] for d in diags)
        line = [d for d in diags if d["reason"] == "line"]
        after = Counter(d["after"] for d in line)
        hold_h = [(d["exit_bar"] - d["tp1_bar"]) * BAR_SEC / 3600 for d in diags if d["exit_bar"] is not None]
        watched = sum(d["watched"] for d in diags)
        out.append({
            "line": r.name, "n_tp1": len(diags),
            "exit_line": round(reasons["line"] / len(diags), 3),
            "exit_lock": round(reasons["lock_stop"] / len(diags), 3),
            "exit_cap": round(reasons["time_cap"] / len(diags), 3),
            "hold_after_tp1_h": round(statistics.median(hold_h), 2) if hold_h else None,
            "giveback_pct": round(statistics.median(d["peak"] - d["exit_pct"] for d in line), 3) if line else None,
            "exit_gross_pct": round(statistics.median(d["exit_pct"] for d in line), 3) if line else None,
            "premature": round(after["premature"] / len(line), 3) if line else None,
            "justified": round(after["justified"] / len(line), 3) if line else None,
            "line_above_lock": round(sum(d["binding"] for d in diags) / watched, 3) if watched else None,
        })
    return out


def _print(rows: list[dict], cols: list[str]) -> None:
    widths = {c: max(len(c), *(len(str(r.get(c))) for r in rows)) for c in cols}
    print("  ".join(c.ljust(widths[c]) for c in cols))
    for r in rows:
        print("  ".join(str(r.get(c)).ljust(widths[c]) for c in cols))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--export", required=True)
    parser.add_argument("--max-hold-hours", type=float, default=72.0)
    args = parser.parse_args()
    export = Path(args.export)
    out = run(export, args.max_hold_hours)
    rows = out["rows"]
    print(f"сделок: {len(rows)}, пропущено: {len(out['skipped'])}")
    print(f"ACTUAL сумма: {sum(r['actual'] for r in rows):.1f} п.п.")
    _print(summarize(rows), ["variant", "sum_pess", "sum_opt", "d_pess", "ci_pess", "d_opt", "ci_opt",
                             "d_early_pess", "d_late_pess", "d_early_opt", "d_late_opt",
                             "d_trend_pess", "d_scalp_pess", "win_pess"])
    print()
    _print(diagnose(rows), ["line", "n_tp1", "exit_line", "exit_lock", "exit_cap", "hold_after_tp1_h",
                            "giveback_pct", "exit_gross_pct", "premature", "justified", "line_above_lock"])
    if out["skipped"]:
        print("skipped:", out["skipped"][:10])
    (export / "trail_rows.json").write_text(json.dumps(rows), encoding="utf-8")


if __name__ == "__main__":
    main()
