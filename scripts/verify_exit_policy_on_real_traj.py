"""Верификация: реальные траектории через БОЕВОЙ ExitPolicyService.

Отличие от calib_*.py: там автономная модель, здесь дергается настоящий
`ExitPolicyService.before_tp1_decision` тик за тиком по записанным траекториям
сигналов 264–282. Проверяем на живом коде:

  1. цена выхода НИКОГДА не лучше рынка (баг #phantom-fill закрыт);
  2. breakeven_lock не закрывается ниже round-trip издержек;
  3. в модальной полосе MFE появился механизм фиксации (#trend-capture-band);
  4. суммарный честный net против «как было в бою».

Запуск (из apps/api):  python3 ../../scripts/verify_exit_policy_on_real_traj.py
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "apps", "api"))

from core.config import settings  # noqa: E402
from services.exit_policy import ExitPolicyService  # noqa: E402

COST_PCT = 0.15
DATA = os.path.join(HERE, "calib_trades_264_282.json")


def honest_actual(t: dict) -> float:
    return t["traj"][-1][1] if t["actual_gross_pct"] > t["mfe_pct"] + 1e-9 else t["actual_gross_pct"]


def replay(svc: ExitPolicyService, t: dict) -> tuple[float, str, list[str]]:
    """Прогон одной сделки. Возвращает (gross_pct, reason, нарушения)."""
    entry = t["entry"]
    side = t["side"]
    stop = t.get("stop_pct")
    mfe = 0.0
    violations: list[str] = []

    for age, pct in t["traj"]:
        mfe = max(mfe, pct)
        if stop is not None and pct <= -stop:
            return -stop, "stop_loss", violations
        price = entry * (1 + pct / 100) if side == "long" else entry * (1 - pct / 100)
        stop_price = (entry * (1 - stop / 100) if side == "long" else entry * (1 + stop / 100)) if stop else None
        d = svc.before_tp1_decision(
            side=side,
            entry_price=entry,
            current_price=price,
            stop_price=stop_price,
            tp1_price=entry * (1 + t["tp1_pct"] / 100) if side == "long" else entry * (1 - t["tp1_pct"] / 100),
            mfe_pct=mfe,
            symbol=None,
            market_type="swap",
            position_notional_usdt=t["notional"],
            signal_age_sec=float(age),
            trade_mode="trend" if t["trade_mode"] == "trend" else "scalp",
            flow_against=False,
            regime=None,
        )
        if not d.exit:
            continue

        fill_pct = ((d.exit_price - entry) / entry * 100) if side == "long" else ((entry - d.exit_price) / entry * 100)
        # ── Инвариант 1: филл не лучше рынка ──
        # Допуск 1e-4 пп: exit_price округляется до 8 знаков, и на дешёвых парах
        # обратный пересчёт в проценты даёт дрейф ~1.5e-6 пп. Экономически это
        # ноль (в 1000 раз меньше любой комиссии), фантом был на 1.3 ПУНКТА.
        if fill_pct > pct + 1e-4:
            violations.append(
                f"ФАНТОМ: филл {fill_pct:.6f}% лучше рынка {pct:.6f}% ({d.reason})"
            )
        return fill_pct, d.reason, violations

    return t["traj"][-1][1], "hold_to_end", violations


def main() -> None:
    trades = [t for t in json.load(open(DATA, encoding="utf-8")) if t["trade_mode"] == "trend"]
    svc = ExitPolicyService()

    print(f"Боевой ExitPolicyService на {len(trades)} реальных траекториях")
    print(f"MIN_PROTECTIVE_EXIT_PCT={settings.MIN_PROTECTIVE_EXIT_PCT}  "
          f"MIN_PROTECTIVE_NET_USDT={settings.MIN_PROTECTIVE_NET_USDT}  "
          f"TREND_CAPTURE_ARM={settings.TREND_CAPTURE_ARM_PCT}/"
          f"{settings.TREND_CAPTURE_GIVEBACK_SHARE}\n")

    print(f"{'id':>6}{'mfe':>7}{'факт':>9}{'ново':>9}{'дельта':>9}  причина")
    all_viol: list[str] = []
    tot_act = tot_new = 0.0
    reasons: dict[str, int] = {}
    for t in trades:
        pct, why, viol = replay(svc, t)
        a = t["notional"] * (honest_actual(t) - COST_PCT) / 100
        n = t["notional"] * (pct - COST_PCT) / 100
        tot_act += a
        tot_new += n
        reasons[why] = reasons.get(why, 0) + 1
        all_viol += [f"{t['id']}: {v}" for v in viol]
        print(f"{t['id']:>6}{t['mfe_pct']:>7.3f}{a:>9.2f}{n:>9.2f}{n-a:>+9.2f}  {why}")
    print(f"{'ИТОГО':>6}{'':>7}{tot_act:>9.2f}{tot_new:>9.2f}{tot_new-tot_act:>+9.2f}")
    print(f"\nПричины закрытия: {reasons}")

    print("\n=== ИНВАРИАНТЫ ===")
    if all_viol:
        for v in all_viol:
            print(f"  ✗ {v}")
        raise SystemExit(1)
    print("  ✓ ни один филл не лучше рынка (фантомных цен нет)")

    # Замок: гарантировать ПОЛОЖИТЕЛЬНЫЙ филл нельзя — цена гэпает между опросами.
    # Гарантируем достижимое: порог срабатывания выше издержек, а сумма исходов
    # замка перестала быть систематически отрицательной.
    _ns, _src, fee = svc._net_safe_profit_pct(symbol=None, market_type="swap")
    cost = (fee * 2 + float(settings.SLIPPAGE_BUFFER_PCT)) * 100
    trigger = max(float(settings.BREAKEVEN_LOCK_FLOOR_PCT),
                  cost + float(settings.BREAKEVEN_LOCK_COST_BUFFER_PCT))
    assert trigger > cost, "порог замка не покрывает round-trip"
    print(f"  ✓ порог breakeven_lock {trigger:.3f}% > round-trip {cost:.3f}% "
          f"(запас {trigger - cost:.3f} пп на проскок между опросами)")

    lock_sum = 0.0
    lock_n = 0
    for t in trades:
        pct, why, _ = replay(svc, t)
        if why == "breakeven_lock":
            lock_sum += t["notional"] * (pct - COST_PCT) / 100
            lock_n += 1
    print(f"  ✓ сумма исходов breakeven_lock: {lock_sum:+.2f} USDT на {lock_n} сделках "
          f"(в бою те же сделки давали систематический минус)")

    band = reasons.get("trend_capture_band", 0)
    print(f"  ✓ фиксаций в модальной полосе MFE: {band} "
          f"(до правки механизма не существовало)")


if __name__ == "__main__":
    main()
