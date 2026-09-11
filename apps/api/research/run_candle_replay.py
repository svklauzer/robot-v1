"""Запуск стенда динамических SL/TP (#dynamic-levels-2026-09-12).

    python -m research.run_candle_replay --export C:/Users/svk/robot-export \\
        [--max-hold-hours 72] [--mode trend]

Сделки — из файлов выгрузки `/signals` (signals_*.json в папке --export),
свечи — публичный своп OKX, 15m, кешируются в <export>/candles. Печатает по
каждому правилу сумму и среднее нетто в % номинала, долю прибыльных и разницу с
нынешней схемой; отдельно — нынешняя схема против факта (точность стенда).
"""
from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path

from research.candle_replay import (
    BAR_SEC, atr_pct, kama_pct, load_trades, net_pct, path_pct, replay,
)

WARMUP_BARS = 100
# Неблагоприятное проскальзывание выхода по стопу — как у настоящего стопа в
# paper (PAPER_STOP_ADVERSE_SLIPPAGE_PCT), во всех вариантах одинаково.
STOP_SLIP = 0.05
POLICIES: tuple[tuple[str, str, dict], ...] = (
    ("current", "current", {}),
    ("atr_trail_k2", "atr_trail", {"k": 2.0}),
    ("atr_trail_k3", "atr_trail", {"k": 3.0}),
    ("chandelier_k3", "chandelier", {"k": 3.0}),
    ("kama", "kama", {}),
    ("be_atr_k1", "be_atr", {"k": 1.0}),
    # Без фиксации половины на TP1: стоп всей позиции переносится на TP1.
    ("full_lock_tp2", "current", {"partial": 0.0}),
    ("full_lock_trail_k2", "atr_trail", {"k": 2.0, "partial": 0.0}),
    ("full_lock_trail_k3", "atr_trail", {"k": 3.0, "partial": 0.0}),
    ("full_lock_kama", "kama", {"partial": 0.0}),
)


def _swap_symbol(symbol: str) -> str:
    base = symbol.split(":", 1)[0]
    quote = base.split("/", 1)[1] if "/" in base else "USDT"
    return f"{base}:{quote}"


def fetch_range(ex, symbol: str, start_ms: int, end_ms: int, cache_dir: Path) -> list[list[float]]:
    """15m свечи символа на [start, end] с кешем на диске (слияние по ts)."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / (symbol.replace("/", "_").replace(":", "_") + "_15m.json")
    bars: dict[int, list[float]] = {}
    if path.exists():
        bars = {int(b[0]): b for b in json.loads(path.read_text(encoding="utf-8"))}
    have = sorted(bars)
    if not have or have[0] > start_ms or have[-1] < end_ms - BAR_SEC * 1000:
        since = start_ms
        while since < end_ms:
            chunk = ex.fetch_ohlcv(_swap_symbol(symbol), "15m", since=since, limit=300)
            if not chunk:
                break
            for b in chunk:
                bars[int(b[0])] = b
            nxt = int(chunk[-1][0]) + BAR_SEC * 1000
            if nxt <= since:
                break
            since = nxt
        path.write_text(json.dumps([bars[k] for k in sorted(bars)]), encoding="utf-8")
    return [bars[k] for k in sorted(bars) if start_ms <= k <= end_ms]


def run(export: Path, max_hold_hours: float = 72.0, mode: str | None = None) -> dict:
    import ccxt

    trades = load_trades(sorted(export.glob("signals_*.json")))
    if mode:
        trades = [t for t in trades if t.trade_mode == mode]
    ex = ccxt.okx({"enableRateLimit": True, "options": {"defaultType": "swap"}})
    hold = max_hold_hours * 3600

    by_symbol: dict[str, list] = {}
    for t in trades:
        by_symbol.setdefault(t.symbol, []).append(t)

    rows: list[dict] = []
    skipped: list[str] = []
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
            # Путь — со свечи ПОСЛЕ той, в которой случился вход.
            idx = next((i for i, c in enumerate(candles) if c[0] > entry_ms), None)
            if idx is None or idx < WARMUP_BARS // 2:
                skipped.append(f"#{t.id} {symbol}: мало свечей")
                continue
            last = next((i for i, c in enumerate(candles) if c[0] > entry_ms + hold * 1000),
                        len(candles))
            start_i = WARMUP_BARS // 2
            window = candles[idx - start_i: last]
            # Индикаторы — по окну с прогревом до входа, правило — с входа.
            warm = path_pct(t, window)
            atr_full, kama_full = atr_pct(warm), kama_pct(t, window)
            path = warm[start_i:]
            if not path:
                continue
            row = {"id": t.id, "symbol": symbol, "side": t.side, "mode": t.trade_mode,
                   "actual": t.actual_pct}
            for name, policy, kw in POLICIES:
                for pess in (True, False):
                    res = replay(t, path, policy, pessimistic=pess, stop_slip=STOP_SLIP,
                                 atr=atr_full[start_i:], kama=kama_full[start_i:], **kw)
                    row[f"{name}|{'pess' if pess else 'opt'}"] = net_pct(t, res)
            rows.append(row)
    return {"rows": rows, "skipped": skipped}


def summarize(rows: list[dict]) -> list[dict]:
    if not rows:
        return []
    out = []
    base = {m: [r[f"current|{m}"] for r in rows] for m in ("pess", "opt")}
    actual = [r["actual"] for r in rows]
    for name, _, _ in POLICIES:
        for m in ("pess", "opt"):
            vals = [r[f"{name}|{m}"] for r in rows]
            out.append({
                "policy": name, "mode": m, "n": len(vals),
                "sum_pct": round(sum(vals), 2),
                "mean_pct": round(statistics.mean(vals), 4),
                "median_pct": round(statistics.median(vals), 4),
                "win_rate": round(sum(1 for v in vals if v > 0) / len(vals), 3),
                "vs_current_pct": round(sum(vals) - sum(base[m]), 2),
            })
    out.append({"policy": "ACTUAL", "mode": "-", "n": len(actual),
                "sum_pct": round(sum(actual), 2),
                "mean_pct": round(statistics.mean(actual), 4),
                "median_pct": round(statistics.median(actual), 4),
                "win_rate": round(sum(1 for v in actual if v > 0) / len(actual), 3),
                "vs_current_pct": None})
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--export", required=True)
    parser.add_argument("--max-hold-hours", type=float, default=72.0)
    parser.add_argument("--mode", default=None)
    args = parser.parse_args()
    out = run(Path(args.export), args.max_hold_hours, args.mode)
    for row in summarize(out["rows"]):
        print(row)
    if out["skipped"]:
        print("skipped:", len(out["skipped"]), out["skipped"][:10])
    Path(args.export, "replay_rows.json").write_text(json.dumps(out["rows"]), encoding="utf-8")


if __name__ == "__main__":
    main()
