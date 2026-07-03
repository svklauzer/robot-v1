"""Offline A/B exit-параметров по записанным траекториям сделок (#audit-traj).

Прогоняет варианты scalp-exit конфига (arm / giveback / time-stop) по
lifecycle.traj из trade_outcomes.jsonl и отвечает result-based: какой набор
параметров дал бы лучший суммарный результат на РЕАЛЬНЫХ траекториях.

Инварианты честности:
  - replay может закрыть сделку только РАНЬШЕ фактического закрытия; если
    правило не сработало — берём фактический final_result_pct (как и было);
  - издержки у всех вариантов одинаковы (ровно один выход) → сравнение по
    gross-% корректно, комиссии сокращаются;
  - сделки без траектории (старые логи) пропускаются и честно считаются.

Только чтение датасета. На торговлю не влияет.
"""
from __future__ import annotations

import json
from itertools import product
from pathlib import Path

from core.config import settings


def _load_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows


def _replay_one(traj: list, final_pct: float, *, arm: float, give: float,
                ts_min: float | None, hard_mult: float) -> tuple[float, str]:
    """Возвращает (result_pct, exit_reason) для варианта конфига."""
    mfe = 0.0
    ts_sec = (ts_min or 0.0) * 60.0
    hard_sec = ts_sec * max(hard_mult, 1.0)
    for point in traj:
        try:
            age, pct = float(point[0]), float(point[1])
        except (TypeError, ValueError, IndexError):
            continue
        mfe = max(mfe, pct)
        # scalp breakeven lock: вооружились и отдали долю пика
        if mfe >= arm and mfe > 0 and (mfe - pct) >= mfe * give:
            return pct, "replay_breakeven_lock"
        # time stop (с grace до hard: не в значимом минусе → держим)
        if ts_min and age >= ts_sec and mfe < arm:
            net_safe = float(getattr(settings, "NET_SAFE_FLOOR_SWAP_PCT", 0.30))
            if age >= hard_sec or pct <= -net_safe:
                return pct, "replay_time_stop"
    return final_pct, "actual_close"


def build(limit: int = 2000) -> dict:
    from services.ml_trade_logger import MLTradeLogger
    path = MLTradeLogger().path
    rows = _load_rows(Path(path))[-int(limit):]

    trades = []
    skipped_no_traj = 0
    for r in rows:
        lc = r.get("lifecycle") or {}
        traj = lc.get("traj")
        # replay применим к scalp/range-профилю ведения
        mode = str(r.get("trade_mode") or "").lower()
        if mode not in ("scalp", "range"):
            continue
        final_pct = r.get("result_pct")
        if final_pct is None:
            continue
        if not traj or len(traj) < 3:
            skipped_no_traj += 1
            continue
        trades.append({"traj": traj, "final_pct": float(final_pct),
                       "symbol": r.get("symbol"), "signal_id": r.get("signal_id")})

    if not trades:
        return {
            "status": "no_data",
            "scalp_closed_total": skipped_no_traj,
            "with_trajectory": 0,
            "message": ("Нет scalp/range-сделок с записанной траекторией. Траектории "
                        "пишутся с момента включения TRAJ_RECORD_ENABLED — подожди новых закрытий."),
        }

    arms = [0.3, 0.5, 0.7]
    gives = [0.4, 0.5, 0.6]
    time_stops = [45.0, 90.0, None]  # None = time-stop off
    hard_mult = float(getattr(settings, "SCALP_TIME_STOP_HARD_MULT", 2.0))

    actual_total = round(sum(t["final_pct"] for t in trades), 4)
    variants = []
    for arm, give, ts in product(arms, gives, time_stops):
        total = 0.0
        wins = 0
        early_exits = 0
        for t in trades:
            pct, reason = _replay_one(t["traj"], t["final_pct"],
                                      arm=arm, give=give, ts_min=ts, hard_mult=hard_mult)
            total += pct
            wins += 1 if pct > 0 else 0
            early_exits += 1 if reason != "actual_close" else 0
        variants.append({
            "arm_pct": arm,
            "giveback_share": give,
            "time_stop_min": ts,
            "total_pct": round(total, 4),
            "delta_vs_actual_pct": round(total - actual_total, 4),
            "winrate_pct": round(wins / len(trades) * 100, 1),
            "early_exits": early_exits,
        })
    variants.sort(key=lambda v: v["total_pct"], reverse=True)

    current = {
        "arm_pct": float(getattr(settings, "SCALP_BREAKEVEN_ARM_PCT", 0.5)),
        "giveback_share": float(getattr(settings, "SCALP_BREAKEVEN_GIVEBACK_SHARE", 0.6)),
        "time_stop_min": (float(getattr(settings, "SCALP_TIME_STOP_MIN", 45.0))
                          if bool(getattr(settings, "SCALP_TIME_STOP_ENABLED", True)) else None),
    }

    return {
        "status": "ok",
        "trades_replayed": len(trades),
        "skipped_no_trajectory": skipped_no_traj,
        "actual_total_pct": actual_total,
        "current_config": current,
        "best": variants[0],
        "worst": variants[-1],
        "variants": variants,
        "note": ("Сравнение по gross-% (издержки у вариантов одинаковы). Replay закрывает "
                 "только РАНЬШЕ факта; траектория даунсемплирована (шаг traj_step) → "
                 "результат консервативная оценка. Выборка <30 сделок = не доказательство."),
    }
